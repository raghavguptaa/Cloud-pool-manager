"""Regenerate the two result figures from the captured telemetry.

Called by `sim.replay.main()`; `make sim` redraws both from the CSVs every time, so a
figure in `results/` can never drift away from the data it claims to show.

Three deliberate departures from the original figures, all documented in
`data/PROVENANCE.md`:

* **Axes are labelled UTC, because the data is UTC.** The original pair were titled
  "Time (IST)" while plotting UTC (baseline arm) and UTC+06:00 (governor arm). IST is
  shown here as a secondary annotation and never as the axis.
* **The governor figure stops where the export stops.** It ends mid-decay at
  11:47:00Z, and the missing minutes are drawn as an explicit no-data region rather
  than extended to the tidier ending the recorded timeline describes.
* **Rendering is deterministic.** Volatile PNG metadata is suppressed, so re-running
  produces a byte-identical file and a redraw shows up in `git status` only when the
  picture actually changed.

Colour carries meaning across both figures: **blue is measured CPU**, **orange is the
control regime** -- the alarm's blind window in one, the threshold bands in the other.
"""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")  # headless: CI has no display, and neither does a Lambda box

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from governor.config import DEFAULT, Config, Phase  # noqa: E402
from sim.telemetry import MetricSeries  # noqa: E402

if TYPE_CHECKING:  # imported for typing only -- sim.replay imports this module
    from sim.replay import Decision, LatencyComparison

__all__ = ["render_baseline", "render_governor"]

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#7a7973"
GRID = "#e8e7e2"
MEASURED = "#2a78d6"  # blue: the CPU series, in both figures
REGIME = "#eb6834"  # orange: thresholds, phases, the alarm window
REGIME_TINT = ("#fefaf7", "#fdf0e8", "#fbe1d2")  # one hue, light -> dark, by phase

# Band fills are keyed by phase so the figure cannot drift from the FSM's own ordering.
_BAND_TINT = dict(zip(Phase, REGIME_TINT, strict=True))

_PNG_METADATA = {"Software": None, "Date": None}
"""Everything matplotlib would otherwise stamp into the PNG that changes between runs
or between machines. Suppressing it is what makes `make sim` idempotent."""

_DPI = 150
_MARGIN = 0.012  # figure-relative left margin shared by title, subtitle and footnote


