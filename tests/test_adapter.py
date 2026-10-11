"""Tests for the boto3 adapter, the control loop and the Lambda handler.

Why moto and not a hand-written fake
------------------------------------
The adapter's whole value is that it compares a decision against what the group is
*actually* configured with, so a test double that only records the arguments it was handed
can never fail the interesting way: it would happily agree with an adapter that wrote the
right call and read the wrong field. moto 5.2.3 was checked end to end first --
`create_auto_scaling_group` -> `put_warm_pool` -> `describe_warm_pool` returns the
configuration that was written, `update_auto_scaling_group` moves `DesiredCapacity`, and
`get_metric_data` honours the dimension, the period and `ScanBy` -- so the assertions below
read the state back out of a real Auto Scaling model instead of out of a recorded call.
`test_moto_round_trips_the_warm_pool_api` pins that, so if a future moto stops modelling
warm pools the suite says so directly instead of failing somewhere confusing.

Two things moto cannot do: throttle on demand, and enforce
`MaxGroupPreparedCapacity >= DesiredCapacity`. Those are covered by `RecordingClient`,
which wraps the same moto client, records every call, and raises queued errors -- so the
error paths and the call ordering are tested without a second, divergent model of AWS.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from governor.aws_adapter import (
    AwsScalingBackend,
    Decision,
    MissingAutoScalingGroupError,
    Outcome,
    ScalingBackend,
    run_once,
)
from governor.config import DEFAULT, SCALE_IN_15, Phase, State
from governor.handler import Settings, _configure_logging, lambda_handler

REGION = "ap-south-1"
ASG = "cloud-pool-manager-asg"

# The three CPU values below are the ones the March 2026 capture actually recorded at the
# transitions (see data/governor_1min.csv): idle, the pre-warm sample at 11:43Z and the
# mitigate sample at 11:44Z. Using them keeps the tests anchored to observed load rather
# than to numbers picked to sit comfortably inside a branch.
IDLE_CPU = 0.275
PRE_WARM_CPU = 29.9078
MITIGATE_CPU = 50.1232

COST_SAVING_STATE = State(1, 0, 1)
PRE_WARM_STATE = State(1, 2, 3)
MITIGATE_STATE = State(2, 0, 4)

_MUTATING_CALLS = frozenset({"put_warm_pool", "update_auto_scaling_group"})

GOVERNOR_ENV_VARS = (
    "ASG_NAME",
    "SCALE_IN_THRESHOLD",
    "PRE_WARM_THRESHOLD",
    "METRIC_PERIOD",
    "DRY_RUN",
    "LOG_LEVEL",
)


# --------------------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------------------


class RecordingClient:
    """A boto3 client wrapper that records calls and can fail them on cue.

    Every call is delegated to the wrapped client, so the AWS model stays moto's; this
    only adds the two things moto will not give us -- a call log, for asserting ordering
    and counts, and injected `ClientError`s, for the retry paths.
    """

    def __init__(self, inner: Any, failures: dict[str, list[Exception]] | None = None) -> None:
        self.inner = inner
        self.failures = {name: list(queue) for name, queue in (failures or {}).items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.inner, name)

        def call(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            queued = self.failures.get(name)
            if queued:
                raise queued.pop(0)
            return method(**kwargs)

        return call

    @property
    def operations(self) -> list[str]:
        return [name for name, _ in self.calls]


@dataclass
class Harness:
    """A live moto account with one Auto Scaling group, plus the backend under test."""

    cloudwatch: Any
    autoscaling: Any
    sleeps: list[float] = field(default_factory=list)
    recorder: RecordingClient | None = None

    def backend(
        self, *, failures: dict[str, list[Exception]] | None = None, max_attempts: int = 3
    ) -> AwsScalingBackend:
        """A backend whose Auto Scaling calls are recorded and whose sleeps are captured."""
        self.recorder = RecordingClient(self.autoscaling, failures)
        return AwsScalingBackend(
            asg_name=ASG,
            cloudwatch=self.cloudwatch,
            autoscaling=self.recorder,
            sleep=self.sleeps.append,
            max_attempts=max_attempts,
        )

    @property
    def mutations(self) -> list[str]:
        """Names of the mutating calls made, in order. Reads are excluded."""
        assert self.recorder is not None, "call harness.backend() first"
        return [name for name in self.recorder.operations if name in _MUTATING_CALLS]

    def publish_cpu(self, value: float, *, seconds_ago: int = 60) -> None:
        self.cloudwatch.put_metric_data(
            Namespace="AWS/EC2",
            MetricData=[
                {
                    "MetricName": "CPUUtilization",
                    "Dimensions": [{"Name": "AutoScalingGroupName", "Value": ASG}],
                    "Timestamp": datetime.now(UTC) - timedelta(seconds=seconds_ago),
                    "Value": value,
                    "Unit": "Percent",
                }
            ],
        )

    def configure(self, state: State) -> None:
        """Put the group into `state` directly, bypassing the adapter."""
        self.autoscaling.update_auto_scaling_group(
            AutoScalingGroupName=ASG, DesiredCapacity=state.desired_capacity
        )
        self.autoscaling.put_warm_pool(
            AutoScalingGroupName=ASG,
            PoolState="Stopped",
            MinSize=state.warm_pool_min_size,
            MaxGroupPreparedCapacity=state.max_group_prepared_capacity,
        )

    def observed(self) -> State | None:
        """Read the group's configuration straight out of moto."""
        group = self.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])[
            "AutoScalingGroups"
        ][0]
        pool = self.autoscaling.describe_warm_pool(AutoScalingGroupName=ASG).get(
            "WarmPoolConfiguration"
        )
        if not pool:
            return None
        return State(
            desired_capacity=group["DesiredCapacity"],
            warm_pool_min_size=pool["MinSize"],
            max_group_prepared_capacity=pool["MaxGroupPreparedCapacity"],
        )


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials, and none of the developer's own configuration.

    Without this a stray `AWS_PROFILE` or a real `ASG_NAME` in the shell would change what
    the suite tests, and a botocore misconfiguration could reach a real account.
    """
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    for name in GOVERNOR_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def restored_log_level() -> Any:
    """Logger levels are process-global; a test that changes one has to put it back."""
    logger = logging.getLogger("governor")
    previous = logger.level
    yield
    logger.setLevel(previous)


@pytest.fixture
def aws() -> Any:
    """A mocked account holding one group at the cost-saving capacity and no warm pool."""
    with mock_aws():
        autoscaling = boto3.client("autoscaling", region_name=REGION)
        cloudwatch = boto3.client("cloudwatch", region_name=REGION)
        autoscaling.create_launch_configuration(
            LaunchConfigurationName="cloud-pool-manager-lc",
            ImageId="ami-0123456789abcdef0",
            InstanceType="t3.micro",
        )
        autoscaling.create_auto_scaling_group(
            AutoScalingGroupName=ASG,
            LaunchConfigurationName="cloud-pool-manager-lc",
            MinSize=1,
            MaxSize=4,
            DesiredCapacity=1,
            AvailabilityZones=[f"{REGION}a"],
        )
        yield Harness(cloudwatch=cloudwatch, autoscaling=autoscaling)


def client_error(code: str, *, status: int = 400, operation: str = "PutWarmPool") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


# --------------------------------------------------------------------------------------
# The test double itself
# --------------------------------------------------------------------------------------


def test_moto_round_trips_the_warm_pool_api(aws: Harness) -> None:
    """Everything below rests on this: what PutWarmPool writes, DescribeWarmPool returns.

    If this ever fails, the rest of the file is asserting against a model that no longer
    describes AWS, and the fix is a different test double -- not a change to the adapter.
    """
    assert aws.observed() is None  # a group with no warm pool reports no configuration

    aws.configure(MITIGATE_STATE)

    assert aws.observed() == MITIGATE_STATE


# --------------------------------------------------------------------------------------
# Each phase writes its knobs
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cpu", "phase", "expected"),
    [
        (IDLE_CPU, Phase.COST_SAVING, COST_SAVING_STATE),
        (PRE_WARM_CPU, Phase.PRE_WARM, PRE_WARM_STATE),
        (MITIGATE_CPU, Phase.MITIGATE, MITIGATE_STATE),
    ],
)
def test_each_phase_writes_its_own_knobs(
    aws: Harness, cpu: float, phase: Phase, expected: State
) -> None:
    aws.publish_cpu(cpu)

    decision = run_once(aws.backend())

    assert decision.phase is phase
    assert decision.outcome is Outcome.APPLIED
    assert aws.observed() == expected


def test_the_pool_is_written_as_a_stopped_pool(aws: Harness) -> None:
    """`PoolState=Stopped` is the whole cost argument: stopped instances bill for storage
    only, so a pre-warmed instance costs a gp3 volume rather than a running t3.micro."""
    aws.publish_cpu(PRE_WARM_CPU)

    run_once(aws.backend())

    pool = aws.autoscaling.describe_warm_pool(AutoScalingGroupName=ASG)["WarmPoolConfiguration"]
    assert pool["PoolState"] == "Stopped"


def test_a_lower_scale_in_threshold_changes_the_phase(aws: Harness) -> None:
    """The 20 % / 15 % ambiguity in the original design is a configuration choice, and it
    has to remain one all the way through the adapter, not just inside `decide`."""
    aws.publish_cpu(17.5)

    assert run_once(aws.backend(), cfg=DEFAULT).phase is Phase.COST_SAVING

    aws.configure(COST_SAVING_STATE)
    assert run_once(aws.backend(), cfg=SCALE_IN_15).phase is Phase.PRE_WARM


# --------------------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------------------


def test_an_unchanged_group_is_not_rewritten(aws: Harness) -> None:
    """The steady state. At one invocation a minute this is the difference between ~1440
    mutating calls a day and none."""
    aws.publish_cpu(MITIGATE_CPU)
    aws.configure(MITIGATE_STATE)

    decision = run_once(aws.backend())

    assert decision.outcome is Outcome.SKIPPED_IDEMPOTENT
    assert aws.mutations == []
    assert aws.observed() == MITIGATE_STATE


def test_the_second_invocation_of_a_transition_writes_nothing(aws: Harness) -> None:
    aws.publish_cpu(MITIGATE_CPU)

    first = run_once(aws.backend())
    second = run_once(aws.backend())

    assert first.outcome is Outcome.APPLIED
    assert second.outcome is Outcome.SKIPPED_IDEMPOTENT
    assert aws.mutations == []  # recorder was replaced for the second invocation


def test_an_unchanged_knob_is_left_alone(aws: Harness) -> None:
    """Cost-saving to pre-warm holds `DesiredCapacity` at 1, so the group update is not
    just unnecessary, it is a scaling API call that would show up in CloudTrail saying
    nothing happened."""
    aws.configure(COST_SAVING_STATE)
    aws.publish_cpu(PRE_WARM_CPU)

    run_once(aws.backend())

    assert aws.mutations == ["put_warm_pool"]
    assert aws.observed() == PRE_WARM_STATE


def test_a_group_with_no_warm_pool_is_treated_as_unknown(aws: Harness) -> None:
    """A fresh group reports no pool, which cannot equal any decided state, so the first
    invocation writes the full configuration and the next one is idempotent."""
    backend = aws.backend()

    assert backend.read_state() is None

    aws.publish_cpu(IDLE_CPU)
    assert run_once(aws.backend()).outcome is Outcome.APPLIED
    assert run_once(aws.backend()).outcome is Outcome.SKIPPED_IDEMPOTENT


def test_a_defaulted_prepared_capacity_reads_as_unknown(aws: Harness) -> None:
    """-1 is the API's sentinel for "the ceiling defaults to the group's MaxSize". It is
    not a capacity, it cannot equal one, and reconciling is the honest response."""
    aws.autoscaling.put_warm_pool(
        AutoScalingGroupName=ASG, PoolState="Stopped", MinSize=2, MaxGroupPreparedCapacity=-1
    )

    assert aws.backend().read_state() is None


def test_a_ceiling_below_desired_capacity_reads_as_unknown(aws: Harness) -> None:
    """A prepared ceiling sitting under the active capacity is a combination the controller
    never writes, so it cannot equal a decided state and reconciling is the honest answer."""
    aws.autoscaling.update_auto_scaling_group(AutoScalingGroupName=ASG, DesiredCapacity=2)
    aws.autoscaling.put_warm_pool(
        AutoScalingGroupName=ASG, PoolState="Stopped", MinSize=0, MaxGroupPreparedCapacity=1
    )

    assert aws.backend().read_state() is None


# --------------------------------------------------------------------------------------
# Write ordering
# --------------------------------------------------------------------------------------


def test_the_prepared_ceiling_is_raised_before_desired_capacity(aws: Harness) -> None:
    """Cost-saving (1, 0, 1) to mitigate (2, 0, 4). Writing `DesiredCapacity=2` first would
    leave the group at desired 2 under a ceiling of 1, which the API rejects."""
    aws.configure(COST_SAVING_STATE)
    aws.publish_cpu(MITIGATE_CPU)

    run_once(aws.backend())

    assert aws.mutations == ["put_warm_pool", "update_auto_scaling_group"]
    assert aws.observed() == MITIGATE_STATE


def test_desired_capacity_is_lowered_before_the_prepared_ceiling(aws: Harness) -> None:
    """Mitigate (2, 0, 4) to cost-saving (1, 0, 1), the mirror image: dropping the ceiling
    to 1 while capacity is still 2 is the same illegal intermediate from the other side."""
    aws.configure(MITIGATE_STATE)
    aws.publish_cpu(IDLE_CPU)

    run_once(aws.backend())

    assert aws.mutations == ["update_auto_scaling_group", "put_warm_pool"]
    assert aws.observed() == COST_SAVING_STATE


# --------------------------------------------------------------------------------------
# DRY_RUN
# --------------------------------------------------------------------------------------


def test_dry_run_decides_but_does_not_write(aws: Harness) -> None:
    aws.configure(COST_SAVING_STATE)
    aws.publish_cpu(MITIGATE_CPU)

    decision = run_once(aws.backend(), dry_run=True)

    assert decision.outcome is Outcome.SKIPPED_DRY_RUN
    assert decision.phase is Phase.MITIGATE
    assert decision.state == MITIGATE_STATE  # it still reports what it would have written
    assert aws.mutations == []
    assert aws.observed() == COST_SAVING_STATE


def test_dry_run_still_reports_an_idempotent_skip_as_one(aws: Harness) -> None:
    """Dry-run must not disguise the steady state as suppressed work, or an operator
    validating a deployment cannot tell "would have written" from "nothing to write"."""
    aws.configure(MITIGATE_STATE)
    aws.publish_cpu(MITIGATE_CPU)

    assert run_once(aws.backend(), dry_run=True).outcome is Outcome.SKIPPED_IDEMPOTENT


# --------------------------------------------------------------------------------------
# Errors and retries
# --------------------------------------------------------------------------------------


def test_throttling_is_retried_with_exponential_backoff(aws: Harness) -> None:
    aws.publish_cpu(MITIGATE_CPU)
    backend = aws.backend(
        failures={"put_warm_pool": [client_error("Throttling"), client_error("Throttling")]}
    )

    decision = run_once(backend)

    assert decision.outcome is Outcome.APPLIED
    assert aws.sleeps == [1.0, 2.0]
    assert aws.mutations.count("put_warm_pool") == 3
    assert aws.observed() == MITIGATE_STATE


def test_server_faults_are_retried_on_status_alone(aws: Harness) -> None:
    """A 5xx is retryable whatever it calls itself; the code list cannot be exhaustive."""
    aws.publish_cpu(MITIGATE_CPU)
    backend = aws.backend(
        failures={"update_auto_scaling_group": [client_error("SomeInternalThing", status=500)]}
    )

    assert run_once(backend).outcome is Outcome.APPLIED
    assert aws.sleeps == [1.0]


def test_retries_are_bounded(aws: Harness) -> None:
    """Three attempts, then the invocation fails. A group still throttling after three
    tries has a problem a fourth will not solve, and the next tick is 60 s away."""
    aws.publish_cpu(MITIGATE_CPU)
    backend = aws.backend(failures={"put_warm_pool": [client_error("Throttling")] * 3})

    with pytest.raises(ClientError):
        run_once(backend)

    assert aws.sleeps == [1.0, 2.0]
    assert aws.mutations.count("put_warm_pool") == 3


def test_validation_errors_are_not_retried(aws: Harness) -> None:
    """Retrying a 400 is just a slower 400 -- and it holds the invocation open while the
    request stays exactly as wrong as it was."""
    aws.publish_cpu(MITIGATE_CPU)
    backend = aws.backend(
        failures={"update_auto_scaling_group": [client_error("ValidationError")]}
    )

    with pytest.raises(ClientError) as raised:
        run_once(backend)

    assert raised.value.response["Error"]["Code"] == "ValidationError"
    assert aws.sleeps == []
    assert aws.mutations == ["update_auto_scaling_group"]  # one attempt, no repeats


def test_access_denied_is_not_retried(aws: Harness) -> None:
    """A too-narrow IAM policy is a deployment bug. Retrying it three times a minute turns
    a clear failure into a quieter, slower, more expensive one."""
    aws.publish_cpu(MITIGATE_CPU)
    backend = aws.backend(
        failures={"describe_auto_scaling_groups": [client_error("AccessDenied", status=403)]}
    )

    with pytest.raises(ClientError):
        run_once(backend)

    assert aws.sleeps == []


def test_an_unknown_group_name_fails_loudly(aws: Harness) -> None:
    """DescribeAutoScalingGroups answers a query for a missing group with an empty list,
    not an error, so a typo in `ASG_NAME` would otherwise look like a healthy no-op."""
    backend = AwsScalingBackend(
        asg_name="not-the-group", cloudwatch=aws.cloudwatch, autoscaling=aws.autoscaling
    )

    with pytest.raises(MissingAutoScalingGroupError, match="not-the-group"):
        backend.read_state()


# --------------------------------------------------------------------------------------
# The metric read, and the gap
# --------------------------------------------------------------------------------------


def test_read_cpu_returns_the_newest_sample(aws: Harness) -> None:
    aws.publish_cpu(10.0, seconds_ago=180)
    aws.publish_cpu(20.0, seconds_ago=120)
    aws.publish_cpu(30.0, seconds_ago=60)

    assert aws.backend().read_cpu(60) == pytest.approx(30.0)


def test_the_metric_is_queried_by_group_not_by_instance(aws: Harness) -> None:
    """The dimension choice, asserted behaviourally. An `InstanceId` query watches one
    member and is blind to load on the others, which is exactly the case the controller
    exists for; only `AutoScalingGroupName` aggregates across the group."""
    aws.cloudwatch.put_metric_data(
        Namespace="AWS/EC2",
        MetricData=[
            {
                "MetricName": "CPUUtilization",
                "Dimensions": [{"Name": "InstanceId", "Value": "i-0da018dd62e55fada"}],
                "Timestamp": datetime.now(UTC) - timedelta(seconds=60),
                "Value": MITIGATE_CPU,
                "Unit": "Percent",
            }
        ],
    )

    assert aws.backend().read_cpu(60) is None


def test_read_cpu_is_none_when_the_window_is_empty(aws: Harness) -> None:
    assert aws.backend().read_cpu(60) is None


def test_a_metric_gap_holds_the_current_configuration(aws: Harness) -> None:
    """The policy: a gap is missing information, not 0 %.

    Reading an empty window as idle would scale in and drain the warm pool at exactly the
    moment the metric pipeline is broken -- an outage caused by the monitoring failing,
    not by the load. Holding costs one 60 s cycle and leaves the group in the last
    configuration a real measurement justified.
    """
    aws.configure(MITIGATE_STATE)

    decision = run_once(aws.backend())

    assert decision.outcome is Outcome.HELD_NO_DATA
    assert decision.cpu is None
    assert decision.phase is None
    assert aws.observed() == MITIGATE_STATE


def test_a_metric_gap_does_not_read_the_group_either(aws: Harness) -> None:
    """The group read exists to decide whether a write is needed. With no write possible
    it is pure cost, and a CloudWatch gap often travels with wider API trouble."""
    backend = aws.backend()

    run_once(backend)

    assert aws.recorder is not None
    assert aws.recorder.operations == []


def test_stale_samples_inside_the_lookback_window_are_still_used(aws: Harness) -> None:
    """EC2 publishes with a lag, so a one-period window lands on a publication delay often
    enough to matter. Four minutes old is old, but it is a measurement, and acting on it
    beats holding on a gap that is not one."""
    aws.publish_cpu(MITIGATE_CPU, seconds_ago=240)

    assert run_once(aws.backend()).phase is Phase.MITIGATE


# --------------------------------------------------------------------------------------
# The log line
# --------------------------------------------------------------------------------------


def test_the_decision_is_one_json_object_per_invocation(
    aws: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """Exactly one `governor.decision` line per call, so counting those lines in Logs
    Insights counts invocations and no cycle can pass unaccounted for."""
    aws.configure(PRE_WARM_STATE)
    aws.publish_cpu(MITIGATE_CPU)

    with caplog.at_level(logging.INFO, logger="governor"):
        run_once(aws.backend())

    lines = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name.startswith("governor")
    ]
    decisions = [line for line in lines if line["event"] == "governor.decision"]
    assert len(decisions) == 1

    logged = decisions[0]
    assert logged["phase"] == "mitigate"
    assert logged["transition"] == "pre_warm->mitigate"
    assert logged["cpu"] == pytest.approx(MITIGATE_CPU)
    assert logged["dry_run"] is False
    assert logged["outcome"] == "applied"
    assert logged["desired_capacity"] == 2
    assert logged["warm_pool_min_size"] == 0
    assert logged["max_group_prepared_capacity"] == 4
    assert logged["asg_name"] == ASG


def test_the_knob_fields_are_present_even_when_nothing_was_decided(aws: Harness) -> None:
    """Null, never absent: a Logs Insights query should not have to cope with a field that
    only exists on some lines."""
    held = json.loads(run_once(aws.backend()).log_line())

    assert held["outcome"] == "held_no_data"
    assert held["transition"] == "unknown->hold"
    for name in ("cpu", "phase", "desired_capacity", "max_group_prepared_capacity"):
        assert name in held
        assert held[name] is None


def test_a_retry_logs_the_error_code_and_the_delay(
    aws: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    aws.publish_cpu(MITIGATE_CPU)
    backend = aws.backend(failures={"put_warm_pool": [client_error("Throttling")]})

    with caplog.at_level(logging.WARNING, logger="governor"):
        run_once(backend)

    retries = [
        json.loads(record.getMessage())
        for record in caplog.records
        if json.loads(record.getMessage())["event"] == "governor.api_retry"
    ]
    assert len(retries) == 1
    assert retries[0]["operation"] == "PutWarmPool"
    assert retries[0]["error_code"] == "Throttling"
    assert retries[0]["delay_seconds"] == 1.0


def test_an_unknown_previous_configuration_is_logged_as_unknown(aws: Harness) -> None:
    aws.publish_cpu(IDLE_CPU)

    assert json.loads(run_once(aws.backend()).log_line())["transition"] == "unknown->cost_saving"


# --------------------------------------------------------------------------------------
# The protocol seam
# --------------------------------------------------------------------------------------


class MemoryBackend:
    """A backend with no AWS in it at all.

    This is the seam's evidence: the control loop is written against `ScalingBackend`, so a
    LocalStack or GCP implementation is a new class and no change to `fsm.py` or to
    `run_once`.
    """

    def __init__(self, cpu: float | None, state: State | None = None) -> None:
        self.asg_name = "in-memory"
        self.cpu = cpu
        self.state = state
        self.writes: list[State] = []

    def read_cpu(self, period_seconds: int) -> float | None:
        return self.cpu

    def read_state(self) -> State | None:
        return self.state

    def write_state(self, state: State, previous: State | None = None) -> None:
        self.state = state
        self.writes.append(state)


def test_the_control_loop_runs_against_a_non_aws_backend() -> None:
    backend = MemoryBackend(cpu=MITIGATE_CPU, state=PRE_WARM_STATE)

    assert isinstance(backend, ScalingBackend)

    decision = run_once(backend)

    assert decision.outcome is Outcome.APPLIED
    assert backend.writes == [MITIGATE_STATE]
    assert run_once(backend).outcome is Outcome.SKIPPED_IDEMPOTENT


def test_the_aws_backend_satisfies_the_protocol(aws: Harness) -> None:
    assert isinstance(aws.backend(), ScalingBackend)


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["1", "t", "true", "TRUE", " True ", "y", "yes", "on"])
def test_dry_run_truthy_spellings(raw: str) -> None:
    assert Settings.from_env({"ASG_NAME": ASG, "DRY_RUN": raw}).dry_run is True


@pytest.mark.parametrize("raw", ["0", "f", "false", "FALSE", "n", "no", "off", "", "  "])
def test_dry_run_falsey_spellings(raw: str) -> None:
    assert Settings.from_env({"ASG_NAME": ASG, "DRY_RUN": raw}).dry_run is False


def test_dry_run_defaults_to_writing_when_unset() -> None:
    assert Settings.from_env({"ASG_NAME": ASG}).dry_run is False


@pytest.mark.parametrize("raw", ["off!", "disabled", "maybe", "2", "none"])
def test_an_unreadable_dry_run_is_refused(raw: str) -> None:
    """Falling back to false here would silently enable live writes an operator meant to
    suppress, and the typo that caused it would never appear anywhere."""
    with pytest.raises(ValueError, match="DRY_RUN"):
        Settings.from_env({"ASG_NAME": ASG, "DRY_RUN": raw})


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_a_blank_asg_name_is_refused(raw: str | None) -> None:
    env = {"ASG_NAME": raw} if raw is not None else {}
    with pytest.raises(ValueError, match="ASG_NAME"):
        Settings.from_env(env)


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("30", 30), ("60", 60), ("300", 300)])
def test_valid_metric_periods(raw: str, expected: int) -> None:
    assert Settings.from_env({"ASG_NAME": ASG, "METRIC_PERIOD": raw}).period_seconds == expected


@pytest.mark.parametrize("raw", ["45", "0", "-60", "7", "sixty"])
def test_invalid_metric_periods_are_refused(raw: str) -> None:
    """CloudWatch would reject these once a minute forever; better to reject them once."""
    with pytest.raises(ValueError, match="METRIC_PERIOD"):
        Settings.from_env({"ASG_NAME": ASG, "METRIC_PERIOD": raw})


def test_the_metric_period_defaults_to_sixty_seconds() -> None:
    assert Settings.from_env({"ASG_NAME": ASG}).period_seconds == 60


def test_thresholds_come_from_the_environment() -> None:
    settings = Settings.from_env(
        {"ASG_NAME": ASG, "SCALE_IN_THRESHOLD": "15", "PRE_WARM_THRESHOLD": "45"}
    )

    assert settings.config.scale_in_threshold == 15.0
    assert settings.config.pre_warm_threshold == 45.0


def test_an_unreadable_threshold_is_refused() -> None:
    with pytest.raises(ValueError, match="SCALE_IN_THRESHOLD"):
        Settings.from_env({"ASG_NAME": ASG, "SCALE_IN_THRESHOLD": "twenty"})


# --------------------------------------------------------------------------------------
# The handler
# --------------------------------------------------------------------------------------


@pytest.fixture
def wired(aws: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """Point the handler's cached client pair at the mocked account."""
    from governor import aws_adapter

    monkeypatch.setattr(aws_adapter, "_clients", lambda: (aws.cloudwatch, aws.autoscaling))
    monkeypatch.setenv("ASG_NAME", ASG)
    return aws


