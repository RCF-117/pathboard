"""Resource paths and the single conflict rule used everywhere.

A path names a piece of state from coarse to fine, e.g.
``data/invoice/INV-1042/category``.  Two paths conflict exactly when one is a
prefix of the other (or they are equal).  A rough read is a short path, a
precise write is a long one, and an operation over a whole collection uses the
collection's path.
"""

from __future__ import annotations

from typing import Iterable

SEP = "/"


def split(path: str) -> tuple[str, ...]:
    """Split a path into segments, rejecting malformed paths loudly."""
    if not isinstance(path, str) or path == "":
        raise ValueError(f"path must be a non-empty string, got {path!r}")
    parts = tuple(path.split(SEP))
    if any(p == "" for p in parts):
        raise ValueError(f"path has an empty segment: {path!r}")
    return parts


def covers(a: str, b: str) -> bool:
    """True when ``a`` equals ``b`` or is a prefix of it (``a`` is coarser)."""
    pa, pb = split(a), split(b)
    return len(pa) <= len(pb) and pb[: len(pa)] == pa


def conflicts(a: str, b: str) -> bool:
    """True when one path is a prefix of the other."""
    return covers(a, b) or covers(b, a)


def any_conflict(xs: Iterable[str], ys: Iterable[str]) -> bool:
    """True when any path in ``xs`` conflicts with any path in ``ys``."""
    index = PathIndex(ys)
    return any(index.conflicts_with(x) for x in xs)


def minimal_cover(paths: Iterable[str]) -> frozenset[str]:
    """Drop every path that is covered by a coarser path in the same set."""
    kept: list[tuple[str, ...]] = []
    for parts in sorted({split(p) for p in paths}, key=len):
        if not any(parts[: len(k)] == k for k in kept):
            kept.append(parts)
    return frozenset(SEP.join(k) for k in kept)


class PathIndex:
    """A set of paths with fast conflict queries (a trie over segments)."""

    __slots__ = ("_root", "_n")
    _END = object()

    def __init__(self, paths: Iterable[str] = ()):
        self._root: dict = {}
        self._n = 0
        for p in paths:
            self.add(p)

    def __len__(self) -> int:
        return self._n

    def add(self, path: str) -> None:
        node = self._root
        for seg in split(path):
            node = node.setdefault(seg, {})
        if self._END not in node:
            node[self._END] = True
            self._n += 1

    def conflicts_with(self, path: str) -> bool:
        node = self._root
        for seg in split(path):
            if self._END in node:  # a stored path is a strict prefix of `path`
                return True
            nxt = node.get(seg)
            if nxt is None:
                return False
            node = nxt
        # `path` is fully consumed: any stored path at or below here conflicts.
        return len(node) > 0

    def covered(self, path: str) -> bool:
        """True when a stored path equals ``path`` or is a prefix of it."""
        node = self._root
        for seg in split(path):
            if self._END in node:
                return True
            nxt = node.get(seg)
            if nxt is None:
                return False
            node = nxt
        return self._END in node
