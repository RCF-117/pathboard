"""Run directories and manifests so every result can be traced and reproduced."""

from __future__ import annotations

import dataclasses
import json
import platform
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path
from typing import Any

from . import __version__

_TRACKED = ("numpy", "PyYAML", "playwright", "anthropic", "matplotlib")


def _git_state(repo: Path) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", *args], cwd=repo, capture_output=True, text=True, timeout=10
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return {"commit": commit, "dirty": None if status is None else status != ""}


def _versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in _TRACKED:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    return out


def _jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        seq = sorted(obj) if isinstance(obj, (set, frozenset)) else obj
        return [_jsonable(v) for v in seq]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def create_run_dir(root: str | Path, experiment: str, tag: str) -> Path:
    """Create ``root/experiment/<utc timestamp>-<tag>``; never reuse a directory."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = Path(root) / experiment / f"{stamp}-{tag}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_manifest(run_dir: Path, experiment: str, config: Any, extra: dict[str, Any]) -> Path:
    """Record config, code version, package versions and platform next to results."""
    repo = Path(__file__).resolve().parents[2]
    manifest = {
        "experiment": experiment,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sharedws_version": __version__,
        "git": _git_state(repo),
        "python": sys.version,
        "platform": platform.platform(),
        "packages": _versions(),
        "argv": sys.argv,
        "config": _jsonable(config),
        **_jsonable(extra),
    }
    out = run_dir / "manifest.json"
    with open(out, "x", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    return out


def write_json(path: Path, obj: Any) -> None:
    with open(path, "x", encoding="utf-8") as fh:
        json.dump(_jsonable(obj), fh, indent=2, sort_keys=True)


def append_jsonl(path: Path, obj: Any) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_jsonable(obj), sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