def test_the_handler_returns_a_json_serialisable_summary(wired: Harness) -> None:
    """The return value exists so a console test invoke shows the outcome without opening
    CloudWatch Logs, which only works if Lambda can serialise it."""
    wired.publish_cpu(MITIGATE_CPU)

    result = lambda_handler({"source": "aws.events"}, None)

    assert json.loads(json.dumps(result)) == result
    assert result["phase"] == "mitigate"
    assert result["outcome"] == "applied"
    assert result["desired_capacity"] == 2
    assert wired.observed() == MITIGATE_STATE


def test_the_handler_honours_dry_run(wired: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    wired.configure(COST_SAVING_STATE)
    wired.publish_cpu(MITIGATE_CPU)

    result = lambda_handler({}, None)

    assert result["outcome"] == "skipped_dry_run"
    assert result["dry_run"] is True
    assert wired.observed() == COST_SAVING_STATE


def test_the_handler_honours_the_scale_in_threshold(
    wired: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCALE_IN_THRESHOLD", "15")
    wired.publish_cpu(17.5)

    assert lambda_handler({}, None)["phase"] == "pre_warm"


def test_the_handler_matches_what_the_decision_logs(wired: Harness) -> None:
    """The returned payload and the log line are rendered from the same object, so the two
    cannot drift into describing different things."""
    wired.publish_cpu(PRE_WARM_CPU)

    result = lambda_handler({}, None)
    logged = json.loads(
        Decision(
            asg_name=result["asg_name"],
            cpu=result["cpu"],
            phase=Phase(result["phase"]),
            state=PRE_WARM_STATE,
            previous_phase=None,
            previous_state=None,
            outcome=Outcome(result["outcome"]),
            dry_run=result["dry_run"],
        ).log_line()
    )

    assert {key: value for key, value in logged.items() if key != "event"} == result


def test_a_missing_asg_name_fails_the_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASG_NAME", raising=False)

    with pytest.raises(ValueError, match="ASG_NAME"):
        lambda_handler({}, None)


def test_an_unreadable_log_level_falls_back_to_info(caplog: pytest.LogCaptureFixture) -> None:
    """The one configuration error that does not fail the invocation: a bad level name
    cannot change what gets written, so taking the controller down over it would be the
    more damaging response."""
    with caplog.at_level(logging.WARNING, logger="governor"):
        _configure_logging("LOUD")
        # Asserted inside the block: caplog restores the level it borrowed on exit.
        assert logging.getLogger("governor").level == logging.INFO

    warning = json.loads(caplog.records[-1].getMessage())
    assert warning["event"] == "governor.config_warning"
    assert warning["variable"] == "LOG_LEVEL"


def test_a_readable_log_level_is_applied(caplog: pytest.LogCaptureFixture) -> None:
    _configure_logging("debug")

    assert logging.getLogger("governor").level == logging.DEBUG
