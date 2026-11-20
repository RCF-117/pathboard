"""Static figures for the experiment reports.

Colours are a fixed categorical order (a palette validated for colour-vision
deficiency); a series keeps its colour wherever it appears.  Every figure has
one y-axis, a legend, and the numbers are also written to a JSON summary.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence

SERIES_COLORS = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
)
_MARKERS = ("o", "s", "^", "D", "v", "P", "X", "*")
_SURFACE, _INK, _MUTED, _GRID = "#fcfcfb", "#1f1f1c", "#6b6a63", "#e4e3dd"


def _axes(title: str, xlabel: str, ylabel: str):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7.2, 4.4), dpi=160)
    fig.patch.set_facecolor(_SURFACE)
    ax.set_facecolor(_SURFACE)
    ax.set_title(title, loc="left", fontsize=11, color=_INK)
    ax.set_xlabel(xlabel, color=_MUTED)
    ax.set_ylabel(ylabel, color=_MUTED)
    ax.grid(axis="y", color=_GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(_GRID)
    ax.tick_params(colors=_MUTED)
    return fig, ax


def line_chart(
    path: Path,
    title: str,
    xlabel: str,
    ylabel: str,
    x: Sequence[float],
    series: dict[str, tuple[Sequence[float], Sequence[float], Sequence[float]]],
    reference: float | None,
) -> None:
    """Lines with confidence bands. ``series[name] = (estimate, low, high)`` aligned with ``x``."""
    if len(series) > len(SERIES_COLORS):
        raise ValueError(f"at most {len(SERIES_COLORS)} series per chart; split the figure instead")
    fig, ax = _axes(title, xlabel, ylabel)
    for i, (name, (est, low, high)) in enumerate(series.items()):
        if not (len(est) == len(low) == len(high) == len(x)):
            raise ValueError(f"series {name!r} is not aligned with x")
        ax.fill_between(x, low, high, color=SERIES_COLORS[i], alpha=0.14, linewidth=0)
        ax.plot(
            x,
            est,
            color=SERIES_COLORS[i],
            linewidth=2,
            marker=_MARKERS[i],
            markersize=6,
            label=name,
        )
    if reference is not None:
        ax.axhline(reference, color=_MUTED, linewidth=1, linestyle=(0, (4, 3)))
    ax.set_xticks(list(x))
    ax.legend(frameon=False, fontsize=8, labelcolor=_INK)
    fig.tight_layout()
    fig.savefig(path)
    import matplotlib.pyplot as plt

    plt.close(fig)


def grouped_bars(
    path: Path,
    title: str,
    xlabel: str,
    ylabel: str,
    groups: Sequence[str],
    series: dict[str, tuple[Sequence[float], Sequence[float], Sequence[float]]],
    ymax: float | None,
) -> None:
    """Grouped bars with interval whiskers. ``series[name] = (estimate, low, high)`` per group."""
    if len(series) > len(SERIES_COLORS):
        raise ValueError(f"at most {len(SERIES_COLORS)} series per chart; split the figure instead")
    fig, ax = _axes(title, xlabel, ylabel)
    n = len(series)
    width = 0.8 / n
    for i, (name, (est, low, high)) in enumerate(series.items()):
        if not (len(est) == len(low) == len(high) == len(groups)):
            raise ValueError(f"series {name!r} is not aligned with groups")
        xs = [g + (i - (n - 1) / 2) * width for g in range(len(groups))]
        err = [
            [e - lo for e, lo in zip(est, low, strict=True)],
            [h - e for e, h in zip(est, high, strict=True)],
        ]
        ax.bar(
            xs,
            est,
            width=width * 0.9,
            color=SERIES_COLORS[i],
            label=name,
            yerr=err,
            ecolor=_INK,
            error_kw={"linewidth": 1, "capsize": 2},
        )
    ax.set_xticks(range(len(groups)))
    ax.set_xticklabels(groups, rotation=20, ha="right")
    if ymax is not None:
        ax.set_ylim(0, ymax)
    ax.legend(frameon=False, fontsize=8, labelcolor=_INK)
    fig.tight_layout()
    fig.savefig(path)
    import matplotlib.pyplot as plt

    plt.close(fig)