def render_baseline(
    series: MetricSeries, latency: LatencyComparison, path: Path
) -> Path:
    """Draw the reactive baseline arm: three datapoints and a 4m36s blind window."""
    _apply_style()
    fig, ax = plt.subplots(figsize=(10.0, 6.2))
    fig.subplots_adjust(left=0.075, right=0.985, top=0.855, bottom=0.19)

    times = [_naive(sample.timestamp) for sample in series]
    values = [sample.cpu for sample in series]
    spike = _naive(latency.spike.time_utc)
    alarm = _naive(latency.alarm.time_utc)

    # The window the system was blind for: load is running, the alarm has not fired.
    ax.axvspan(spike, alarm, color=REGIME, alpha=0.10, linewidth=0)
    ax.axvline(spike, color=INK_MUTED, linewidth=1.0)
    ax.axvline(alarm, color=REGIME, linewidth=1.8)

    ax.axhline(50.0, color=INK_MUTED, linewidth=1.0, linestyle=(0, (5, 4)))
    ax.text(
        0.997,
        51.5,
        "alarm threshold (> 50 %)",
        transform=ax.get_yaxis_transform(),  # x in axes fraction, y in CPU %
        ha="right",
        color=INK_SECONDARY,
        fontsize=9,
    )

    ax.plot(
        times,
        values,
        color=MEASURED,
        linewidth=1.8,
        marker="o",
        markersize=7,
        markerfacecolor=MEASURED,
        markeredgecolor=SURFACE,
        markeredgewidth=1.5,
        zorder=3,
    )

    # Three points: labelling all of them is the whole argument, not clutter. The
    # series climbs left-to-right into its peak and falls away after it, so a label
    # sits above-left of its point, and the final point takes its label on the right.
    last = len(values) - 1
    for index, (moment, value) in enumerate(zip(times, values, strict=True)):
        offset, ha, va = ((10, 0), "left", "center") if index == last else (
            (-10, 8),
            "right",
            "bottom",
        )
        ax.annotate(
            f"{value:.2f} %",
            xy=(moment, value),
            xytext=offset,
            textcoords="offset points",
            fontsize=10,
            color=INK,
            ha=ha,
            va=va,
        )

    # The 60-70 band is kept clear of data so the two event labels have somewhere to
    # live that is not on top of the series.
    ax.annotate(
        f"CloudWatch alarm fires\n{latency.alarm.time_utc:%H:%M:%S}Z"
        f"  (+{_mmss(latency.baseline_alarm_s)})",
        xy=(alarm, 64.0),
        xytext=(8, 0),
        textcoords="offset points",
        fontsize=9.5,
        color=REGIME,
        va="center",
        fontweight="bold",
    )
    ax.annotate(
        f"{_mmss(latency.baseline_alarm_s)} blind:\nload running, nothing scaled",
        xy=(spike + (alarm - spike) / 2, 64.0),
        ha="center",
        va="center",
        fontsize=10,
        color=INK_SECONDARY,
    )
    ax.annotate(
        "load starts",
        xy=(spike, 2.0),
        xytext=(-8, 0),
        textcoords="offset points",
        fontsize=9,
        color=INK_SECONDARY,
        ha="right",
    )

    ax.set_ylim(0, 70)
    ax.set_xlim(
        _naive(series.start) - timedelta(minutes=3),
        _naive(series.end) + timedelta(minutes=4),
    )
    ax.yaxis.set_major_locator(mticker.MultipleLocator(10))
    _format_time_axis(ax, minutes=5)
    ax.set_ylabel("CPU utilisation (%)")
    ax.set_xlabel(_utc_xlabel(series, ist_hint="00:25-00:35 IST on 2026-03-17"))

    _titles(
        fig,
        title=f"Reactive baseline: {_mmss(latency.baseline_alarm_s)} to notice the spike",
        subtitle=_provenance(series),
        caption=(
            "Three datapoints is the whole incident: at 300 s Average the peak reads "
            f"{series.peak.cpu:.2f} %, under the alarm's own > 50 % threshold.\n"
            "Once the alarm did fire the warm pool had an instance running "
            f"{_mmss(latency.baseline_extraction_s)} later -- the delay was all detection, "
            "not capacity."
        ),
    )
    return _save(fig, path)


def render_governor(
    series: MetricSeries,
    decisions: Sequence[Decision],
    path: Path,
    cfg: Config = DEFAULT,
) -> Path:
    """Draw the governor arm: the 1-minute series against the phase bands it drove."""
    _apply_style()
    fig, ax = plt.subplots(figsize=(11.5, 6.4))
    fig.subplots_adjust(left=0.065, right=0.99, top=0.855, bottom=0.19)

    top = 60.0
    edges = (0.0, cfg.scale_in_threshold, cfg.pre_warm_threshold, top)
    for phase, lower, upper in zip(Phase, edges[:-1], edges[1:], strict=True):
        ax.axhspan(lower, upper, color=_BAND_TINT[phase], linewidth=0, zorder=0)
    for edge in edges[1:-1]:
        ax.axhline(edge, color=REGIME, linewidth=1.0, alpha=0.55, zorder=1)

    times = [_naive(sample.timestamp) for sample in series]
    values = [sample.cpu for sample in series]
    ax.plot(
        times,
        values,
        color=MEASURED,
        linewidth=1.8,
        marker="o",
        markersize=4.5,
        markerfacecolor=MEASURED,
        markeredgecolor=SURFACE,
        markeredgewidth=1.0,
        zorder=3,
    )

    changes = [decision for decision in decisions if decision.is_transition]
    _draw_bands_key(ax, cfg, edges, top, series)
    _draw_transitions(ax, changes, top)
    _draw_export_gap(ax, series, top)

    ax.set_ylim(0, top)
    ax.set_xlim(
        _naive(series.start) - timedelta(minutes=2),
        _naive(series.end) + timedelta(minutes=6),
    )
    ax.yaxis.set_major_locator(mticker.MultipleLocator(10))
    _format_time_axis(ax, minutes=5)
    ax.set_ylabel("CPU utilisation (%)")
    ax.set_xlabel(_utc_xlabel(series, ist_hint="16:52-17:17 IST on 2026-03-17"))

    _titles(
        fig,
        title=(
            "Warm-pool governor: pre-warm within one sample of load, "
            "mitigating on the next"
        ),
        subtitle=_provenance(series),
        caption=(
            "Every point was replayed through governor.fsm.decide(). The "
            f"{len(changes)} ringed markers are its phase transitions; band labels give the "
            "ASG state it writes.\n"
            f"The export stops mid-decay at {series.end:%H:%M:%S}Z, a minute before the "
            "recorded scale-in, so that transition is outside this figure's evidence."
        ),
    )
    return _save(fig, path)


