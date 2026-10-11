"""The telemetry replay, pinned against what was actually observed on AWS.

`tests/test_fsm.py` proves the controller is internally consistent. This file proves it
is the *same* controller: every assertion below is a fact about the run captured on
2026-03-17 in `data/governor_1min.csv`, re-derived by feeding that export back through
`governor.fsm.decide()`. If someone changes a threshold, a state vector or a comparison
operator, the timeline stops matching the observed result and these tests fail -- which
is the whole point of keeping the telemetry in the repository.

Two habits worth naming, because they are what makes the suite worth trusting:

* **Expected values are the raw ones.** `29.907834869418842`, not `29.91`. A rounded
  expectation would still pass if the reader silently reformatted its input.
* **The gap is a test, not an omission.** The export stops at 11:47:00Z and the
  recorded scale-in is at 11:48:00Z. `test_recorded_scale_in_is_a_known_gap` asserts
  that the replay reports it as unverifiable; if a future change quietly started
  "covering" that row, the suite would fail rather than accept the new evidence.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from governor.config import DEFAULT, SCALE_IN_15, Phase, State
from sim import figures
from sim.replay import (
    Verdict,
    build_report,
    compare_latency,
    phase_runs,
    reconcile,
    replay,
    threshold_ambiguity,
    transitions,
)
from sim.telemetry import (
    BASELINE_METRICS,
    GOVERNOR_METRICS,
    find_baseline_event,
    read_baseline_events,
    read_governor_states,
    read_metric_export,
)

LOAD_START = datetime(2026, 3, 17, 11, 43, tzinfo=UTC)
MITIGATION = datetime(2026, 3, 17, 11, 44, tzinfo=UTC)
EXPORT_END = datetime(2026, 3, 17, 11, 47, tzinfo=UTC)
RECORDED_SCALE_IN = datetime(2026, 3, 17, 11, 48, tzinfo=UTC)

# The exported values, to the digit. See the header of this module.
CPU_AT_LOAD_START = 29.907834869418842
CPU_AT_MITIGATION = 50.123226537001266


@pytest.fixture(scope="module")
def governor_series():
    return read_metric_export(GOVERNOR_METRICS)


@pytest.fixture(scope="module")
def baseline_series():
    return read_metric_export(BASELINE_METRICS)


@pytest.fixture(scope="module")
def decisions(governor_series):
    return replay(governor_series.samples)


@pytest.fixture(scope="module")
def latency(decisions, governor_series):
    return compare_latency(decisions, read_baseline_events(), governor_series.period_seconds)


# --------------------------------------------------------------------------------------
# Parsing the exports
# --------------------------------------------------------------------------------------


def test_export_header_is_detected_not_counted(tmp_path: Path) -> None:
    """An extra header line must not cost a datapoint.

    The console's header is five lines today. Skipping "five lines" would silently eat
    a sample the day AWS adds a sixth, and the loss would look like data rather than a
    bug -- so the reader looks for the first parseable timestamp instead.
    """
    export = tmp_path / "extra_header.csv"
    export.write_text(
        "Id,m21\n"
        "StatusCode,Complete\n"
        "Messages,\n"
        "Something New,whatever the console adds next\n"
        "Full label,ap-south-1:AWS/EC2 InstanceId:i-abc CPUUtilization Maximum 60\n"
        "Label,CPUUtilization\n"
        "2026/03/17 11:22:00,0.275\n"
        "2026/03/17 11:23:00,1.5\n",
        encoding="utf-8",
    )
    series = read_metric_export(export)
    assert len(series) == 2
    assert series.samples[0].cpu == 0.275
    assert series.instance_id == "i-abc"
    assert series.statistic == "Maximum"
    assert series.period_seconds == 60


def test_export_reader_rejects_a_metric_gap(tmp_path: Path) -> None:
    """A blank value is a gap, and a gap is not a number to be guessed at."""
    export = tmp_path / "gap.csv"
    export.write_text(
        "Label,CPUUtilization\n2026/03/17 11:22:00,0.275\n2026/03/17 11:23:00,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="metric gap"):
        read_metric_export(export)


def test_export_reader_rejects_duplicate_timestamps(tmp_path: Path) -> None:
    export = tmp_path / "dupe.csv"
    export.write_text(
        "Label,CPUUtilization\n2026/03/17 11:22:00,0.275\n2026/03/17 11:22:00,0.3\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate timestamp"):
        read_metric_export(export)


def test_governor_export_provenance(governor_series) -> None:
    """The captured export is the high-resolution series, on the instance it claims."""
    assert len(governor_series) == 26
    assert governor_series.instance_id == "i-0da018dd62e55fada"
    assert (governor_series.statistic, governor_series.period_seconds) == ("Maximum", 60)
    assert governor_series.start == datetime(2026, 3, 17, 11, 22, tzinfo=UTC)
    assert governor_series.end == EXPORT_END


def test_baseline_export_is_three_points_of_five_minute_average(baseline_series) -> None:
    """Three datapoints is the baseline arm's defining property, not a truncation."""
    assert len(baseline_series) == 3
    assert (baseline_series.statistic, baseline_series.period_seconds) == ("Average", 300)
    assert baseline_series.peak.cpu == pytest.approx(48.30297762606763)


