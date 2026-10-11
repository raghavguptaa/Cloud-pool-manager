"""The boto3 layer between the pure decision function and EC2 Auto Scaling.

`governor/fsm.py` decides. This module is everything that decision has to survive on the
way to a real API: reading the metric, comparing the decision against what the group is
actually configured with, writing only the knobs that differ, and retrying the failures
that are worth retrying.

Three pieces live here:

* `ScalingBackend` -- the protocol. Three operations: read the CPU, read the current
  configuration, write a new one. Anything that can do those three can drive the
  controller, which is what keeps `fsm.py` free of boto3 and leaves room for the
  LocalStack or GCP backend the original design's future-work section anticipates.
* `AwsScalingBackend` -- the implementation against CloudWatch and Auto Scaling.
* `run_once` -- the control loop. It is written against the protocol, not against boto3,
  so the same loop that runs in Lambda runs in the tests with no AWS at all.

Why idempotency is the interesting part
---------------------------------------
The function fires once a minute, and the overwhelmingly common case is that nothing has
changed: CPU is still idle, the group is already in the cost-saving configuration, and
the correct action is to do nothing. A controller that writes its decision unconditionally
issues ~1440 mutating API calls per group per day to re-assert a configuration that was
already correct -- burning Auto Scaling's request quota, filling CloudTrail with noise
that hides real changes, and risking a scaling activity every time. So `run_once` reads
the current configuration, builds the `State` it represents, and if that equals the state
it computed, it writes nothing and says so. In steady state this adapter makes two
read calls and zero writes.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from governor import __version__
from governor.config import DEFAULT, Config, Phase, State
from governor.fsm import decide

__all__ = [
    "DEFAULT_PERIOD_SECONDS",
    "AwsScalingBackend",
    "Decision",
    "MissingAutoScalingGroupError",
    "Outcome",
    "ScalingBackend",
    "run_once",
]

LOGGER = logging.getLogger(__name__)

DEFAULT_PERIOD_SECONDS = 60
"""Matches the 1-minute high-resolution series the controller was built against."""

CPU_NAMESPACE = "AWS/EC2"
CPU_METRIC_NAME = "CPUUtilization"
CPU_STATISTIC = "Maximum"
WARM_POOL_STATE = "Stopped"

_CPU_QUERY_ID = "cpu"

# How far back `read_cpu` looks, in whole periods. EC2 metrics are published with a lag of
# up to a couple of minutes, so a window of exactly one period frequently lands on a gap
# that is a publication delay rather than an outage. Five periods is wide enough to ride
# out that lag and narrow enough that the newest sample in it is still a fair description
# of "now" -- the query asks for the newest datapoint, not an average over the window.
_LOOKBACK_PERIODS = 5

_MAX_ATTEMPTS = 3
_BASE_DELAY_SECONDS = 1.0

# Retried: the server is telling us to come back later. Everything else -- above all the
# 400-class validation and authorization errors -- is a bug in the request or the IAM
# policy, and retrying a 400 is just a slower 400.
_RETRYABLE_ERROR_CODES = frozenset(
    {
        "InternalError",
        "InternalFailure",
        "RequestLimitExceeded",
        "RequestThrottled",
        "RequestThrottledException",
        "ServiceUnavailable",
        "ThrottledException",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
    }
)

_STATE_FIELDS = ("desired_capacity", "warm_pool_min_size", "max_group_prepared_capacity")


class MissingAutoScalingGroupError(LookupError):
    """`ASG_NAME` names a group that does not exist.

    `DescribeAutoScalingGroups` answers a query for an unknown group with an empty list
    rather than an error, so without this the controller would quietly treat a typo'd
    group name as "nothing to reconcile" and report success forever.
    """


class Outcome(StrEnum):
    """What the controller did with the decision it reached."""

    APPLIED = "applied"
    """The group differed from the decision and was written."""

    SKIPPED_IDEMPOTENT = "skipped_idempotent"
    """The group was already in the decided state. The steady-state outcome."""

    SKIPPED_DRY_RUN = "skipped_dry_run"
    """A write was needed but `DRY_RUN` suppressed it."""

    HELD_NO_DATA = "held_no_data"
    """The metric query returned no datapoints; the current configuration was held."""


@dataclass(frozen=True, slots=True)
class Decision:
    """One invocation's outcome, in the shape it is both logged and returned.

    The log line and the handler's return value are rendered from the same object on
    purpose: what an operator reads in CloudWatch Logs Insights is exactly what a test
    invoke hands back, so there is no second serialisation to drift.
    """

    asg_name: str
    cpu: float | None
    phase: Phase | None
    state: State | None
    previous_phase: Phase | None
    previous_state: State | None
    outcome: Outcome
    dry_run: bool

    @property
    def transition(self) -> str:
        """`"<from>-><to>"`, e.g. `"pre_warm->mitigate"`.

        `unknown` on the left means the group's configuration matched no phase in the
        table -- a fresh group with no warm pool, or drift from a manual change. `hold`
        on the right means no phase was chosen because there was no metric to choose on.
        """
        source = self.previous_phase.value if self.previous_phase else "unknown"
        target = self.phase.value if self.phase else "hold"
        return f"{source}->{target}"

    def to_dict(self) -> dict[str, Any]:
        """Render to JSON-serialisable primitives.

        The decided state's three knobs are flattened to top-level keys so a Logs Insights
        query can `stats max(desired_capacity)` without unnesting, and they are always
        present (null when no decision was reached) so no query has to cope with a field
        that sometimes does not exist. The previous state stays nested -- it is diagnostic
        context, not something anyone aggregates on.
        """
        return {
            "asg_name": self.asg_name,
            "cpu": self.cpu,
            "phase": self.phase.value if self.phase else None,
            "transition": self.transition,
            "outcome": self.outcome.value,
            "dry_run": self.dry_run,
            **_state_fields(self.state),
            "previous_state": _state_fields(self.previous_state),
        }

    def log_line(self) -> str:
        """One JSON object on one line -- the unit CloudWatch Logs Insights parses."""
        return json.dumps({"event": "governor.decision", **self.to_dict()})


@runtime_checkable
class ScalingBackend(Protocol):
    """What `run_once` needs from a scaling platform.

    Deliberately three operations wide. The seam exists so a LocalStack, a stub, or a
    future GCP managed-instance-group backend can drive the same control loop; it does not
    exist to abstract over cloud providers in general, and it stops at the smallest set of
    operations that makes the loop work.

    `asg_name` is the scaling target's identifier. It is named for the AWS resource
    because that is what an operator greps the logs for; a non-AWS backend maps its own
    group identifier onto it.
    """

    asg_name: str

    def read_cpu(self, period_seconds: int) -> float | None:
        """Newest CPU percentage, or `None` if the metric returned no datapoints."""
        ...

    def read_state(self) -> State | None:
        """Current configuration, or `None` if it does not correspond to a known state."""
        ...

    def write_state(self, state: State, previous: State | None = None) -> None:
        """Move the target to `state`, given the `previous` state read this cycle."""
        ...


# Region and credentials come from the Lambda execution environment.
#
# botocore's own retry policy is switched off (`max_attempts: 1`) so this adapter has
# exactly one retry policy -- the one in `_call` below, which is the one the tests
# exercise. Two layers of retry silently multiply: a 3-attempt policy under a 3-attempt
# policy is 9 calls and 9x the backoff, which on a 60 s cadence is how an invocation
# overruns into the next one.
_BOTO_CONFIG = BotoConfig(
    retries={"mode": "standard", "max_attempts": 1},
    connect_timeout=3,
    read_timeout=5,
    user_agent_extra=f"cloud-pool-manager/{__version__}",
)


@lru_cache(maxsize=1)
def _clients() -> tuple[Any, Any]:
    """The `(cloudwatch, autoscaling)` client pair, built once per execution environment.

    Built on first use rather than at import time, for two reasons: importing this module
    then costs no region lookup or credential resolution (which is what lets the tests and
    the offline tooling import it with no AWS configuration at all), and Lambda keeps the
    result for the life of the container, so only the first invocation after a cold start
    pays for client construction.
    """
    session = boto3.session.Session()
    return (
        session.client("cloudwatch", config=_BOTO_CONFIG),
        session.client("autoscaling", config=_BOTO_CONFIG),
    )


@dataclass(slots=True)
class AwsScalingBackend:
    """`ScalingBackend` over CloudWatch and EC2 Auto Scaling.

    The clients are constructor arguments rather than globals so the tests can hand in
    moto's; `sleep` is injected for the same reason, so the retry path costs nothing in
    wall-clock time to test. They are typed `Any` because boto3 synthesises its clients at
    runtime and has no static type to name without pulling in a stubs package that the
    Lambda deployment package would then have to carry.
    """

    asg_name: str
    cloudwatch: Any
    autoscaling: Any
    sleep: Callable[[float], None] = time.sleep
    max_attempts: int = _MAX_ATTEMPTS

    @classmethod
    def for_asg(cls, asg_name: str) -> AwsScalingBackend:
        """Build a backend on the cached client pair."""
        cloudwatch, autoscaling = _clients()
        return cls(asg_name=asg_name, cloudwatch=cloudwatch, autoscaling=autoscaling)

    def read_cpu(self, period_seconds: int = DEFAULT_PERIOD_SECONDS) -> float | None:
        """Newest `Maximum` CPU sample for the group, or `None` on an empty window.

        Dimensioned by `AutoScalingGroupName`, not `InstanceId`. The March 2026 capture in
        `data/` is per-instance because that run had exactly one instance, and a
        per-instance query would work here too -- for exactly one instance. The moment the
        group holds two, an `InstanceId` query is watching one member and blind to the
        load on the other, which is precisely the situation this controller exists to
        react to. The group dimension aggregates across members, so `Maximum` reads as
        "the hottest instance in the group right now". It is also the only identifier the
        function is given: `ASG_NAME` is in the environment contract, an instance id
        is not.

        The cost of that choice is a deployment requirement: group-level `CPUUtilization`
        is only published at 60 s resolution when the instances have detailed monitoring
        enabled. With basic monitoring the series is 5-minute, and a `METRIC_PERIOD` of 60
        would return an empty window on every invocation -- which this reports as a gap
        rather than as idle, so the failure is visible instead of expensive.
        """
        end = datetime.now(UTC)
        start = end - timedelta(seconds=period_seconds * _LOOKBACK_PERIODS)
        response = self._call(
            "GetMetricData",
            lambda: self.cloudwatch.get_metric_data(
                MetricDataQueries=[
                    {
                        "Id": _CPU_QUERY_ID,
                        "MetricStat": {
                            "Metric": {
                                "Namespace": CPU_NAMESPACE,
                                "MetricName": CPU_METRIC_NAME,
                                "Dimensions": [
                                    {"Name": "AutoScalingGroupName", "Value": self.asg_name}
                                ],
                            },
                            "Period": period_seconds,
                            "Stat": CPU_STATISTIC,
                        },
                        "ReturnData": True,
                    }
                ],
                StartTime=start,
                EndTime=end,
                ScanBy="TimestampDescending",
            ),
        )

        samples = [
            (timestamp, value)
            for result in response.get("MetricDataResults", [])
            if result.get("Id") == _CPU_QUERY_ID
            for timestamp, value in zip(
                result.get("Timestamps", []), result.get("Values", []), strict=True
            )
        ]
        if not samples:
            return None
        # Selected by timestamp rather than by position: `ScanBy` orders each page, and
        # trusting page order to survive a future pagination change is a bug waiting for a
        # busy day. Values are not clamped into [0, 100] either -- `decide` rejects an
        # impossible percentage and the invocation fails loudly, which holds the current
        # configuration and raises an Errors datapoint, rather than acting on a number the
        # metric should not have been able to produce.
        newest = max(samples)
        return float(newest[1])

    def read_state(self) -> State | None:
        """The group's current `State`, or `None` when it cannot be read as one.

        `None` means "unknown, reconcile": no warm pool exists yet, or the pool reports a
        `MaxGroupPreparedCapacity` of -1 (the API's sentinel for "defaults to the group's
        MaxSize"), or the combination violates an invariant `State` enforces. None of
        those can equal a state the controller would write, so treating them as unknown
        makes the next cycle write the full configuration and converge.

        The group is read before the pool so a wrong `ASG_NAME` surfaces as
        `MissingAutoScalingGroupError` rather than as a confusing warm-pool failure.
        """
        groups = self._call(
            "DescribeAutoScalingGroups",
            lambda: self.autoscaling.describe_auto_scaling_groups(
                AutoScalingGroupNames=[self.asg_name]
            ),
        ).get("AutoScalingGroups", [])
        if not groups:
            raise MissingAutoScalingGroupError(
                f"auto scaling group {self.asg_name!r} does not exist in this account/region"
            )
        desired_capacity = groups[0].get("DesiredCapacity")

        # The pool is read through DescribeWarmPool even though DescribeAutoScalingGroups
        # echoes a WarmPoolConfiguration block, because this is the value that gets
        # compared against what PutWarmPool writes. Reading through one API and writing
        # through another is how a drift check quietly stops matching.
        pool = self._call(
            "DescribeWarmPool",
            lambda: self.autoscaling.describe_warm_pool(AutoScalingGroupName=self.asg_name),
        ).get("WarmPoolConfiguration")
        if not pool:
            return None
        try:
            return State(
                desired_capacity=int(desired_capacity),
                warm_pool_min_size=int(pool["MinSize"]),
                max_group_prepared_capacity=int(pool["MaxGroupPreparedCapacity"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def write_state(self, state: State, previous: State | None = None) -> None:
        """Write the knobs that differ, in an order the API will accept.

        Only the changed knobs are written: a cost-saving to pre-warm move leaves
        `DesiredCapacity` at 1, so it is a single `PutWarmPool` and no group update.

        Ordering matters because Auto Scaling enforces
        `MaxGroupPreparedCapacity >= DesiredCapacity` on every call, and the two knobs
        live behind two APIs, so there is always an intermediate state that has to be
        legal too. The rule that is always safe:

        * ceiling rising -> raise the ceiling first; the intermediate is
          `(old desired, new ceiling)`, and the new ceiling exceeds the old one, which
          already bounded the old desired.
        * otherwise -> write `DesiredCapacity` first; the intermediate is
          `(new desired, old ceiling)`, and the old ceiling is at least the new one, which
          `State` guarantees bounds the new desired.

        `previous is None` takes the second branch. In the two cases that actually produce
        an unknown previous state -- no pool at all, or a pool whose ceiling defaults to
        the group's MaxSize -- there is no ceiling below the group's maximum to collide
        with, so writing capacity first is safe.

        `previous` is passed in rather than re-read so a cycle stays at two reads.
        """
        capacity_differs = previous is None or state.desired_capacity != previous.desired_capacity
        pool_differs = previous is None or (
            state.warm_pool_min_size != previous.warm_pool_min_size
            or state.max_group_prepared_capacity != previous.max_group_prepared_capacity
        )

        writes: list[tuple[str, Callable[[], object]]] = []
        if capacity_differs:
            writes.append(
                (
                    "UpdateAutoScalingGroup",
                    lambda: self.autoscaling.update_auto_scaling_group(
                        AutoScalingGroupName=self.asg_name,
                        DesiredCapacity=state.desired_capacity,
                    ),
                )
            )

        if pool_differs:
            pool_write = (
                "PutWarmPool",
                lambda: self.autoscaling.put_warm_pool(
                    AutoScalingGroupName=self.asg_name,
                    PoolState=WARM_POOL_STATE,
                    MinSize=state.warm_pool_min_size,
                    MaxGroupPreparedCapacity=state.max_group_prepared_capacity,
                ),
            )
            ceiling_rising = (
                previous is not None
                and state.max_group_prepared_capacity > previous.max_group_prepared_capacity
            )
            if ceiling_rising:
                writes.insert(0, pool_write)
            else:
                writes.append(pool_write)

        for operation, invoke in writes:
            self._call(operation, invoke)

    def _call[T](self, operation: str, invoke: Callable[[], T]) -> T:
        """Run one AWS call, retrying only what the server asked us to retry.

        Backoff is deterministic (1 s, 2 s) rather than jittered. Jitter exists to
        de-correlate a crowd of clients retrying in lockstep; there is exactly one
        governor per group, firing once a minute, so there is no crowd to de-correlate and
        a predictable delay is easier to reason about and to test. Three attempts caps the
        added latency at 3 s of sleep against a 60 s cadence -- and a group whose API is
        still throttling after three tries has a problem that another retry will not fix.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return invoke()
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if attempt >= self.max_attempts or not _is_retryable(exc):
                    LOGGER.error(
                        _event(
                            "governor.api_error",
                            asg_name=self.asg_name,
                            operation=operation,
                            error_code=code,
                            attempt=attempt,
                        )
                    )
                    raise
                delay = _BASE_DELAY_SECONDS * 2 ** (attempt - 1)
                LOGGER.warning(
                    _event(
                        "governor.api_retry",
                        asg_name=self.asg_name,
                        operation=operation,
                        error_code=code,
                        attempt=attempt,
                        delay_seconds=delay,
                    )
                )
                self.sleep(delay)


def run_once(
    backend: ScalingBackend,
    *,
    cfg: Config = DEFAULT,
    period_seconds: int = DEFAULT_PERIOD_SECONDS,
    dry_run: bool = False,
) -> Decision:
    """Read, decide, reconcile -- one invocation of the controller.

    Emits exactly one `governor.decision` line per call, so counting those lines counts
    invocations and no cycle can pass unaccounted for.

    Args:
        backend: Anything satisfying `ScalingBackend`.
        cfg: Thresholds and the phase -> state table.
        period_seconds: Metric period to query, in seconds.
        dry_run: Compute and log the decision, write nothing.

    Returns:
        The `Decision`, whether or not anything was written.
    """
    cpu = backend.read_cpu(period_seconds)

    if cpu is None:
        # Metric gap policy: hold. A gap is missing information, not 0 %, and reading it
        # as 0 % would scale in and drain the warm pool at exactly the moment the metric
        # pipeline is broken -- the worst possible time to give up warm capacity, and a
        # self-inflicted outage triggered by a monitoring failure. Holding costs one
        # cycle; the configuration stays whatever the last real measurement justified,
        # which is by construction the last defensible answer.
        #
        # The group is not read either. That read exists to decide whether a write is
        # needed; with no write possible it is pure cost, and a CloudWatch gap often
        # travels with wider API trouble, which is not the moment to add calls.
        decision = Decision(
            asg_name=backend.asg_name,
            cpu=None,
            phase=None,
            state=None,
            previous_phase=None,
            previous_state=None,
            outcome=Outcome.HELD_NO_DATA,
            dry_run=dry_run,
        )
        LOGGER.warning(decision.log_line())
        return decision

    phase, state = decide(cpu, cfg)
    previous_state = backend.read_state()

    if state == previous_state:
        outcome = Outcome.SKIPPED_IDEMPOTENT
    elif dry_run:
        outcome = Outcome.SKIPPED_DRY_RUN
    else:
        backend.write_state(state, previous_state)
        outcome = Outcome.APPLIED

    decision = Decision(
        asg_name=backend.asg_name,
        cpu=cpu,
        phase=phase,
        state=state,
        previous_phase=_phase_for_state(previous_state, cfg),
        previous_state=previous_state,
        outcome=outcome,
        dry_run=dry_run,
    )
    LOGGER.info(decision.log_line())
    return decision


def _phase_for_state(state: State | None, cfg: Config) -> Phase | None:
    """Reverse-lookup a state in the phase table, for the `transition` log field.

    `None` when the configuration matches no phase: a group that has never been written,
    or one someone has changed by hand.
    """
    if state is None:
        return None
    return next((phase for phase, known in cfg.states.items() if known == state), None)


def _state_fields(state: State | None) -> dict[str, int | None]:
    """The three knobs as log fields, all present, null when there is no state."""
    if state is None:
        return dict.fromkeys(_STATE_FIELDS)
    return {name: getattr(state, name) for name in _STATE_FIELDS}


def _is_retryable(exc: ClientError) -> bool:
    """Throttling and server faults are worth another try; nothing else is."""
    code = exc.response.get("Error", {}).get("Code", "")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
    return code in _RETRYABLE_ERROR_CODES or status >= 500


def _event(name: str, **fields: Any) -> str:
    """One JSON object on one line, with the event name first."""
    return json.dumps({"event": name, **fields})
