"""Boundary and totality tests for the pure decision function.

Deliberately dependency-free -- no boto3, no moto, no network, no fixtures beyond
pytest itself. If this file ever needs a mock, the FSM has stopped being pure.

The boundaries are the point. A threshold controller has exactly two places it can be
wrong by one sample, and they are `cpu == scale_in_threshold` and
`cpu == pre_warm_threshold`. Everything else is interpolation between them.
"""

from __future__ import annotations

import math

import pytest

from governor.config import DEFAULT, SCALE_IN_15, Config, Phase, State
from governor.fsm import classify, decide

# --------------------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------------------

BOUNDARY_CASES = [
    # (cpu, expected phase) under DEFAULT (20 / 40). Bounds are half-open upward:
    # a sample exactly on a threshold escalates.
    (0.0, Phase.COST_SAVING),
    (0.002, Phase.COST_SAVING),  # the idle level actually observed on 2026-03-17
    (19.99, Phase.COST_SAVING),
    (19.999_999_9, Phase.COST_SAVING),
    (20.0, Phase.PRE_WARM),  # on the threshold -> escalate
    (20.000_000_1, Phase.PRE_WARM),
    (29.99, Phase.PRE_WARM),
    (39.99, Phase.PRE_WARM),
    (39.999_999_9, Phase.PRE_WARM),
    (40.0, Phase.MITIGATE),  # on the threshold -> escalate
    (40.000_000_1, Phase.MITIGATE),
    (99.99, Phase.MITIGATE),
    (100.0, Phase.MITIGATE),
]


@pytest.mark.parametrize(("cpu", "expected"), BOUNDARY_CASES)
def test_phase_boundaries(cpu: float, expected: Phase) -> None:
    assert classify(cpu) is expected
    assert decide(cpu)[0] is expected


@pytest.mark.parametrize(("cpu", "expected"), BOUNDARY_CASES)
def test_decide_state_matches_table(cpu: float, expected: Phase) -> None:
    """`decide` must return exactly the table entry for the phase it chose.

    Guards against the state vector being re-derived (and drifting) at the call site.
    """
    phase, state = decide(cpu)
    assert state is DEFAULT.states[expected]
    assert phase is expected


def test_threshold_samples_escalate_not_relax() -> None:
    """Pin the half-open direction down on its own.

    Flipping `<` to `<=` in `classify` would leave most tests green while making the
    controller respond one sample late at exactly the moment load crosses a threshold.
    """
    assert classify(DEFAULT.scale_in_threshold) is Phase.PRE_WARM
    assert classify(DEFAULT.pre_warm_threshold) is Phase.MITIGATE
    assert classify(math.nextafter(DEFAULT.scale_in_threshold, 0.0)) is Phase.COST_SAVING
    assert classify(math.nextafter(DEFAULT.pre_warm_threshold, 0.0)) is Phase.PRE_WARM


# --------------------------------------------------------------------------------------
# The state table
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (Phase.COST_SAVING, State(1, 0, 1)),
        (Phase.PRE_WARM, State(1, 2, 3)),
        (Phase.MITIGATE, State(2, 0, 4)),
    ],
)
def test_state_table_matches_original_design(phase: Phase, expected: State) -> None:
    """The three state vectors from the original design, pinned.

    (desired_capacity, warm_pool_min_size, max_group_prepared_capacity)
    """
    assert DEFAULT.states[phase] == expected


def test_cost_saving_scales_the_warm_pool_to_zero() -> None:
    """The scale-to-zero property the whole cost argument rests on."""
    assert DEFAULT.states[Phase.COST_SAVING].warm_pool_min_size == 0


def test_mitigate_adds_an_active_instance() -> None:
    cost_saving = DEFAULT.states[Phase.COST_SAVING]
    mitigate = DEFAULT.states[Phase.MITIGATE]
    assert mitigate.desired_capacity > cost_saving.desired_capacity


def test_pre_warm_prepares_without_activating() -> None:
    """Pre-warming must not put instances in front of traffic -- that is the whole idea."""
    cost_saving = DEFAULT.states[Phase.COST_SAVING]
    pre_warm = DEFAULT.states[Phase.PRE_WARM]
    assert pre_warm.desired_capacity == cost_saving.desired_capacity
    assert pre_warm.warm_pool_min_size > 0


# --------------------------------------------------------------------------------------
# Totality and purity
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("cpu", [i / 4 for i in range(0, 401)])
def test_total_over_the_valid_domain(cpu: float) -> None:
    """Every percentage in [0, 100] at 0.25 granularity yields exactly one known phase."""
    phase, state = decide(cpu)
    assert phase in set(Phase)
    assert state is DEFAULT.states[phase]


def test_decide_is_deterministic() -> None:
    assert [decide(37.5) for _ in range(100)] == [decide(37.5)] * 100


def test_decide_does_not_mutate_config() -> None:
    cfg = Config()
    before = (cfg.scale_in_threshold, cfg.pre_warm_threshold, dict(cfg.states))
    for cpu in (0.0, 25.0, 75.0):
        decide(cpu, cfg)
    assert (cfg.scale_in_threshold, cfg.pre_warm_threshold, dict(cfg.states)) == before


def test_state_is_immutable() -> None:
    state = decide(50.0)[1]
    with pytest.raises((AttributeError, TypeError)):
        state.desired_capacity = 99  # type: ignore[misc]