def _draw_bands_key(
    ax: Axes,
    cfg: Config,
    edges: tuple[float, ...],
    top: float,
    series: MetricSeries,
) -> None:
    """Label each band in place, so the figure needs no legend box."""
    knobs = {
        phase: (
            f"({state.desired_capacity}, {state.warm_pool_min_size}, "
            f"{state.max_group_prepared_capacity})"
        )
        for phase, state in cfg.states.items()
    }
    bounds = {
        Phase.COST_SAVING: f"< {cfg.scale_in_threshold:g} %",
        Phase.PRE_WARM: f"{cfg.scale_in_threshold:g}-{cfg.pre_warm_threshold:g} %",
        Phase.MITIGATE: f">= {cfg.pre_warm_threshold:g} %",
    }
    left = _naive(series.start) - timedelta(seconds=90)
    for index, (phase, lower, upper) in enumerate(
        zip(Phase, edges[:-1], edges[1:], strict=True), start=1
    ):
        ax.text(
            left,
            (lower + min(upper, top)) / 2,
            f"Phase {index} · {_phase_label(phase)}  "
            f"{bounds[phase]}  ->  {knobs[phase]}",
            fontsize=9,
            color=INK_SECONDARY,
            va="center",
            zorder=2,
        )


def _draw_transitions(ax: Axes, changes: Sequence[Decision], top: float) -> None:
    """Ring and label each phase change.

    Label placement follows the shape of the data instead of being hand-positioned
    per figure: a point in the upper half of the plot takes its label underneath, and
    a point in the lower half takes it on the far side from the steep segment -- which
    is above-left when the phase escalated and below-left when it relaxed.
    """
    order = {phase: index for index, phase in enumerate(Phase)}
    for decision in changes:
        escalating = order[decision.phase] > order[decision.previous_phase]
        if decision.cpu > top / 2:
            offset, ha, va = (0, -16), "center", "top"
        elif escalating:
            offset, ha, va = (-10, 8), "right", "bottom"
        else:
            offset, ha, va = (-10, -8), "right", "top"
        moment = _naive(decision.timestamp)
        ax.plot(
            [moment],
            [decision.cpu],
            marker="o",
            markersize=9,
            markerfacecolor=REGIME,
            markeredgecolor=SURFACE,
            markeredgewidth=2.0,
            zorder=4,
        )
        ax.annotate(
            f"{decision.timestamp:%H:%M}Z  {decision.cpu:.2f} %\n"
            f"-> {_phase_label(decision.phase)}",
            xy=(moment, decision.cpu),
            xytext=offset,
            textcoords="offset points",
            ha=ha,
            va=va,
            fontsize=9,
            color=INK,
            zorder=5,
        )