# --------------------------------------------------------------------------------------
# The replayed timeline
# --------------------------------------------------------------------------------------


def test_replay_covers_every_sample_once(decisions, governor_series) -> None:
    """No smoothing, no resampling, no interpolation: 26 samples, 26 decisions."""
    assert len(decisions) == len(governor_series) == 26
    assert [d.timestamp for d in decisions] == [s.timestamp for s in governor_series]
    assert [d.cpu for d in decisions] == [s.cpu for s in governor_series]


def test_idle_run_is_cost_saving_with_the_warm_pool_at_zero(decisions) -> None:
    """Twenty-one minutes of idle, and the controller never asks for a warm instance."""
    idle = [d for d in decisions if d.timestamp < LOAD_START]
    assert len(idle) == 21
    assert {d.phase for d in idle} == {Phase.COST_SAVING}
    assert {d.state for d in idle} == {State(1, 0, 1)}
    # Including the 1.30 % blip at 11:30Z -- comfortably inside the dead-band, so it
    # must not have provoked a pre-warm.
    assert max(d.cpu for d in idle) == pytest.approx(1.2999783336944384)


def test_pre_warm_is_entered_at_the_first_loaded_sample(decisions) -> None:
    """11:43:00Z, 29.9078 % -- the first sample of the load test.

    This is the observed result the controller has to reproduce: one sample after load
    begins, the warm pool is told to hold two instances.
    """
    entry = next(d for d in decisions if d.phase is Phase.PRE_WARM)
    assert entry.timestamp == LOAD_START
    assert entry.cpu == CPU_AT_LOAD_START
    assert entry.is_transition
    assert entry.previous_phase is Phase.COST_SAVING
    assert entry.state == State(1, 2, 3)


def test_mitigation_is_entered_one_sample_later(decisions) -> None:
    """11:44:00Z, 50.1232 % -- 60 s after load starts, and a second instance goes live."""
    entry = next(d for d in decisions if d.phase is Phase.MITIGATE)
    assert entry.timestamp == MITIGATION
    assert entry.cpu == CPU_AT_MITIGATION
    assert entry.previous_phase is Phase.PRE_WARM
    assert entry.state == State(2, 0, 4)
    assert (entry.timestamp - LOAD_START).total_seconds() == 60


def test_phase_sequence_over_the_whole_export(decisions) -> None:
    """The observed run, pinned sample by sample.

    Any change to a threshold or to a comparison operator shows up here as a different
    list, which is exactly the drift this file exists to catch.
    """
    assert [d.phase for d in decisions] == (
        [Phase.COST_SAVING] * 21
        + [Phase.PRE_WARM]
        + [Phase.MITIGATE] * 3
        + [Phase.PRE_WARM]
    )
    assert phase_runs(decisions) == (
        (Phase.COST_SAVING, 21),
        (Phase.PRE_WARM, 1),
        (Phase.MITIGATE, 3),
        (Phase.PRE_WARM, 1),
    )


