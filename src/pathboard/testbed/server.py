"""HTTP server for the Ledger testbed.

Two kinds of endpoints:

* ``/api/...`` is the application's own API, used by the web page.
* ``/__gt/...`` is ground-truth instrumentation for the experimenter only.
  Agents and observers must never call it.

Every mutating API call is appended to an operation log together with the leaf
paths it changed (the diff of the full state before and after).
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Literal
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

from .state import ASSIGNEES, CATEGORIES, STATUSES, Ledger, LedgerConfig, LedgerError, diff_flat

RenderMode = Literal["granular", "rerender_keyed", "rerender_anon"]


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int  # 0 picks a free port
    render_mode: RenderMode  # how the page applies updates (experimental variable)
    poll_interval_ms: int  # how often the page asks for other actors' changes
    api_latency_ms: int  # artificial delay added to every /api call
    page_size: int  # rows per page in the invoice table


_STATIC_TYPES = {
    "app.js": "application/javascript",
    "app.css": "text/css",
    "index.html": "text/html",
}


class Testbed:
    """Ledger state plus the operation log, guarded by one lock."""

    def __init__(self, ledger_cfg: LedgerConfig, server_cfg: ServerConfig):
        self.lock = threading.RLock()
        self.server_cfg = server_cfg
        self.reset(ledger_cfg)

    def reset(self, ledger_cfg: LedgerConfig) -> None:
        with self.lock:
            self.ledger = Ledger(ledger_cfg)
            self.oplog: list[dict[str, Any]] = []
            self.seq = 0

    def mutate(
        self, actor: str, op: str, args: dict[str, Any], fn: Callable[[], Any]
    ) -> tuple[Any, int]:
        """Run a mutating operation and log its ground-truth effect."""
        with self.lock:
            before = self.ledger.flat()
            result = fn()  # raises LedgerError without changing state
            writes = diff_flat(before, self.ledger.flat())
            self.seq += 1
            self.oplog.append(
                {
                    "seq": self.seq,
                    "t_mono": time.monotonic(),
                    "actor": actor,
                    "op": op,
                    "args": args,
                    "writes": writes,
                }
            )
            return result, self.seq

    def changes_since(self, since: int) -> dict[str, Any]:
        """What a client must refresh: records whose shown values changed."""
        with self.lock:
            inv_ids: set[str] = set()
            ven_ids: set[str] = set()
            batches = False
            for entry in self.oplog[since:]:
                for path in entry["writes"]:
                    parts = path.split("/")
                    if parts[1] == "invoice":
                        inv_ids.add(parts[2])
                    elif parts[1] == "vendor":
                        ven_ids.add(parts[2])
                    elif parts[1] in ("batch", "batch-index"):
                        batches = True
            led = self.ledger
            # A vendor rename changes what every one of its invoices displays.
            for iid, inv in led.invoices.items():
                if inv["vendor_id"] in ven_ids:
                    inv_ids.add(iid)
            return {
                "seq": self.seq,
                "invoices": [led.invoice_record(i) for i in sorted(inv_ids) if i in led.invoices],
                "removed_invoices": sorted(i for i in inv_ids if i not in led.invoices),
                "vendors": [led.vendor_record(v) for v in sorted(ven_ids)],
                "batches": self.batch_list() if batches else None,
            }

    def batch_list(self) -> list[dict[str, Any]]:
        return [{"id": bid, **b} for bid, b in self.ledger.batches.items()]


def _make_handler(tb: Testbed):  # noqa: C901  the request handler class is defined inside
    static_dir = resources.files("pathboard.testbed") / "static"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout clean
            return

        # ---- plumbing ----------------------------------------------------
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise LedgerError("invalid", "request body must be a JSON object")
            return data

        def _actor(self) -> str:
            return self.headers.get("X-Actor", "unknown")

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            path = url.path
            try:
                if path.startswith("/__gt/"):
                    self._gt(method, path, parse_qs(url.query))
                elif path.startswith("/api/"):
                    if tb.server_cfg.api_latency_ms:
                        time.sleep(tb.server_cfg.api_latency_ms / 1000.0)
                    self._api(method, path, parse_qs(url.query))
                elif method == "GET":
                    self._static(path)
                else:
                    self._json(405, {"error": "method not allowed"})
            except LedgerError as exc:
                status = {"invalid": 400, "not_found": 404, "locked": 409}[exc.code]
                self._json(status, {"error": str(exc), "code": exc.code})
            except json.JSONDecodeError:
                self._json(400, {"error": "malformed JSON", "code": "invalid"})

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PATCH(self) -> None:
            self._dispatch("PATCH")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        # ---- static page -------------------------------------------------
        def _static(self, path: str) -> None:
            name = path[len("/static/") :] if path.startswith("/static/") else "index.html"
            if name not in _STATIC_TYPES:
                self._json(404, {"error": "not found"})
                return
            self._send(
                200, (static_dir / name).read_bytes(), _STATIC_TYPES[name] + "; charset=utf-8"
            )

        # ---- application API ---------------------------------------------
        def _api(self, method: str, path: str, query: dict[str, list[str]]) -> None:  # noqa: C901  flat router
            led = tb.ledger
            actor = self._actor()
            m = lambda pattern: re.fullmatch(pattern, path)  # noqa: E731

            if method == "GET" and path == "/api/config":
                c = tb.server_cfg
                self._json(
                    200,
                    {
                        "render_mode": c.render_mode,
                        "poll_interval_ms": c.poll_interval_ms,
                        "page_size": c.page_size,
                        "categories": CATEGORIES,
                        "assignees": ASSIGNEES,
                        "statuses": STATUSES,
                    },
                )
            elif method == "GET" and path == "/api/bootstrap":
                with tb.lock:
                    self._json(
                        200,
                        {
                            "seq": tb.seq,
                            "invoices": [led.invoice_record(i) for i in led.invoices],
                            "vendors": [led.vendor_record(v) for v in led.vendors],
                            "batches": tb.batch_list(),
                        },
                    )
            elif method == "GET" and path == "/api/changes":
                since = int(query["since"][0])
                self._json(200, tb.changes_since(since))
            elif method == "POST" and path == "/api/invoices":
                body = self._body()
                iid, seq = tb.mutate(
                    actor, "create_invoice", body, lambda: led.create_invoice(body)
                )
                self._json(201, {"seq": seq, "id": iid})
            elif method == "POST" and path == "/api/bulk":
                body = self._body()
                ids, fields = body.get("ids"), body.get("fields")
                if not isinstance(ids, list) or not isinstance(fields, dict):
                    raise LedgerError("invalid", "bulk needs 'ids' and 'fields'")
                _, seq = tb.mutate(actor, "bulk_update", body, lambda: led.bulk_update(ids, fields))
                self._json(200, {"seq": seq})
            elif method == "POST" and path == "/api/batches":
                body = self._body()
                ids = body.get("ids")
                if not isinstance(ids, list):
                    raise LedgerError("invalid", "batch needs 'ids'")
                bid, seq = tb.mutate(actor, "create_batch", body, lambda: led.create_batch(ids))
                self._json(201, {"seq": seq, "id": bid})
            elif (mm := m(r"/api/invoices/([^/]+)")) and method == "PATCH":
                body = self._body()
                _, seq = tb.mutate(
                    actor,
                    "update_invoice",
                    {"id": mm[1], **body},
                    lambda: led.update_invoice(mm[1], body),
                )
                self._json(200, {"seq": seq})
            elif (mm := m(r"/api/invoices/([^/]+)")) and method == "DELETE":
                _, seq = tb.mutate(
                    actor, "delete_invoice", {"id": mm[1]}, lambda: led.delete_invoice(mm[1])
                )
                self._json(200, {"seq": seq})
            elif (mm := m(r"/api/invoices/([^/]+)/lines")) and method == "POST":
                body = self._body()
                lid, seq = tb.mutate(
                    actor, "add_line", {"id": mm[1], **body}, lambda: led.add_line(mm[1], body)
                )
                self._json(201, {"seq": seq, "id": lid})
            elif (mm := m(r"/api/invoices/([^/]+)/lines/([^/]+)")) and method == "PATCH":
                body = self._body()
                _, seq = tb.mutate(
                    actor,
                    "update_line",
                    {"id": mm[1], "line": mm[2], **body},
                    lambda: led.update_line(mm[1], mm[2], body),
                )
                self._json(200, {"seq": seq})
            elif (mm := m(r"/api/invoices/([^/]+)/lines/([^/]+)")) and method == "DELETE":
                _, seq = tb.mutate(
                    actor,
                    "remove_line",
                    {"id": mm[1], "line": mm[2]},
                    lambda: led.remove_line(mm[1], mm[2]),
                )
                self._json(200, {"seq": seq})
            elif (mm := m(r"/api/vendors/([^/]+)")) and method == "PATCH":
                body = self._body()
                _, seq = tb.mutate(
                    actor,
                    "update_vendor",
                    {"id": mm[1], **body},
                    lambda: led.update_vendor(mm[1], body),
                )
                self._json(200, {"seq": seq})
            else:
                self._json(404, {"error": "not found", "code": "not_found"})

        # ---- ground truth (experimenter only) ------------------------------
        def _gt(self, method: str, path: str, query: dict[str, list[str]]) -> None:
            if method == "GET" and path == "/__gt/state":
                with tb.lock:
                    self._json(200, {"seq": tb.seq, "flat": tb.ledger.flat()})
            elif method == "GET" and path == "/__gt/oplog":
                since = int(query["since"][0])
                with tb.lock:
                    self._json(200, {"seq": tb.seq, "entries": tb.oplog[since:]})
            else:
                self._json(404, {"error": "not found"})

    return Handler


class TestbedServer:
    """Runs the testbed in a background thread; use as a context manager."""

    __test__ = False  # not a pytest test class

    def __init__(self, ledger_cfg: LedgerConfig, server_cfg: ServerConfig):
        self.testbed = Testbed(ledger_cfg, server_cfg)
        self._httpd = ThreadingHTTPServer(
            (server_cfg.host, server_cfg.port), _make_handler(self.testbed)
        )
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://{self.testbed.server_cfg.host}:{self.port}"

    def start(self) -> TestbedServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> TestbedServer:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # Direct, in-process access for the experiment harness -------------------
    def reset(self, ledger_cfg: LedgerConfig) -> None:
        self.testbed.reset(ledger_cfg)

    def flat(self) -> dict[str, Any]:
        with self.testbed.lock:
            return self.testbed.ledger.flat()

    def seq(self) -> int:
        with self.testbed.lock:
            return self.testbed.seq

    def oplog_since(self, since: int) -> list[dict[str, Any]]:
        with self.testbed.lock:
            return list(self.testbed.oplog[since:])


def main() -> None:
    """Serve the testbed for manual inspection: ``python -m pathboard.testbed.server CONFIG``."""
    import sys

    from ..config import ConfigError, from_dict, load_yaml

    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m pathboard.testbed.server configs/testbed.yaml")
    raw = load_yaml(sys.argv[1])
    if set(raw) != {"ledger", "server"}:
        raise ConfigError("testbed config needs exactly 'ledger' and 'server' sections")
    server = TestbedServer(
        from_dict(LedgerConfig, raw["ledger"]), from_dict(ServerConfig, raw["server"])
    ).start()
    print(f"Ledger testbed running at {server.base_url}  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
