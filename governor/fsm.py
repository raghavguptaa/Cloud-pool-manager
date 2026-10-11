"""The Dynamic Governor's decision function.

This module is the contract the rest of the repository is built around, and it is
deliberately the most boring file here: one pure function, no I/O, no boto3, no clock,
no logging. The Lambda adapter, the offline replay and the tests all call *this* code
path, which is the only reason the replay in `sim/replay.py` can claim to reproduce
what the deployed controller did.

Why a finite state machine and not a forecaster
-----------------------------------------------
A model that predicts load needs training data, a retraining story, and an answer for
what happens when it is confidently wrong. A three-branch comparison needs none of
those. `decide()` runs in O(1) with no allocation beyond its return value, is total
over its declared domain, and is exhaustively testable by enumerating boundaries --
which `tests/test_fsm.py` does. For a controller that fires once a minute and can move
real money, "provably boring" beats "probably smarter".

On "hysteresis"
---------------
The original design calls the 20-40 % gap a hysteresis band. It is more precisely a
*dead-band*: `decide()` is memoryless, so the phase depends only on the current sample
and never on the previous phase. True hysteresis would need distinct rising and falling
thresholds, which requires feeding the previous phase back in. The dead-band still
suppresses flapping on small oscillations around a single threshold, so the practical
effect is close -- but the distinction is real and is called out in the README.
"""

from __future__ import annotations

import math

from governor.config import DEFAULT, Config, Phase, State

# Re-exported so callers can depend on one module for the whole contract:
#     from governor.fsm import decide, Phase, State, Config
__all__ = ["DEFAULT", "Config", "Phase", "State", "classify", "decide"]


def classify(cpu: float, cfg: Config = DEFAULT) -> Phase:
    """Map a CPU utilisation sample to its operational phase.

    The comparisons are ordered low-to-high and the bounds are half-open, so each
    sample lands in exactly one phase:

        cpu <  scale_in_threshold                        -> COST_SAVING
        scale_in_threshold <= cpu < pre_warm_threshold   -> PRE_WARM
        pre_warm_threshold <= cpu                        -> MITIGATE

    A sample exactly *on* a threshold escalates rather than relaxes -- 20.0 pre-warms
    and 40.0 mitigates. Under load, being one phase early costs a stopped instance;
    being one phase late costs a cold start in front of live traffic.
    """
    value = _validated(cpu)
    if value < cfg.scale_in_threshold:
        return Phase.COST_SAVING
    if value < cfg.pre_warm_threshold:
        return Phase.PRE_WARM
    return Phase.MITIGATE


def decide(cpu: float, cfg: Config = DEFAULT) -> tuple[Phase, State]:
    """Return the phase for `cpu` and the ASG state that phase demands.

    Pure: same input, same output, no side effects, nothing observed but its arguments.

    Args:
        cpu: Maximum `CPUUtilization` over the last period, as a percentage in
            `[0, 100]`.
        cfg: Thresholds and the phase -> state table. Defaults to `config.DEFAULT`.

    Returns:
        `(phase, state)` -- the phase entered and the three ASG knobs to write.

    Raises:
        TypeError: `cpu` is not a real number.
        ValueError: `cpu` is NaN or outside `[0, 100]`.
    """
    phase = classify(cpu, cfg)
    return phase, cfg.states[phase]


def _validated(cpu: float) -> float:
    """Reject inputs that cannot be a CPU percentage.

    NaN is rejected explicitly rather than tolerated. Every comparison against NaN is
    false, so an unguarded NaN would fall through both branches of `classify` and
    silently return MITIGATE -- scaling out on a metric gap, which is the most
    expensive possible response to "no data". Failing loudly hands that decision to the
    caller, where `governor/handler.py` can hold the previous state instead.
    """
    if isinstance(cpu, bool) or not isinstance(cpu, int | float):
        raise TypeError(f"cpu must be a real number, got {type(cpu).__name__}")
    value = float(cpu)
    if math.isnan(value):
        raise ValueError("cpu is NaN; refusing to guess a phase from a metric gap")
    if not 0.0 <= value <= 100.0:
        raise ValueError(f"cpu must be a percentage in [0, 100], got {value}")
    return value
