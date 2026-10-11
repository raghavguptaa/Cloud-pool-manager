"""Replay the captured telemetry through the controller and check it against reality.

Run it with ``python -m sim.replay`` (or ``make sim``). No AWS credentials, no network.

What this does and why it is the centre of the repository
---------------------------------------------------------
Every sample in `data/governor_1min.csv` is fed to `governor.fsm.decide()` -- the same
pure function the deployed Lambda called -- and the resulting state timeline is diffed
against `data/governor_events.csv`, the timeline a human wrote down while watching the
console on 2026-03-17. That turns "the controller behaved like this" from a claim in a
write-up into an assertion that a test can fail.

Three rules the report follows without exception:

* **Nothing is smoothed, extended or interpolated.** The export is 26 samples; the
  replay is 26 decisions.
* **A gap is named, never skipped.** The retained export stops at 11:47:00Z and the
  recorded scale-in is at 11:48:00Z, so that row cannot be checked. It is reported as
  unverifiable rather than dropped from the table.
* **A disagreement is printed as loudly as an agreement.** One recorded row is not
  supported by the retained CSV; the report says so and says why.

The cross-arm latency comparison is deliberately hedged: the two arms ran on different
days on different instances at different metric resolutions, with n=1 each. The numbers
are still worth computing -- they are the reason the project exists -- but the report
states what is being compared before it states the result.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from governor.config import DEFAULT, SCALE_IN_15, Config, Phase, State
from governor.fsm import classify, decide
from sim import figures
from sim.telemetry import (
    BASELINE_METRICS,
    GOVERNOR_METRICS,
    RESULTS_DIR,
    BaselineEvent,
    MetricSeries,
    RecordedState,
    Sample,
    find_baseline_event,
    read_baseline_events,
    read_governor_states,
    read_metric_export,
)

__all__ = [
    "Decision",
    "LatencyComparison",
    "StateCheck",
    "Verdict",
    "build_report",
    "compare_latency",
    "main",
    "phase_runs",
    "reconcile",
    "replay",
    "threshold_ambiguity",
    "transitions",
]

# The recorded CPU figures were written down truncated to one decimal place
# (49.953...% was recorded as "49.90"), so a recorded value may sit up to 0.1
# below the exported one and still be the same observation.
CPU_TOLERANCE_PCT = 0.1

_PHASE_ORDINAL_RE = re.compile(r"\bPhase\s+([123])\b")
_PHASE_BY_ORDINAL = {
    1: Phase.COST_SAVING,
    2: Phase.PRE_WARM,
    3: Phase.MITIGATE,
}


@dataclass(frozen=True, slots=True)
class Decision:
    """One replayed poll: the sample that drove it and what the controller chose."""

    timestamp: datetime
    cpu: float
    phase: Phase
    state: State
    previous_phase: Phase | None
    """The phase of the preceding sample, or None for the first sample of the run."""

    @property
    def is_transition(self) -> bool:
        """True when this sample changed the phase.

        The first sample is not a transition: there is no earlier sample it could
        have differed from, and counting it as one would put a phantom event on the
        figure at the left edge of every export.
        """
        return self.previous_phase is not None and self.phase is not self.previous_phase


class Verdict(StrEnum):
    """Outcome of checking one recorded row against the replayed timeline."""

    AGREE = "agree"
    DISAGREE = "disagree"
    NOT_COVERED = "not covered"
    """The export has no sample inside the row's window, so the row is neither
    confirmed nor contradicted by the primary artifact."""


@dataclass(frozen=True, slots=True)
class StateCheck:
    """One recorded row, the decisions covering it, and whether they match."""

    row: RecordedState
    verdict: Verdict
    expected_phase: Phase | None
    covering: tuple[Decision, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LatencyComparison:
    """Time-to-mitigation for both arms, measured from the onset of load.

    The baseline arm is read straight off the hand-recorded event timeline: load
    started at a known instant, the alarm fired at a known instant, the warm instance
    reached InService at a known instant.

    The governor arm has to be reasoned about, because nobody wrote down the wall-clock
    moment its Lambda wrote `DesiredCapacity=2`. What the data does establish is that
    the 11:43:00Z bucket is the first to carry load and the 11:44:00Z bucket is the
    first to cross the mitigate threshold -- 60 s apart by timestamp. A one-minute
    bucket, though, is not readable until it closes, so the governor is charged one
    further `metric_period_s` for the 11:44 bucket to close and publish before its
    poll can act on it. That pessimistic reading is what `governor_mitigation_s`
    reports; the optimistic 60 s reading ignores publication delay and is not used.
    """

    spike: BaselineEvent
    alarm: BaselineEvent
    in_service: BaselineEvent
    load_onset: Decision
    mitigation: Decision
    metric_period_s: int

    @property
    def baseline_alarm_s(self) -> float:
        """Load onset to CloudWatch alarm, baseline arm."""
        return (self.alarm.time_utc - self.spike.time_utc).total_seconds()

    @property
    def baseline_mitigation_s(self) -> float:
        """Load onset to mitigating capacity InService, baseline arm."""
        return (self.in_service.time_utc - self.spike.time_utc).total_seconds()

    @property
    def baseline_extraction_s(self) -> float:
        """Alarm to InService -- the part of the baseline arm that was already fast."""
        return (self.in_service.time_utc - self.alarm.time_utc).total_seconds()

    @property
    def governor_detection_s(self) -> float:
        """First loaded sample to first mitigating sample, by metric timestamp."""
        return (self.mitigation.timestamp - self.load_onset.timestamp).total_seconds()

    @property
    def governor_mitigation_s(self) -> float:
        """Detection plus one metric period for the deciding bucket to publish."""
        return self.governor_detection_s + self.metric_period_s

    @property
    def saved_s(self) -> float:
        return self.baseline_mitigation_s - self.governor_mitigation_s

    @property
    def speedup(self) -> float:
        return self.baseline_mitigation_s / self.governor_mitigation_s


def replay(samples: Iterable[Sample], cfg: Config = DEFAULT) -> tuple[Decision, ...]:
    """Run every sample through `decide()` in timestamp order.

    The controller is memoryless, so the previous phase is carried purely to mark
    transitions in the output -- it is never fed back into the decision.
    """
    ordered = sorted(samples, key=lambda sample: sample.timestamp)
    decisions: list[Decision] = []
    previous: Phase | None = None
    for sample in ordered:
        phase, state = decide(sample.cpu, cfg)
        decisions.append(
            Decision(
                timestamp=sample.timestamp,
                cpu=sample.cpu,
                phase=phase,
                state=state,
                previous_phase=previous,
            )
        )
        previous = phase
    return tuple(decisions)


def transitions(decisions: Iterable[Decision]) -> tuple[Decision, ...]:
    """The decisions at which the phase changed."""
    return tuple(decision for decision in decisions if decision.is_transition)


def phase_runs(decisions: Iterable[Decision]) -> tuple[tuple[Phase, int], ...]:
    """The phase sequence, run-length encoded as `(phase, consecutive samples)`."""
    runs: list[list[Phase | int]] = []
    for decision in decisions:
        if runs and runs[-1][0] is decision.phase:
            runs[-1][1] += 1  # type: ignore[operator]
        else:
            runs.append([decision.phase, 1])
    return tuple((phase, count) for phase, count in runs)  # type: ignore[misc]


def reconcile(
    decisions: Sequence[Decision],
    rows: Iterable[RecordedState],
    cfg: Config = DEFAULT,
) -> tuple[StateCheck, ...]:
    """Diff the replayed timeline against the hand-recorded one.

    Joins on `time_utc_start`/`time_utc_end` -- the corrected UTC columns -- because
    the `time_ist_recorded` column carries a +06:00 offset mislabelled as IST. See
    `data/PROVENANCE.md`.
    """
    if not decisions:
        raise ValueError("cannot reconcile against an empty replay")
    first, last = decisions[0].timestamp, decisions[-1].timestamp
    return tuple(_check_row(decisions, row, cfg, first, last) for row in rows)


def compare_latency(
    governor: Sequence[Decision],
    baseline_events: Sequence[BaselineEvent],
    metric_period_s: int,
) -> LatencyComparison:
    """Build the cross-arm latency comparison from the two recorded arms.

    Raises:
        LookupError: the governor replay never leaves `COST_SAVING` or never reaches
            `MITIGATE`, or the baseline timeline is missing one of the three rows the
            comparison is built from.
    """
    load_onset = _first(governor, lambda d: d.phase is not Phase.COST_SAVING)
    if load_onset is None:
        raise LookupError("the replayed run never rises above the scale-in threshold")
    mitigation = _first(governor, lambda d: d.phase is Phase.MITIGATE)
    if mitigation is None:
        raise LookupError("the replayed run never reaches the mitigate threshold")
    return LatencyComparison(
        spike=find_baseline_event(baseline_events, "CPU spike"),
        alarm=find_baseline_event(baseline_events, "alarm triggered"),
        in_service=find_baseline_event(baseline_events, "InService"),
        load_onset=load_onset,
        mitigation=mitigation,
        metric_period_s=metric_period_s,
    )


def threshold_ambiguity(
    samples: Sequence[Sample],
    left: Config = DEFAULT,
    right: Config = SCALE_IN_15,
) -> tuple[bool, tuple[Sample, ...]]:
    """Ask whether this telemetry can tell two scale-in thresholds apart.

    The original design spec is ambiguous about the scale-in threshold: its formula
    says 20 %, its narrative and the recorded state timeline say "< 15.0 %". Only
    samples landing between the two candidates can distinguish them.

    Returns:
        `(timelines_identical, samples_in_the_disputed_band)`.
    """
    low = min(left.scale_in_threshold, right.scale_in_threshold)
    high = max(left.scale_in_threshold, right.scale_in_threshold)
    disputed = tuple(sample for sample in samples if low <= sample.cpu < high)
    identical = [d.phase for d in replay(samples, left)] == [
        d.phase for d in replay(samples, right)
    ]
    return identical, disputed


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------


def build_report(
    governor_series: MetricSeries,
    baseline_series: MetricSeries,
    recorded: Sequence[RecordedState],
    baseline_events: Sequence[BaselineEvent],
    cfg: Config = DEFAULT,
) -> str:
    """Render the whole replay as plain text. Pure -- writes nothing, plots nothing."""
    decisions = replay(governor_series.samples, cfg)
    checks = reconcile(decisions, recorded, cfg)
    latency = compare_latency(decisions, baseline_events, governor_series.period)
    identical, disputed = threshold_ambiguity(governor_series.samples)

    lines: list[str] = []
    lines += _section("Offline replay of the captured telemetry")
    lines += [
        "Every sample below was fed to governor.fsm.decide() -- the same pure function",
        "the deployed Lambda called. Nothing is smoothed, resampled or extended: the",
        "export holds 26 samples and the replay holds 26 decisions.",
        "",
        f"  governor arm  {governor_series.path.name}  ({len(governor_series)} samples)",
        f"                {governor_series.describe()}",
        f"                {_window(governor_series)}",
        f"  baseline arm  {baseline_series.path.name}  ({len(baseline_series)} samples)",
        f"                {baseline_series.describe()}",
        f"                {_window(baseline_series)}",
        "",
        f"  thresholds    scale-in {cfg.scale_in_threshold:g} %, "
        f"pre-warm {cfg.pre_warm_threshold:g} %",
    ]

    lines += _section("Replayed state timeline")
    lines += _timeline_table(decisions)

    lines += _section("Phase transitions")
    lines += _transition_lines(decisions)

    lines += _section("Replay vs the hand-recorded timeline")
    lines += _reconciliation_lines(checks)

    lines += _section("Time to mitigation: baseline arm vs governor arm")
    lines += _latency_lines(latency, governor_series, baseline_series)

    lines += _section("Can this data settle the scale-in threshold?")
    lines += _ambiguity_lines(identical, disputed)

    return "\n".join(lines) + "\n"


def main() -> None:
    """Print the replay report and regenerate the figures in `results/`."""
    governor_series = read_metric_export(GOVERNOR_METRICS)
    baseline_series = read_metric_export(BASELINE_METRICS)
    recorded = read_governor_states()
    baseline_events = read_baseline_events()

    print(build_report(governor_series, baseline_series, recorded, baseline_events))

    # build_report is pure and replays the export itself; redoing that here is 26
    # comparisons and keeps the report function free of output parameters.
    decisions = replay(governor_series.samples)
    latency = compare_latency(decisions, baseline_events, governor_series.period)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    written = (
        figures.render_baseline(
            baseline_series, latency, RESULTS_DIR / "fig1_baseline.png"
        ),
        figures.render_governor(
            governor_series, decisions, RESULTS_DIR / "fig2_governor.png"
        ),
    )
    print(_rule("Figures"))
    for path in written:
        print(f"  wrote {path.relative_to(path.parents[1])}")


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------


def _check_row(
    decisions: Sequence[Decision],
    row: RecordedState,
    cfg: Config,
    first: datetime,
    last: datetime,
) -> StateCheck:
    expected, derivation = _expected_phase(row, cfg)
    covering = tuple(
        d for d in decisions if row.time_utc_start <= d.timestamp <= row.window_end
    )
    notes = [derivation]

    if not covering:
        edge = "before the export begins" if row.window_end < first else "after the export ends"
        notes.append(
            f"no sample in the export covers {_clock(row.time_utc_start)}"
            f" -- the row falls {edge}"
            f" ({_clock(first)}-{_clock(last)})"
        )
        return StateCheck(row, Verdict.NOT_COVERED, expected, covering, tuple(notes))

    observed = {d.phase for d in covering}
    agrees = expected is not None and observed == {expected}
    notes.append(_observed_note(covering))
    notes.extend(_cpu_notes(row, covering))
    if not agrees:
        notes.append(
            "the retained export does not support this row; the disagreement between "
            "the export and the recorded timeline is documented in data/PROVENANCE.md"
        )
    return StateCheck(
        row,
        Verdict.AGREE if agrees else Verdict.DISAGREE,
        expected,
        covering,
        tuple(notes),
    )


def _expected_phase(row: RecordedState, cfg: Config) -> tuple[Phase | None, str]:
    """Work out which phase a recorded row claims the controller was in.

    Two independent readings are available and they are cross-checked against each
    other: the observer's own "Phase N" label, and the phase the recorded CPU figure
    would produce if run through `classify()`. Agreement between them is a small piece
    of evidence that the timeline was written down accurately.
    """
    labelled = _labelled_phase(row.orchestrator_action)
    derived = _phase_from_recorded_cpu(row, cfg)

    if labelled is not None and derived is not None:
        if labelled is derived:
            return labelled, (
                f"recorded as {labelled.value}; the recorded CPU "
                f"({row.max_cpu_text} %) classifies the same way"
            )
        return labelled, (
            f"recorded as {labelled.value}, but the recorded CPU ({row.max_cpu_text} %) "
            f"classifies as {derived.value} -- the row is internally inconsistent"
        )
    if labelled is not None:
        return labelled, f"recorded as {labelled.value}"
    if derived is not None:
        return derived, (
            f"no phase label on this row; {derived.value} derived from the recorded "
            f"CPU ({row.max_cpu_text} %)"
        )
    return None, "the row names no phase and its CPU cell does not determine one"


def _labelled_phase(action: str) -> Phase | None:
    match = _PHASE_ORDINAL_RE.search(action)
    return _PHASE_BY_ORDINAL[int(match[1])] if match else None


def _phase_from_recorded_cpu(row: RecordedState, cfg: Config) -> Phase | None:
    if row.cpu_value is not None:
        return classify(row.cpu_value, cfg)
    if row.cpu_upper_bound is not None:
        # A bound such as "<15.0" pins a phase only when every value it admits lands
        # in the same one. Checking the two extremes is enough: classify is monotone.
        floor = classify(0.0, cfg)
        ceiling = classify(math.nextafter(row.cpu_upper_bound, 0.0), cfg)
        return floor if floor is ceiling else None
    return None


def _observed_note(covering: Sequence[Decision]) -> str:
    phases = ", ".join(sorted({d.phase.value for d in covering}))
    span = (
        _clock(covering[0].timestamp)
        if len(covering) == 1
        else f"{_clock(covering[0].timestamp)}-{_clock(covering[-1].timestamp)}"
    )
    return f"replay over {span}: {phases} ({len(covering)} sample(s))"


def _cpu_notes(row: RecordedState, covering: Sequence[Decision]) -> list[str]:
    values = [d.cpu for d in covering]
    if row.cpu_upper_bound is not None:
        breaches = [d for d in covering if d.cpu >= row.cpu_upper_bound]
        if not breaches:
            return [f"every covered sample satisfies the recorded bound {row.max_cpu_text} %"]
        listed = ", ".join(f"{d.cpu:.2f} % at {_clock(d.timestamp)}" for d in breaches)
        return [f"recorded bound {row.max_cpu_text} % is contradicted by {listed}"]

    if row.cpu_value is None:
        return []

    if row.is_range:
        inside = min(values) <= row.cpu_value <= max(values)
        note = (
            f"recorded {row.cpu_value:.2f} % sits inside the observed range "
            f"{min(values):.2f}-{max(values):.2f} %"
            if inside
            else f"recorded {row.cpu_value:.2f} % is outside the observed range "
            f"{min(values):.2f}-{max(values):.2f} %"
        )
        peak = max(covering, key=lambda d: d.cpu)
        return [
            note,
            f"the window's actual maximum is {peak.cpu:.2f} % at {_clock(peak.timestamp)}"
            f" ({peak.phase.value})",
        ]

    delta = abs(covering[0].cpu - row.cpu_value)
    marker = "matches" if delta <= CPU_TOLERANCE_PCT else "differs from"
    return [
        f"recorded {row.cpu_value:.2f} % {marker} the exported "
        f"{covering[0].cpu:.4f} % (delta {delta:.4f}, tolerance "
        f"{CPU_TOLERANCE_PCT:g} for one-decimal truncation)"
    ]


def _timeline_table(decisions: Sequence[Decision]) -> list[str]:
    header = (
        f"  {'time (UTC)':<12}{'CPU %':>8}  {'phase':<12}"
        f"{'desired':>8}{'warm-min':>10}{'prepared':>10}  note"
    )
    rows = [header, "  " + "-" * 78]
    for index, d in enumerate(decisions):
        if index == 0:
            note = f"run starts in {d.phase.value}"
        elif d.is_transition:
            note = f"-> {d.phase.value}"
        else:
            note = ""
        rows.append(
            f"  {_clock(d.timestamp):<12}{d.cpu:>8.2f}  {d.phase.value:<12}"
            f"{d.state.desired_capacity:>8}{d.state.warm_pool_min_size:>10}"
            f"{d.state.max_group_prepared_capacity:>10}  {note}".rstrip()
        )
    rows.append(f"  {'':<52}{'':>10}  export ends here")
    return rows


def _transition_lines(decisions: Sequence[Decision]) -> list[str]:
    changes = transitions(decisions)
    lines = [
        f"  {len(changes)} transitions across {len(decisions)} samples; "
        f"the rest are holds.",
        "",
    ]
    for d in changes:
        lines.append(
            f"  {_clock(d.timestamp)}  {d.previous_phase.value:>11} -> "  # type: ignore[union-attr]
            f"{d.phase.value:<11} at {d.cpu:.4f} %  "
            f"-> (desired={d.state.desired_capacity}, "
            f"warm_min={d.state.warm_pool_min_size}, "
            f"prepared={d.state.max_group_prepared_capacity})"
        )
    lines += [
        "",
        "  Run-length encoded phase sequence:",
        "    " + " -> ".join(f"{phase.value} x{count}" for phase, count in phase_runs(decisions)),
    ]
    return lines


def _reconciliation_lines(checks: Sequence[StateCheck]) -> list[str]:
    tally = {verdict: sum(1 for c in checks if c.verdict is verdict) for verdict in Verdict}
    lines = [
        f"  {len(checks)} recorded rows: "
        + ", ".join(f"{count} {verdict.value}" for verdict, count in tally.items()),
        "",
    ]
    for check in checks:
        row = check.row
        window = _clock(row.time_utc_start)
        if row.is_range:
            window += f"-{_clock(row.window_end)}"
        lines.append(f"  [{check.verdict.value.upper():^11}] {window}  {row.system_state}")
        lines.append(f"{'':>16}recorded: {row.orchestrator_action}")
        for note in check.notes:
            lines.append(f"{'':>16}- {note}")
        lines.append("")
    lines.append(
        "  The rising edge of the run -- idle, pre-warm, mitigate -- is corroborated by"
    )
    lines.append(
        "  the export, the recorded timeline and the original figure alike. The falling"
    )
    lines.append(
        "  edge is not: the retained export stops at 11:47:00Z, one minute before the"
    )
    lines.append(
        "  recorded scale-in, and disagrees with the recorded CPU for the two minutes it"
    )
    lines.append(
        "  does cover. That half of the run is evidenced by the hand-recorded timeline"
    )
    lines.append("  only, and this replay does not claim otherwise.")
    return lines


def _latency_lines(
    latency: LatencyComparison,
    governor_series: MetricSeries,
    baseline_series: MetricSeries,
) -> list[str]:
    return [
        "  What is being compared: time from the onset of load to mitigating capacity",
        "  serving traffic. Read this before the numbers --",
        "",
        f"    - different days: baseline {baseline_series.start:%Y-%m-%d}, "
        f"governor {governor_series.start:%Y-%m-%d}",
        f"    - different instances: {baseline_series.instance_id} vs "
        f"{governor_series.instance_id}",
        f"    - different metric resolution: {baseline_series.statistic}/"
        f"{baseline_series.period_seconds} s vs {governor_series.statistic}/"
        f"{governor_series.period_seconds} s",
        "    - n = 1 per arm; wall-clock events were read off the console by hand (~1 s)",
        "    - the two changes are confounded: switching to detailed monitoring and",
        "      switching to a polled controller happened together, so the split between",
        "      them cannot be recovered from this data",
        "",
        "  Baseline arm (reactive step scaling, from the recorded event timeline):",
        f"    load onset            {_stamp(latency.spike.time_utc)}",
        f"    CloudWatch alarm      {_stamp(latency.alarm.time_utc)}"
        f"   +{_duration(latency.baseline_alarm_s)}",
        f"    instance InService    {_stamp(latency.in_service.time_utc)}"
        f"   +{_duration(latency.baseline_mitigation_s)}",
        "",
        "  Governor arm (replayed from the metric export):",
        f"    first loaded sample   {_stamp(latency.load_onset.timestamp)}"
        f"   {latency.load_onset.cpu:.4f} % -> {latency.load_onset.phase.value}",
        f"    first mitigating one  {_stamp(latency.mitigation.timestamp)}"
        f"   {latency.mitigation.cpu:.4f} % -> {latency.mitigation.phase.value}",
        f"    {'detection, by metric timestamp':<45}"
        f"{_duration(latency.governor_detection_s)}",
        f"    {f'+ one {latency.metric_period_s} s period for that bucket to publish':<45}"
        f"{_duration(latency.governor_mitigation_s)} to mitigation",
        "",
        f"  Result: {_duration(latency.baseline_mitigation_s)} baseline vs "
        f"{_duration(latency.governor_mitigation_s)} governor -- "
        f"{_duration(latency.saved_s)} earlier, {latency.speedup:.2f}x faster.",
        "",
        "  Worth noting where the time went. Once the baseline arm's alarm did fire, the",
        f"  warm pool had a running instance {_duration(latency.baseline_extraction_s)} later:"
        " capacity was never the bottleneck in either",
        "  arm -- detection was. The governor's whole advantage is that it reads a",
        f"  {governor_series.period_seconds} s {governor_series.statistic} every minute "
        f"instead of waiting for an alarm to evaluate a "
        f"{baseline_series.period_seconds} s {baseline_series.statistic}.",
    ]


def _ambiguity_lines(identical: bool, disputed: Sequence[Sample]) -> list[str]:
    band = f"[{SCALE_IN_15.scale_in_threshold:g} %, {DEFAULT.scale_in_threshold:g} %)"
    lines = [
        "  The original design spec gives the scale-in threshold two ways: 20 % in its",
        '  formula, "< 15.0 %" in the narrative of the run and in the recorded timeline.',
        "  Only a sample landing between the two candidates could tell them apart.",
        "",
        f"    samples in {band}: {len(disputed)}",
        f"    timelines identical under both thresholds: {'yes' if identical else 'no'}",
    ]
    if identical and not disputed:
        lines += [
            "",
            "  So this telemetry cannot settle it, and the repository does not pretend",
            "  to: governor.config exposes DEFAULT (20 %) and SCALE_IN_15 (15 %), and",
            "  neither is treated as the privileged reading.",
        ]
    else:
        lines += [
            "",
            "  The two thresholds are distinguishable on this data -- "
            f"{len(disputed)} sample(s) fall in the disputed band:",
            *(f"    {_stamp(s.timestamp)}  {s.cpu:.4f} %" for s in disputed),
        ]
    return lines


def _first(
    items: Iterable[Decision], predicate: Callable[[Decision], bool]
) -> Decision | None:
    return next((item for item in items if predicate(item)), None)


def _section(title: str) -> list[str]:
    return ["", _rule(title), ""]


def _rule(title: str) -> str:
    return f"== {title} " + "=" * max(0, 78 - len(title))


def _window(series: MetricSeries) -> str:
    return f"{_stamp(series.start)} - {_stamp(series.end)}"


def _stamp(moment: datetime) -> str:
    return f"{moment:%Y-%m-%d %H:%M:%S}Z"


def _clock(moment: datetime) -> str:
    return f"{moment:%H:%M:%S}Z"


def _duration(seconds: float) -> str:
    minutes, remainder = divmod(int(round(seconds)), 60)
    return f"{minutes}m{remainder:02d}s ({int(round(seconds))} s)" if minutes else f"{remainder} s"


if __name__ == "__main__":
    main()