def test_three_transitions_and_the_rest_are_holds(decisions) -> None:
    """The first sample is a start, not a transition -- there is nothing before it."""
    changes = transitions(decisions)
    assert [(d.timestamp, d.previous_phase, d.phase) for d in changes] == [
        (LOAD_START, Phase.COST_SAVING, Phase.PRE_WARM),
        (MITIGATION, Phase.PRE_WARM, Phase.MITIGATE),
        (EXPORT_END, Phase.MITIGATE, Phase.PRE_WARM),
    ]
    assert not decisions[0].is_transition
    assert decisions[0].previous_phase is None


def test_replay_is_order_independent(governor_series) -> None:
    """Shuffled input, same timeline: the replay sorts, it does not trust file order."""
    reversed_samples = list(reversed(governor_series.samples))
    assert replay(reversed_samples) == replay(governor_series.samples)


# --------------------------------------------------------------------------------------
# Replay vs the hand-recorded timeline
# --------------------------------------------------------------------------------------


def test_recorded_rows_join_on_the_corrected_utc_columns() -> None:
    """The recorded clock label is UTC+06:00 mislabelled as IST; the join ignores it.

    `data/PROVENANCE.md` has the evidence -- under +06:00 all three overlapping rows
    match the export's CPU values, and under true IST none of them land in the window
    at all.
    """
    rows = read_governor_states()
    load_row = next(row for row in rows if row.time_utc_start == LOAD_START)
    assert load_row.time_ist_recorded == "05:43 PM"  # 17:43 local == 11:43Z + 6h
    assert load_row.cpu_value == pytest.approx(29.90)


def test_recorded_timeline_agrees_on_the_rising_edge(decisions) -> None:
    """Idle, pre-warm, mitigate and the post-spike hold all reconcile."""
    checks = reconcile(decisions, read_governor_states())
    agreed = {c.row.time_utc_start: c for c in checks if c.verdict is Verdict.AGREE}
    assert len(agreed) == 4
    assert agreed[LOAD_START].expected_phase is Phase.PRE_WARM
    assert agreed[MITIGATION].expected_phase is Phase.MITIGATE


def test_recorded_falling_edge_is_reported_as_a_disagreement(decisions) -> None:
    """The one row the retained export contradicts is flagged, not smoothed over.

    The observer recorded "< 15.0 %" for 11:46-11:47Z; the export holds 49.95 % and
    29.39 % for those minutes. The replay reports the conflict and points at the
    provenance note rather than picking a winner.
    """
    checks = reconcile(decisions, read_governor_states())
    disagreements = [c for c in checks if c.verdict is Verdict.DISAGREE]
    assert len(disagreements) == 1
    (check,) = disagreements
    assert check.row.time_utc_start == datetime(2026, 3, 17, 11, 46, tzinfo=UTC)
    assert check.row.cpu_upper_bound == 15.0
    assert {d.phase for d in check.covering} == {Phase.MITIGATE, Phase.PRE_WARM}
    assert any("PROVENANCE" in note for note in check.notes)


def test_recorded_scale_in_is_a_known_gap(decisions) -> None:
    """The export ends one minute before the scale-in it is supposed to evidence.

    That row is genuinely unverifiable from the primary artifact, so the replay must
    say so. Asserting it here means the gap cannot be closed by accident -- only by
    data that does not exist.
    """
    checks = reconcile(decisions, read_governor_states())
    uncovered = [c for c in checks if c.verdict is Verdict.NOT_COVERED]
    assert [c.row.time_utc_start for c in uncovered] == [
        RECORDED_SCALE_IN,
        datetime(2026, 3, 17, 11, 50, tzinfo=UTC),
    ]

    scale_in = uncovered[0]
    assert scale_in.row.system_state == "Cost scale-in"
    assert scale_in.expected_phase is Phase.COST_SAVING
    assert scale_in.covering == ()
    assert any("after the export ends" in note for note in scale_in.notes)
    assert RECORDED_SCALE_IN > EXPORT_END


# --------------------------------------------------------------------------------------
# Cross-arm latency
# --------------------------------------------------------------------------------------


def test_baseline_alarm_latency_is_four_minutes_thirty_six(latency) -> None:
    assert latency.baseline_alarm_s == 276.0
    assert latency.baseline_mitigation_s == 283.0
    # Once it fired, the warm pool did its job in seconds. Detection was the cost.
    assert latency.baseline_extraction_s == 7.0


