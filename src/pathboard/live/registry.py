"""The board's registry of versions, kept by the writers themselves.

The design doc's read marks with versions need, for each path, the version of its latest write.
A real board cannot read the app's server, so writers publish: right after an action of theirs
that saved, a writer reports, for each path that action changed on its own page, the app's
change number for that save (the number is in the app's reply to the save, which the writer's
page receives; the app refreshes the page's data right after).  The writer still holds the path,
or the per-action lock, when it publishes, so no other worker can save the same path in between.

A reader's read mark is the change number its page's data was up to when it read (also from the
app's own replies).  Its edit is stale if the registry holds a later number for its target: a
board worker saved the target after the reader's page last caught up.

The registry knows only what the board's own workers wrote.  A change made outside the board
(a person, another program) never reaches it; ``checks`` can add the reader's own page for that.
Versions, not values, are compared, so an outside change does not make the registry look newer
than a page that has seen it.
"""

from __future__ import annotations

import threading
from typing import Any


def under(values: dict[str, Any], paths: tuple[str, ...] | frozenset[str]) -> dict[str, Any]:
    """The entries of ``values`` at or below any of ``paths``."""
    return {k: v for k, v in values.items() if any(k == t or k.startswith(t + "/") for t in paths)}


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._versions: dict[str, int] = {}
        self.publishes = 0

    def publish(self, versions: dict[str, int]) -> None:
        if not versions:
            return
        with self._lock:
            for k, v in versions.items():
                self._versions[k] = max(v, self._versions.get(k, v))
            self.publishes += 1

    def latest(self, paths: tuple[str, ...] | frozenset[str]) -> dict[str, int]:
        with self._lock:
            return under(self._versions, paths)
