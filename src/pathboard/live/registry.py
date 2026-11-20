"""The board's registry of latest values, kept by the writers themselves.

The design doc's read marks with versions need, for each path, the latest value.  A real board
cannot read the app's server, so writers publish: right after an action of theirs, a writer
reports the values its own page now shows for the paths that action edited (the app refreshes
the page's data right after each save).  The writer still holds the path, or the per-action
lock, when it publishes, so no other worker can save the same path in between.

The registry knows only what the board's own workers wrote.  A change made outside the board
(a person, another program) never reaches it; ``checks`` can add the reader's own page for that.
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
        self._values: dict[str, Any] = {}
        self.publishes = 0

    def publish(self, values: dict[str, Any]) -> None:
        if not values:
            return
        with self._lock:
            self._values.update(values)
            self.publishes += 1

    def latest(self, paths: tuple[str, ...] | frozenset[str]) -> dict[str, Any]:
        with self._lock:
            return under(self._values, paths)