def test_governor_reaches_mitigation_within_two_minutes(latency) -> None:
    """60 s by metric timestamp, 120 s once the deciding bucket has to publish.

    The larger figure is the one the report quotes: charging the controller a full
    metric period it may not have needed keeps the comparison honest in the direction
    that works against the result being claimed.
    """
    assert latency.governor_detection_s == 60.0
    assert latency.governor_mitigation_s == 120.0
    assert latency.load_onset.timestamp == LOAD_START
    assert latency.mitigation.timestamp == MITIGATION


def test_the_delta_between_the_arms(latency) -> None:
    assert latency.saved_s == 163.0
    assert latency.speedup == pytest.approx(2.358, abs=0.001)


def test_the_two_arms_are_not_paired(governor_series, baseline_series) -> None:
    """The comparison is across different days, instances and metric resolutions.

    Asserted rather than merely written down, so the caveat cannot quietly stop being
    true if the data files are ever swapped.
    """
    assert governor_series.instance_id != baseline_series.instance_id
    assert governor_series.start.date() != baseline_series.start.date()
    assert governor_series.period_seconds != baseline_series.period_seconds
    assert governor_series.statistic != baseline_series.statistic


def test_baseline_event_lookup_refuses_an_ambiguous_match() -> None:
    """The latency figures are differences between named rows; a near-match is a bug."""
    events = read_baseline_events()
    assert find_baseline_event(events, "alarm triggered").time_utc == datetime(
        2026, 3, 16, 19, 4, 36, tzinfo=UTC
    )
    with pytest.raises(LookupError):
        find_baseline_event(events, "load test")  # matches two rows


# --------------------------------------------------------------------------------------
# The 20 % / 15 % ambiguity
# --------------------------------------------------------------------------------------


def test_this_telemetry_cannot_settle_the_scale_in_threshold(governor_series) -> None:
    """No retained sample lands between 15 % and 20 %, so both readings replay alike.

    A useful negative result: the original design spec is ambiguous about the scale-in
    threshold, and the captured run does not disambiguate it. Both values stay
    reachable in `governor.config` and neither is treated as the privileged one.
    """
    identical, disputed = threshold_ambiguity(governor_series.samples)
    assert disputed == ()
    assert identical
    assert [d.phase for d in replay(governor_series.samples, SCALE_IN_15)] == [
        d.phase for d in replay(governor_series.samples, DEFAULT)
    ]


# --------------------------------------------------------------------------------------
# Report and figures
# --------------------------------------------------------------------------------------


def test_report_names_the_gap_and_the_disagreement(
    governor_series, baseline_series
) -> None:
    """The printed report has to surface the weak evidence, not bury it."""
    report = build_report(
        governor_series, baseline_series, read_governor_states(), read_baseline_events()
    )
    assert "DISAGREE" in report
    assert "NOT COVERED" in report
    assert "i-0da018dd62e55fada" in report
    assert "4m36s" in report


def test_both_figures_render_headless(
    governor_series, baseline_series, decisions, latency, tmp_path: Path
) -> None:
    """CI has no display; `matplotlib.use("Agg")` in sim.figures is what makes this pass."""
    baseline = figures.render_baseline(baseline_series, latency, tmp_path / "fig1.png")
    governor = figures.render_governor(governor_series, decisions, tmp_path / "fig2.png")
    for path in (baseline, governor):
        content = path.read_bytes()
        assert content.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(content) > 20_000  # a blank canvas is far smaller than this


def test_figure_rendering_is_deterministic(
    governor_series, decisions, tmp_path: Path
) -> None:
    """Re-running `make sim` must not produce a byte-different PNG.

    Only two renders from this same process are compared. Pinning the bytes of the
    committed file instead would tie the suite to one matplotlib build, which is a
    promise this repository cannot keep across CI images.
    """
    first = figures.render_governor(governor_series, decisions, tmp_path / "a.png")
    second = figures.render_governor(governor_series, decisions, tmp_path / "b.png")
    assert hashlib.sha256(first.read_bytes()).hexdigest() == (
        hashlib.sha256(second.read_bytes()).hexdigest()
    )
