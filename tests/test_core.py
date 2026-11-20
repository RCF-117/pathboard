import dataclasses
import random
from typing import Literal

import numpy as np
import pytest

from sharedws.config import ConfigError, from_dict
from sharedws.paths import PathIndex, any_conflict, conflicts, covers, minimal_cover, split
from sharedws.stats import cluster_bootstrap_ci, geometric_mean, wilson_interval


# ---- paths -------------------------------------------------------------------
def test_prefix_rule():
    assert conflicts("data/invoice/INV-1", "data/invoice/INV-1/category")
    assert conflicts("data/invoice/INV-1/category", "data/invoice/INV-1")
    assert conflicts("a/b", "a/b")
    assert not conflicts("data/invoice/INV-1", "data/invoice/INV-10")  # segment, not string, prefix
    assert not conflicts("data/invoice/INV-1/category", "data/invoice/INV-1/notes")
    assert covers("a", "a/b") and not covers("a/b", "a")


@pytest.mark.parametrize("bad", ["", "a//b", "/a", "a/"])
def test_malformed_paths_fail(bad):
    with pytest.raises(ValueError):
        split(bad)


def test_index_matches_brute_force():
    rng = random.Random(0)
    rand = lambda: "/".join(rng.choice("abc") for _ in range(rng.randint(1, 4)))  # noqa: E731
    for _ in range(200):
        stored = [rand() for _ in range(rng.randint(0, 8))]
        index = PathIndex(stored)
        for _ in range(20):
            q = rand()
            assert index.conflicts_with(q) == any(conflicts(q, s) for s in stored)
            assert index.covered(q) == any(covers(s, q) for s in stored)
        assert any_conflict(stored, [q]) == index.conflicts_with(q)


def test_minimal_cover():
    assert minimal_cover(["a/b", "a", "c/d", "c/d/e"]) == frozenset({"a", "c/d"})


# ---- config ------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Inner:
    rate: float


@dataclasses.dataclass(frozen=True)
class Outer:
    name: str
    n: int
    mode: Literal["x", "y"]
    inner: Inner
    opt: Inner | None
    items: tuple[int, ...]


GOOD = {"name": "a", "n": 3, "mode": "x", "inner": {"rate": 1}, "opt": None, "items": [1, 2]}


def test_config_ok():
    cfg = from_dict(Outer, GOOD)
    assert cfg.inner.rate == 1.0 and cfg.items == (1, 2) and cfg.opt is None


@pytest.mark.parametrize("change", [
    lambda d: d.pop("n"),  # missing key
    lambda d: d.update(extra=1),  # unknown key
    lambda d: d.update(mode="z"),  # not an allowed value
    lambda d: d.update(n="3"),  # wrong type
    lambda d: d.update(n=True),  # bool is not an int here
    lambda d: d["inner"].update(more=1),  # unknown nested key
])
def test_config_rejects(change):
    d = {**GOOD, "inner": dict(GOOD["inner"])}
    change(d)
    with pytest.raises(ConfigError):
        from_dict(Outer, d)


# ---- stats -------------------------------------------------------------------
def test_wilson_known_value():
    p, lo, hi = wilson_interval(8, 10, 0.95)
    assert p == 0.8 and lo == pytest.approx(0.4902, abs=1e-3) and hi == pytest.approx(0.9433, abs=1e-3)


def test_geometric_mean():
    assert geometric_mean(np.array([1.0, 4.0])) == pytest.approx(2.0)
    with pytest.raises(ValueError):
        geometric_mean(np.array([1.0, 0.0]))


def test_bootstrap_constant_and_coverage():
    rng = np.random.default_rng(0)
    est, lo, hi = cluster_bootstrap_ci({"a": [2.0, 2.0], "b": [2.0]}, lambda x: float(np.mean(x)), 200, 0.95, rng)
    assert est == lo == hi == 2.0
    # Clusters contribute their mean: a cluster with many runs does not get more weight.
    est, _, _ = cluster_bootstrap_ci({"a": [0.0] * 50, "b": [1.0]}, lambda x: float(np.mean(x)), 200, 0.95, rng)
    assert est == 0.5
    with pytest.raises(ValueError):
        cluster_bootstrap_ci({"a": [float("nan")]}, lambda x: float(np.mean(x)), 10, 0.95, rng)