def test_states_table_is_immutable() -> None:
    with pytest.raises(TypeError):
        DEFAULT.states[Phase.MITIGATE] = State(9, 9, 9)  # type: ignore[index]


# --------------------------------------------------------------------------------------
# Rejected input
# --------------------------------------------------------------------------------------


def test_nan_is_rejected_rather_than_silently_mitigating() -> None:
    """NaN must raise, not fall through to MITIGATE.

    Every comparison against NaN is false, so an unguarded NaN reaches the final
    `return Phase.MITIGATE` -- scaling out because a metric was *missing*. That is the
    most expensive possible reading of "no data", so it has to be impossible.
    """
    with pytest.raises(ValueError, match="NaN"):
        decide(float("nan"))


@pytest.mark.parametrize("cpu", [-0.001, -1.0, 100.001, 1e9, float("inf"), float("-inf")])
def test_out_of_range_is_rejected(cpu: float) -> None:
    with pytest.raises(ValueError, match=r"\[0, 100\]"):
        decide(cpu)


@pytest.mark.parametrize("cpu", ["50", None, True, [50.0], object()])
def test_non_numeric_is_rejected(cpu: object) -> None:
    with pytest.raises(TypeError, match="real number"):
        decide(cpu)  # type: ignore[arg-type]


def test_ints_are_accepted() -> None:
    """CloudWatch yields floats, but an int is still a valid percentage."""
    assert classify(50) is Phase.MITIGATE
    assert classify(0) is Phase.COST_SAVING


# --------------------------------------------------------------------------------------
# Configurability -- thresholds are data, not literals
# --------------------------------------------------------------------------------------


def test_scale_in_15_reaches_the_alternative_reading() -> None:
    """The original design spec is ambiguous: its formula says 20 %, its narrative of
    the observed run says "< 15.0 %". Both readings must be reachable by config alone,
    with no code change."""
    assert classify(17.5, DEFAULT) is Phase.COST_SAVING
    assert classify(17.5, SCALE_IN_15) is Phase.PRE_WARM
    assert classify(14.99, SCALE_IN_15) is Phase.COST_SAVING
    assert classify(15.0, SCALE_IN_15) is Phase.PRE_WARM


def test_custom_thresholds_shift_every_boundary() -> None:
    cfg = Config(scale_in_threshold=5.0, pre_warm_threshold=80.0)
    assert classify(4.99, cfg) is Phase.COST_SAVING
    assert classify(5.0, cfg) is Phase.PRE_WARM
    assert classify(79.99, cfg) is Phase.PRE_WARM
    assert classify(80.0, cfg) is Phase.MITIGATE


def test_custom_state_table_is_honoured() -> None:
    custom = dict(DEFAULT.states) | {Phase.MITIGATE: State(5, 0, 9)}
    cfg = Config(states=custom)
    assert decide(90.0, cfg)[1] == State(5, 0, 9)


@pytest.mark.parametrize(
    ("scale_in", "pre_warm"),
    [(40.0, 20.0), (20.0, 20.0), (-1.0, 40.0), (20.0, 101.0), (100.0, 100.0)],
)
def test_invalid_threshold_pairs_are_rejected(scale_in: float, pre_warm: float) -> None:
    with pytest.raises(ValueError):
        Config(scale_in_threshold=scale_in, pre_warm_threshold=pre_warm)


def test_incomplete_state_table_is_rejected() -> None:
    """A partial table would make `decide` partial -- a KeyError at 3 a.m. instead of
    a ValueError at construction."""
    with pytest.raises(ValueError, match="missing an entry"):
        Config(states={Phase.COST_SAVING: State(1, 0, 1)})


def test_config_from_env_reads_both_thresholds() -> None:
    cfg = Config.from_env({"SCALE_IN_THRESHOLD": "15", "PRE_WARM_THRESHOLD": "55.5"})
    assert (cfg.scale_in_threshold, cfg.pre_warm_threshold) == (15.0, 55.5)


def test_config_from_env_falls_back_to_defaults() -> None:
    cfg = Config.from_env({})
    assert (cfg.scale_in_threshold, cfg.pre_warm_threshold) == (20.0, 40.0)
    assert Config.from_env({"SCALE_IN_THRESHOLD": ""}).scale_in_threshold == 20.0


def test_config_from_env_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="SCALE_IN_THRESHOLD"):
        Config.from_env({"SCALE_IN_THRESHOLD": "twenty"})


def test_config_from_env_ignores_runtime_variables() -> None:
    """`Config` configures the decision only. ASG name, dry-run and friends belong to
    the handler, and leaking them in here would couple the pure core to the runtime."""
    cfg = Config.from_env({"ASG_NAME": "x", "DRY_RUN": "true", "METRIC_PERIOD": "60"})
    assert cfg == Config()


# --------------------------------------------------------------------------------------
# The transitions actually observed on AWS
# --------------------------------------------------------------------------------------


def test_reproduces_the_observed_transitions() -> None:
    """The three CPU samples that drove the observed run on 2026-03-17.

    Values are the exact floats from `data/governor_1min.csv`. `tests/test_replay.py`
    checks the whole series; this pins the three that mattered, right next to the FSM.
    """
    assert classify(0.274_999_999_999_999_97) is Phase.COST_SAVING  # idle, 11:22Z
    assert classify(29.907_834_869_418_842) is Phase.PRE_WARM  # spike detected, 11:43Z
    assert classify(50.123_226_537_001_266) is Phase.MITIGATE  # mitigation, 11:44Z
