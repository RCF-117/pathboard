"""Score an observer's answer against ground truth.

Both are sets of paths.  An observer path counts as naming every leaf it
covers (itself or anything beneath it).  The universe is every leaf that
existed before or after the action: all data leaves plus the tab's view leaves.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..paths import PathIndex


@dataclass(frozen=True)
class Score:
    n_truth: int  # leaves that really changed
    n_claimed: int  # leaves the observer's answer covers (plus phantom paths)
    n_hit: int  # changed leaves the answer covers
    n_phantom: int  # answer paths that match no existing leaf
    recall: float  # n_hit / n_truth          (1.0 when nothing changed)
    precision: float  # n_hit / n_claimed     (1.0 when nothing was claimed)
    visible_recall: float  # recall restricted to changed leaves the page displayed
    n_truth_visible: int
    verdict: str  # exact | too_wide | too_narrow | wide_and_narrow

    @property
    def safe(self) -> bool:
        """No real change was missed.  This is what a coordinator needs first."""
        return self.n_hit == self.n_truth


def score(truth: frozenset[str], claimed: frozenset[str], universe: frozenset[str], visible: frozenset[str]) -> Score:
    stray = truth - universe
    if stray:
        raise ValueError(f"ground-truth leaves outside the universe: {sorted(stray)[:5]}")
    index = PathIndex(claimed)
    covered = frozenset(u for u in universe if index.covered(u))
    leaves = PathIndex(universe)
    phantom = sum(1 for c in claimed if not leaves.conflicts_with(c))
    hit = covered & truth
    n_claimed = len(covered) + phantom
    truth_visible = truth & visible
    missed, extra = len(truth) - len(hit), n_claimed - len(hit)
    verdict = ("exact" if not missed and not extra else "too_wide" if not missed
               else "too_narrow" if not extra else "wide_and_narrow")
    return Score(
        n_truth=len(truth), n_claimed=n_claimed, n_hit=len(hit), n_phantom=phantom,
        recall=len(hit) / len(truth) if truth else 1.0,
        precision=len(hit) / n_claimed if n_claimed else 1.0,
        visible_recall=len(covered & truth_visible) / len(truth_visible) if truth_visible else 1.0,
        n_truth_visible=len(truth_visible), verdict=verdict)
