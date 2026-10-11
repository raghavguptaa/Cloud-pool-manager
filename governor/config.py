"""Configuration data for the Dynamic Governor.

Thresholds and the phase -> ASG-state table live here as *data*, never as literals
buried in a branch. Two things fall out of that:

* the controller can be re-tuned without editing control flow, and
* the ambiguity in the original design spec (below) is expressible as configuration
  rather than as an argument about which number is "right".

**The 20 % / 15 % ambiguity.** The original design spec is ambiguous about the
scale-in threshold. Its mathematical formulation uses 20 % for the cost-saving
transition, while the narrative describing the observed run -- and the recorded state
timeline in `data/governor_events.csv` -- both report scale-in at "< 15.0 %". Only one
of those can be what the deployed Lambda actually did, and the artifact that would
settle it (the function source) did not survive. Both values are therefore reachable:
`DEFAULT` carries 20.0, `SCALE_IN_15` carries 15.0, and nothing in the codebase treats
either as privileged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

__all__ = [
    "DEFAULT",
    "DEFAULT_STATES",
    "SCALE_IN_15",
    "Config",
    "Phase",
    "State",
]


class Phase(StrEnum):
    """The three mutually exclusive operational phases.

    `StrEnum` so a phase serialises straight into a JSON log line without a
    `.value` dance at every call site.
    """

    COST_SAVING = "cost_saving"
    """Below the scale-in threshold. Warm pool to zero -- stops paying for EBS
    volumes attached to stopped instances that no traffic justifies."""

    PRE_WARM = "pre_warm"
    """Between the thresholds. Boot instances into the warm pool in the background,
    ahead of a spike, without exposing them to traffic."""

    MITIGATE = "mitigate"
    """At or above the pre-warm threshold. Extract the already-booted instances from
    the warm pool into service, bypassing the cold-start boot entirely."""


@dataclass(frozen=True, slots=True)
class State:
    """The three Auto Scaling Group knobs the governor writes.

    Frozen because a decision is a value, not a mutable buffer: two `State`s compare
    equal iff they describe the same ASG configuration, which is exactly what the
    adapter's idempotency check needs.
    """

    desired_capacity: int
    warm_pool_min_size: int
    max_group_prepared_capacity: int

    def __post_init__(self) -> None:
        for name in ("desired_capacity", "warm_pool_min_size", "max_group_prepared_capacity"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be an int, got {type(value).__name__}")
            if value < 0:
                raise ValueError(f"{name} must be >= 0, got {value}")
        if self.max_group_prepared_capacity < self.desired_capacity:
            raise ValueError(
                "max_group_prepared_capacity must be >= desired_capacity "
                f"({self.max_group_prepared_capacity} < {self.desired_capacity}); "
                "AWS rejects a prepared ceiling below the active count"
            )


# The phase -> state table, transcribed from the original design's state vectors.
#
#   (desired_capacity, warm_pool_min_size, max_group_prepared_capacity)
#
# COST_SAVING (1, 0, 1)  one active instance, no warm pool, no headroom to prepare.
# PRE_WARM    (1, 2, 3)  still one active instance; two instances boot into the pool,
#                        ceiling of 3 = 1 active + 2 prepared.
# MITIGATE    (2, 0, 4)  a second instance goes into service. warm_pool_min_size drops
#                        to 0 *because* the pooled instances are being extracted, not
#                        because the pool is being abandoned -- the ceiling rises to 4
#                        so the ASG may re-prepare behind the newly active pair.
DEFAULT_STATES: Mapping[Phase, State] = MappingProxyType(
    {
        Phase.COST_SAVING: State(1, 0, 1),
        Phase.PRE_WARM: State(1, 2, 3),
        Phase.MITIGATE: State(2, 0, 4),
    }
)


@dataclass(frozen=True, slots=True)
class Config:
    """Tunables for `governor.fsm.decide`.

    `scale_in_threshold` and `pre_warm_threshold` are percentages of CPU utilisation
    and must satisfy `0 <= scale_in < pre_warm <= 100`. The gap between them is the
    dead-band that keeps the controller from flapping between phases on noise.
    """

    scale_in_threshold: float = 20.0
    pre_warm_threshold: float = 40.0
    states: Mapping[Phase, State] = field(default_factory=lambda: DEFAULT_STATES)

    def __post_init__(self) -> None:
        for name in ("scale_in_threshold", "pre_warm_threshold"):
            value = getattr(self, name)
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise TypeError(f"{name} must be a number, got {type(value).__name__}")
            if not 0.0 <= float(value) <= 100.0:
                raise ValueError(f"{name} must lie in [0, 100], got {value}")
        if self.scale_in_threshold >= self.pre_warm_threshold:
            raise ValueError(
                "scale_in_threshold must be strictly below pre_warm_threshold "
                f"({self.scale_in_threshold} >= {self.pre_warm_threshold}); "
                "an empty or inverted dead-band makes PRE_WARM unreachable"
            )
        missing = set(Phase) - set(self.states)
        if missing:
            raise ValueError(
                f"states is missing an entry for {sorted(p.value for p in missing)}; "
                "the table must be total over Phase or decide() could not be"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Build a `Config` from `SCALE_IN_THRESHOLD` / `PRE_WARM_THRESHOLD`.

        Unset variables fall back to the class defaults. Runtime concerns -- the ASG
        name, metric period, dry-run, log level -- are deliberately *not* read here:
        this object configures the pure decision, nothing else.
        """
        source = os.environ if env is None else env
        kwargs: dict[str, float] = {}
        for var, attr in (
            ("SCALE_IN_THRESHOLD", "scale_in_threshold"),
            ("PRE_WARM_THRESHOLD", "pre_warm_threshold"),
        ):
            raw = source.get(var)
            if raw is None or raw == "":
                continue
            try:
                kwargs[attr] = float(raw)
            except ValueError as exc:
                raise ValueError(f"{var}={raw!r} is not a number") from exc
        return cls(**kwargs)


DEFAULT = Config()
"""The formulation given in the original design spec: scale in below 20 %, pre-warm
from 20 % to 40 %, mitigate at 40 % and above."""

SCALE_IN_15 = Config(scale_in_threshold=15.0)
"""The alternative reading: the narrative of the observed run and the recorded state
timeline both report scale-in at "< 15.0 %". See the module docstring."""