def _draw_export_gap(ax: Axes, series: MetricSeries, top: float) -> None:
    """Draw the absence of data as a thing you can see."""
    end = _naive(series.end)
    right = end + timedelta(minutes=6)
    ax.axvspan(end, right, color=GRID, alpha=0.8, linewidth=0, zorder=0)
    ax.axvline(end, color=INK_MUTED, linewidth=1.0, zorder=2)
    ax.annotate(
        f"export ends here\nno data past {series.end:%H:%M:%S}Z\nthe recorded scale-in"
        "\nat 11:48Z is not shown",
        xy=(end + timedelta(seconds=20), top - 3),
        va="top",
        fontsize=9,
        color=INK_SECONDARY,
        zorder=5,
    )


def _phase_label(phase: Phase) -> str:
    """`cost_saving` -> `cost-saving`: the enum value, hyphenated for prose."""
    return phase.value.replace("_", "-")


def _apply_style() -> None:
    """Pin every rendering parameter the figures depend on.

    Set on each render rather than at import: matplotlib's rcParams are global, and a
    module that quietly restyles the importing process is a bad neighbour.
    """
    plt.rcParams.update(
        {
            # DejaVu Sans ships with matplotlib, so the render does not depend on which
            # fonts the machine happens to have installed.
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "axes.edgecolor": GRID,
            "axes.labelcolor": INK_SECONDARY,
            "axes.labelsize": 10,
            "axes.grid": True,
            "axes.axisbelow": True,
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "grid.linestyle": "-",
            "text.color": INK,
            "xtick.color": INK_SECONDARY,
            "ytick.color": INK_SECONDARY,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "savefig.facecolor": SURFACE,
        }
    )


def _titles(fig: Figure, *, title: str, subtitle: str, caption: str) -> None:
    """Title, provenance line and footnote, all flush to the figure's left margin."""
    fig.text(_MARGIN, 0.955, title, fontsize=15, fontweight="bold", color=INK, va="top")
    fig.text(_MARGIN, 0.905, subtitle, fontsize=9.5, color=INK_MUTED, va="top")
    fig.text(
        _MARGIN,
        0.02,
        _wrap(caption, fig),
        fontsize=9,
        color=INK_SECONDARY,
        va="bottom",
        linespacing=1.5,
    )


def _wrap(caption: str, fig: Figure) -> str:
    """Hard-wrap the footnote to the figure width.

    matplotlib will happily draw text straight off the canvas, so the wrap width is
    derived from the figure rather than guessed: ~11.5 characters per inch is what
    9 pt DejaVu Sans measures at this dpi.
    """
    width = int(fig.get_figwidth() * 12.8)
    return "\n".join(
        textwrap.fill(paragraph, width=width) for paragraph in caption.split("\n")
    )


def _format_time_axis(ax: Axes, *, minutes: int) -> None:
    ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=range(0, 60, minutes)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)


def _provenance(series: MetricSeries) -> str:
    return (
        f"{series.start:%Y-%m-%d} · {series.region} · {series.instance_id} · "
        f"{series.metric} {series.statistic} / {series.period_seconds} s · "
        f"{len(series)} datapoints · source: data/{series.path.name}"
    )


def _utc_xlabel(series: MetricSeries, *, ist_hint: str) -> str:
    return f"Time (UTC), {series.start:%Y-%m-%d}    [local time was {ist_hint}]"


def _naive(moment: datetime) -> datetime:
    """Drop the tzinfo for plotting.

    Every timestamp reaching this module is already UTC, and matplotlib's date units
    would otherwise convert tz-aware values into whatever `rcParams['timezone']` says.
    Stripping it makes the axis mean exactly what its label claims.
    """
    return moment.replace(tzinfo=None)


def _mmss(seconds: float) -> str:
    minutes, remainder = divmod(int(round(seconds)), 60)
    return f"{minutes}m{remainder:02d}s" if minutes else f"{remainder}s"


def _save(fig: Figure, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=_DPI, metadata=_PNG_METADATA)
    plt.close(fig)
    return path
