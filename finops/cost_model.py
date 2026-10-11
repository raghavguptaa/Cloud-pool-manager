"""Projected monthly cost of three warm-pool strategies, priced from AWS list rates.

**These are projections from a pricing model, not metered spend.** No AWS bill was
captured from the deployment this describes, and the account has since been closed, so
none of these figures can be reconciled against one. Every number below is an AWS list
price multiplied by a modelled quantity of usage, and every rate carries its source in
`finops/pricing.json`.

The three scenarios, all at the same instance type in the same region:

* **always-on** -- a fixed fleet (three `t3.micro` by default) running 24/7. This is
  what the original experiment compared against.
* **static warm pool** -- one instance in service plus two permanently stopped warm
  instances. The stopped pair costs nothing in compute, but their EBS volumes bill for
  every hour of the month regardless. That *phantom storage* is the entire reason a
  pool that drains to zero can beat a pool that merely sits there.
* **dynamic governor** -- one instance in service; the warm pool is held at zero while
  CPU is below the scale-in threshold and only spun up for the fraction of the month
  under load. When the pool drains, the pooled instances terminate and their root
  volumes go with them, so the phantom storage disappears rather than idling.

The assumption the result is most sensitive to is the **duty cycle**: the share of the
month the governor spends out of its cost-saving phase. It is an explicit named input
(`Assumptions.duty_cycle`), it is swept in the sensitivity table, and it is the first
thing to argue with if you disagree with the conclusion.

One finding worth stating up front, because it is the kind of thing a cost model exists
to catch: the controller's own operating cost is dominated not by the Lambda but by the
CloudWatch ``GetMetricData`` calls the Lambda makes. `GetMetricData` bills per metric
requested and is excluded from the CloudWatch free tier, so a 60-second poll costs
several times what the compute it runs on does. It is still a rounding error against
the fleet -- the model prints it so you can see that it is, rather than asserting it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any, Final

__all__ = [
    "PROJECTION_NOTICE",
    "Assumptions",
    "Breakdown",
    "Rates",
    "Reconciliation",
    "all_scenarios",
    "always_on",
    "break_even_duty_cycle",
    "dynamic_governor",
    "main",
    "phantom_storage",
    "reconcile",
    "reduction",
    "render_report",
    "static_warm_pool",
    "write_chart",
]


PROJECTION_NOTICE: Final = (
    "THESE ARE PROJECTIONS FROM A PRICING MODEL, NOT METERED SPEND.\n"
    "No AWS bill was captured for this architecture and none can be -- the account is\n"
    "closed. Every figure is a published list price times a modelled quantity of usage.\n"
    "Rates, with the AWS page each came from, are in finops/pricing.json."
)

HOURS_PER_MONTH: Final = 730.0
"""AWS's own convention for a month: 365 x 24 / 12. Using calendar months instead
swings every total by +-2 %, which is larger than several of the line items below --
hence an explicit constant rather than a quietly assumed 720 or 744."""

DETAILED_METRICS_PER_INSTANCE: Final = 7
"""EC2 detailed (1-minute) monitoring publishes seven metrics per instance. The
governor needs it: a 60-second control loop cannot run on 5-minute basic monitoring."""

# Derived from data/governor_1min.csv, classified at the default 20 % / 40 % thresholds:
# of its 26 one-minute samples, 5 sit at or above 20 % and 3 of those at or above 40 %.
# That window is a deliberate load test, not a representative month -- it appears in the
# sensitivity sweep as a labelled row and is NOT the default duty cycle.
OBSERVED_WINDOW_SAMPLES: Final = 26
OBSERVED_ELEVATED_SAMPLES: Final = 5
OBSERVED_MITIGATE_SAMPLES: Final = 3

# The figures the original experiment's cost analysis reported, kept as data so the
# model can be checked against them instead of quietly agreeing with them.
ORIGINAL_ALWAYS_ON_USD: Final = 24.95
ORIGINAL_GOVERNOR_USD: Final = 8.66
ORIGINAL_CUT_VS_ALWAYS_ON: Final = 0.65
ORIGINAL_CUT_VS_STATIC: Final = 0.11

_REQUIRED_RATE_FIELDS: Final = ("value", "unit", "as_of", "source")


# --------------------------------------------------------------------------------------
# Rates
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rates:
    """The published rates the model consumes, loaded from `pricing.json`.

    Flattened out of the JSON on purpose: the JSON carries the citation metadata a
    reader needs, this object carries only the numbers the arithmetic needs, and
    `from_document` is the one place the two representations meet.
    """

    region: str
    region_name: str
    instance_hour: float
    gp3_gb_month: float
    ipv4_hour: float
    lambda_per_request: float
    lambda_gb_second: float
    metric_data_per_metric: float
    detailed_metric_month: float
    free_detailed_metrics: float

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Rates:
        """Build `Rates` from a parsed `pricing.json`, rejecting uncited rates.

        The citation check is not decoration. A rate with no `source` and no `as_of` is
        indistinguishable from a number someone remembered, and a cost model built on
        remembered numbers is worth nothing. Failing the load is the cheapest possible
        place to catch that.
        """
        rates = document.get("rates")
        if not isinstance(rates, Mapping):
            raise ValueError("pricing document has no 'rates' object")

        for name, entry in rates.items():
            if not isinstance(entry, Mapping):
                raise ValueError(f"rate {name!r} is not an object")
            missing = [f for f in _REQUIRED_RATE_FIELDS if not entry.get(f)]
            if missing:
                raise ValueError(
                    f"rate {name!r} is missing {', '.join(missing)}; "
                    "every rate must carry its value, unit, as-of date and source URL"
                )

        def rate(name: str) -> float:
            try:
                return float(rates[name]["value"])
            except KeyError as exc:
                raise ValueError(f"pricing document is missing rate {name!r}") from exc

        return cls(
            region=str(document["region"]),
            region_name=str(document["region_name"]),
            instance_hour=rate("ec2_on_demand_t3_micro"),
            gp3_gb_month=rate("ebs_gp3_storage"),
            ipv4_hour=rate("public_ipv4_in_use"),
            lambda_per_request=rate("lambda_requests"),
            lambda_gb_second=rate("lambda_gb_second"),
            metric_data_per_metric=rate("cloudwatch_get_metric_data"),
            detailed_metric_month=rate("cloudwatch_detailed_monitoring_metric"),
            free_detailed_metrics=rate("cloudwatch_free_tier_metrics"),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> Rates:
        """Load the rates shipped alongside this module, or from an explicit path."""
        if path is None:
            text = resources.files(__package__).joinpath("pricing.json").read_text("utf-8")
        else:
            text = path.read_text("utf-8")
        return cls.from_document(json.loads(text))


# --------------------------------------------------------------------------------------
# Assumptions
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Assumptions:
    """Every modelling choice, in one place, named and overridable.

    Nothing in the arithmetic below reaches for a literal. If a number here is wrong the
    conclusion moves, and the point of collecting them is that a reader can see exactly
    which number to push on.
    """

    fleet_size: int = 3
    """Instances the always-on comparison runs 24/7, and the size the static warm pool
    reaches when fully prepared (1 active + 2 stopped)."""

    hours_per_month: float = HOURS_PER_MONTH
    volume_gb: float = 8.0
    """Root volume per instance. 8 GB is the Amazon Linux 2023 default; it is also the
    single most leveraged input in the static-vs-governor comparison, because phantom
    storage scales with it directly."""

    duty_cycle: float = 0.05
    """Share of the month the governor spends above the scale-in threshold, i.e. out of
    its cost-saving phase. The headline assumption. 5 % is roughly 36 hours a month."""

    mitigate_share: float = 0.40
    """Of that duty window, the share spent in MITIGATE (a second instance actually in
    service) rather than PRE_WARM (two stopped instances waiting in the pool)."""

    poll_seconds: float = 60.0
    lambda_memory_mb: float = 128.0
    lambda_duration_seconds: float = 1.0
    charge_public_ipv4: bool = False
    """Off by default. Three in-use public IPv4 addresses add $10.95/month, which would
    swamp every comparison here; the default assumes instances sit behind a load
    balancer or NAT with no address of their own."""

    def __post_init__(self) -> None:
        if self.fleet_size < 2:
            raise ValueError("fleet_size must be at least 2 for a warm pool to mean anything")
        for name in ("hours_per_month", "volume_gb", "poll_seconds", "lambda_memory_mb"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")
        if self.lambda_duration_seconds <= 0:
            raise ValueError("lambda_duration_seconds must be positive")
        for name in ("duty_cycle", "mitigate_share"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} is a fraction and must lie in [0, 1], got {value}")

    @property
    def mitigate_hours(self) -> float:
        """Hours a month the governor holds a second instance in service."""
        return self.hours_per_month * self.duty_cycle * self.mitigate_share

    @property
    def pre_warm_hours(self) -> float:
        """Hours a month the governor holds two stopped instances in the warm pool."""
        return self.hours_per_month * self.duty_cycle * (1.0 - self.mitigate_share)

    @property
    def invocations(self) -> float:
        """Controller invocations a month, one per poll interval."""
        return self.hours_per_month * 3600.0 / self.poll_seconds


# --------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Breakdown:
    """One scenario's monthly bill, split into the parts that behave differently.

    `controller_lambda` and `controller_metrics` are kept apart because they answer
    different questions: the first is what running the control loop costs, the second is
    what *observing the system well enough to run it* costs -- and the second is the
    larger of the two.
    """

    scenario: str
    compute: float
    storage: float
    controller_lambda: float = 0.0
    controller_metrics: float = 0.0
    public_ipv4: float = 0.0

    @property
    def controller(self) -> float:
        return self.controller_lambda + self.controller_metrics

    @property
    def total(self) -> float:
        return self.compute + self.storage + self.controller + self.public_ipv4


def _controller_cost(rates: Rates, a: Assumptions) -> tuple[float, float]:
    """Return `(lambda_cost, observability_cost)` for the governor's control loop.

    The observability half is the surprising one. `GetMetricData` bills per *metric
    requested* -- not per API call -- and is one of the few CloudWatch APIs the free
    tier explicitly excludes, so a once-a-minute poll for a single metric bills 43,800
    metrics a month. Detailed monitoring itself is usually free here: it publishes seven
    metrics per instance and the free allowance is ten, so only the second instance that
    MITIGATE brings into service can push the account past it.
    """
    invocations = a.invocations
    lambda_cost = invocations * rates.lambda_per_request / 1_000_000.0 + (
        invocations
        * (a.lambda_memory_mb / 1024.0)
        * a.lambda_duration_seconds
        * rates.lambda_gb_second
    )

    metric_api = invocations * rates.metric_data_per_metric / 1_000.0

    instance_months = (a.hours_per_month + a.mitigate_hours) / a.hours_per_month
    metric_months = DETAILED_METRICS_PER_INSTANCE * instance_months
    billable_metrics = max(0.0, metric_months - rates.free_detailed_metrics)
    detailed = billable_metrics * rates.detailed_metric_month

    return lambda_cost, metric_api + detailed


def always_on(rates: Rates, a: Assumptions) -> Breakdown:
    """A fixed fleet running 24/7 -- no pool, no controller, no scaling at all."""
    running_hours = a.fleet_size * a.hours_per_month
    return Breakdown(
        scenario="always-on",
        compute=running_hours * rates.instance_hour,
        storage=a.fleet_size * a.volume_gb * rates.gp3_gb_month,
        public_ipv4=running_hours * rates.ipv4_hour if a.charge_public_ipv4 else 0.0,
    )


def static_warm_pool(rates: Rates, a: Assumptions) -> Breakdown:
    """One instance in service, the rest of the fleet permanently stopped in the pool.

    Compute drops to a single instance, but the storage line does not move at all: a
    stopped instance's root volume is billed exactly like a running one's. Those
    volume-months are the phantom storage, and `phantom_storage` below isolates them.
    """
    return Breakdown(
        scenario="static warm pool",
        compute=a.hours_per_month * rates.instance_hour,
        storage=a.fleet_size * a.volume_gb * rates.gp3_gb_month,
        public_ipv4=a.hours_per_month * rates.ipv4_hour if a.charge_public_ipv4 else 0.0,
    )


def dynamic_governor(rates: Rates, a: Assumptions) -> Breakdown:
    """One instance in service; warm capacity exists only during the duty window.

    Instance-hours follow the FSM directly. Every phase keeps `desired_capacity >= 1`,
    so one instance runs all month; MITIGATE raises it to 2 for the length of the spike.

    Volume-months follow the instances, which is the whole trick: COST_SAVING sets
    `warm_pool_min_size = 0`, the pooled instances terminate, and -- with the launch
    template's default `delete_on_termination` on the root volume -- their storage stops
    billing instead of idling. That assumption is load-bearing: pin
    `delete_on_termination = false` and this scenario degrades to the static one.
    """
    running_hours = a.hours_per_month + a.mitigate_hours
    volume_hours = a.hours_per_month + 2.0 * a.pre_warm_hours + a.mitigate_hours
    lambda_cost, metrics_cost = _controller_cost(rates, a)
    return Breakdown(
        scenario="dynamic governor",
        compute=running_hours * rates.instance_hour,
        storage=(volume_hours / a.hours_per_month) * a.volume_gb * rates.gp3_gb_month,
        controller_lambda=lambda_cost,
        controller_metrics=metrics_cost,
        public_ipv4=running_hours * rates.ipv4_hour if a.charge_public_ipv4 else 0.0,
    )


def all_scenarios(rates: Rates, a: Assumptions) -> list[Breakdown]:
    """The three scenarios in comparison order: most expensive strategy first."""
    return [always_on(rates, a), static_warm_pool(rates, a), dynamic_governor(rates, a)]


def phantom_storage(rates: Rates, a: Assumptions) -> float:
    """Monthly cost of the static pool's stopped-instance volumes: paying to keep disks
    warm for instances that are not running."""
    return (a.fleet_size - 1) * a.volume_gb * rates.gp3_gb_month


def reduction(candidate: float, baseline: float) -> float:
    """Fractional saving of `candidate` against `baseline`; negative means worse."""
    if baseline <= 0.0:
        raise ValueError("baseline must be positive to express a reduction against it")
    return 1.0 - candidate / baseline


def break_even_duty_cycle(rates: Rates, a: Assumptions) -> float | None:
    """The duty cycle at which the governor stops beating a static warm pool.

    Found by bisection rather than algebra: the governor's cost is affine in the duty
    cycle *today*, but the free-tier clamp in `_controller_cost` is a kink waiting to
    happen, and a search that survives the model changing shape is worth the twelve
    lines. Returns `None` when the governor wins at every duty cycle in [0, 1].
    """
    target = static_warm_pool(rates, a).total

    def governor_total(duty: float) -> float:
        return dynamic_governor(rates, replace(a, duty_cycle=duty)).total

    if governor_total(0.0) >= target:
        return 0.0
    if governor_total(1.0) <= target:
        return None

    low, high = 0.0, 1.0
    for _ in range(60):
        mid = (low + high) / 2.0
        if governor_total(mid) < target:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


# --------------------------------------------------------------------------------------
# Reconciliation against the original projection
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """This model's figures set beside the original experiment's, with the gap.

    `governor_lambda_only` exists because the gap turns out to be explainable rather
    than mysterious: the original projection's two percentage claims both fall out of
    this model to within a rounding step, but only when the controller is costed the way
    its brief implies -- as the Lambda alone, with the CloudWatch calls that Lambda makes
    left uncounted.
    """

    model_always_on: float
    model_static: float
    model_governor: float
    governor_lambda_only: float
    compute_only_always_on_730h: float
    compute_only_always_on_744h: float


def reconcile(rates: Rates, a: Assumptions) -> Reconciliation:
    fleet, static, governor = all_scenarios(rates, a)
    return Reconciliation(
        model_always_on=fleet.total,
        model_static=static.total,
        model_governor=governor.total,
        governor_lambda_only=governor.total - governor.controller_metrics,
        compute_only_always_on_730h=a.fleet_size * 730.0 * rates.instance_hour,
        compute_only_always_on_744h=a.fleet_size * 744.0 * rates.instance_hour,
    )


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _pct(value: float) -> str:
    return f"{value * 100.0:.1f} %"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], indent: str = "  ") -> str:
    """Render a fixed-width table: first column left-aligned, the rest right-aligned."""
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row, strict=True)]

    def line(cells: Sequence[str]) -> str:
        head = cells[0].ljust(widths[0])
        tail = "  ".join(c.rjust(w) for c, w in zip(cells[1:], widths[1:], strict=True))
        return f"{indent}{head}  {tail}".rstrip()

    rule = f"{indent}{'-' * (sum(widths) + 2 * len(widths))}"
    return "\n".join([line(headers), rule, *(line(r) for r in rows), rule])


_SCENARIO_SHAPES: Final = {
    "always-on": "3 x t3.micro, 24/7",
    "static warm pool": "1 in service + 2 stopped",
    "dynamic governor": "1 in service, pool at zero",
}


def render_report(rates: Rates, a: Assumptions) -> str:
    """The full stdout report: assumptions, scenarios, deltas, reconciliation, sweeps."""
    fleet, static, governor = all_scenarios(rates, a)
    out: list[str] = []
    rule = "=" * 88

    out.append(rule)
    out.append(f" Projected monthly cost -- {rates.region} ({rates.region_name})")
    out.append(rule)
    out.append("")
    for notice_line in PROJECTION_NOTICE.splitlines():
        out.append(f"  {notice_line}")
    out.append("")

    out.append("Assumptions")
    ipv4_on = a.charge_public_ipv4
    ipv4_note = "one per running instance" if ipv4_on else "no public address per instance"
    out.append(
        _table(
            ["input", "value", "note"],
            [
                ["hours per month", f"{a.hours_per_month:,.0f}", "AWS convention: 365 x 24 / 12"],
                ["fleet size", f"{a.fleet_size}", "instances the always-on arm runs 24/7"],
                ["root volume", f"{a.volume_gb:,.0f} GB", "gp3, one per instance"],
                ["governor duty cycle", _pct(a.duty_cycle), "share of month above scale-in"],
                ["  of which MITIGATE", _pct(a.mitigate_share), "2nd instance actually in service"],
                [
                    "poll interval",
                    f"{a.poll_seconds:,.0f} s",
                    f"{a.invocations:,.0f} invocations/month",
                ],
                [
                    "Lambda sizing",
                    f"{a.lambda_memory_mb:,.0f} MB",
                    f"{a.lambda_duration_seconds:.2f} s billed per invocation",
                ],
                ["public IPv4", "charged" if ipv4_on else "not charged", ipv4_note],
            ],
        )
    )
    out.append("")

    out.append("Monthly cost by scenario")
    headers = ["scenario", "compute", "storage", "control", "total"]
    rows = [
        [
            f"{b.scenario} ({_SCENARIO_SHAPES[b.scenario]})",
            _money(b.compute),
            _money(b.storage),
            _money(b.controller),
            _money(b.total),
        ]
        for b in (fleet, static, governor)
    ]
    if a.charge_public_ipv4:
        headers.insert(4, "IPv4")
        for row, b in zip(rows, (fleet, static, governor), strict=True):
            row.insert(4, _money(b.public_ipv4))
    out.append(_table(headers, rows))
    out.append("")

    out.append("Deltas")
    out.append(
        _table(
            ["comparison", "saving", "reduction"],
            [
                [
                    "dynamic governor vs always-on",
                    _money(fleet.total - governor.total),
                    _pct(reduction(governor.total, fleet.total)),
                ],
                [
                    "dynamic governor vs static warm pool",
                    _money(static.total - governor.total),
                    _pct(reduction(governor.total, static.total)),
                ],
                [
                    "static warm pool vs always-on",
                    _money(fleet.total - static.total),
                    _pct(reduction(static.total, fleet.total)),
                ],
            ],
        )
    )
    out.append("")

    phantom = phantom_storage(rates, a)
    out.append("Where the governor's margin over a static pool comes from")
    out.append(
        _table(
            ["line item", "amount"],
            [
                [
                    f"phantom storage avoided ({a.fleet_size - 1} stopped volumes x "
                    f"{a.volume_gb:,.0f} GB)",
                    f"-{_money(phantom)}",
                ],
                [
                    "extra compute for the duty window",
                    f"+{_money(governor.compute - static.compute)}",
                ],
                [
                    "extra storage during PRE_WARM / MITIGATE",
                    f"+{_money(governor.storage - (static.storage - phantom))}",
                ],
                ["controller: Lambda", f"+{_money(governor.controller_lambda)}"],
                ["controller: CloudWatch observability", f"+{_money(governor.controller_metrics)}"],
                ["net", f"-{_money(static.total - governor.total)}"],
            ],
        )
    )
    controller_ratio = (
        governor.controller_metrics / governor.controller_lambda
        if governor.controller_lambda > 0
        else float("nan")
    )
    out.append(
        f"  The controller costs {_money(governor.controller)}/month -- "
        f"{_pct(governor.controller / governor.total)} of the governor's bill, a rounding error\n"
        "  against the fleet, exactly as claimed. The surprise is its shape: CloudWatch costs\n"
        f"  {controller_ratio:.1f}x what the Lambda does. GetMetricData bills per metric "
        "requested and is one of\n"
        "  the few CloudWatch APIs the free tier excludes, so the poll interval -- not the\n"
        "  function -- sets the control plane's price."
    )
    out.append("")

    out.append(_render_reconciliation(rates, a))
    out.append("")
    out.append(_render_duty_sweep(rates, a))
    out.append("")
    out.append(_render_volume_sweep(rates, a))
    out.append("")
    out.append("  Reminder: projections from list prices. Not a bill. Not measured spend.")
    return "\n".join(out)


def _render_reconciliation(rates: Rates, a: Assumptions) -> str:
    r = reconcile(rates, a)
    excl = r.governor_lambda_only
    governor_metrics = dynamic_governor(rates, a).controller_metrics
    cut_vs_fleet = reduction(r.model_governor, r.model_always_on)
    cut_vs_static = reduction(r.model_governor, r.model_static)
    out = ["Reconciliation against the original projection"]
    out.append(
        _table(
            ["figure", "original", "this model", "gap"],
            [
                [
                    "always-on, monthly",
                    _money(ORIGINAL_ALWAYS_ON_USD),
                    _money(r.model_always_on),
                    f"+{_money(r.model_always_on - ORIGINAL_ALWAYS_ON_USD)}",
                ],
                [
                    "dynamic governor, monthly",
                    _money(ORIGINAL_GOVERNOR_USD),
                    _money(r.model_governor),
                    f"+{_money(r.model_governor - ORIGINAL_GOVERNOR_USD)}",
                ],
                [
                    "cut vs always-on",
                    _pct(ORIGINAL_CUT_VS_ALWAYS_ON),
                    _pct(cut_vs_fleet),
                    f"{(cut_vs_fleet - ORIGINAL_CUT_VS_ALWAYS_ON) * 100:+.1f} pp",
                ],
                [
                    "cut vs static warm pool",
                    _pct(ORIGINAL_CUT_VS_STATIC),
                    _pct(cut_vs_static),
                    f"{(cut_vs_static - ORIGINAL_CUT_VS_STATIC) * 100:+.1f} pp",
                ],
            ],
        )
    )
    out.append("")
    out.append("  No calibration constant was added to close that gap. What explains it:")
    out.append("")
    out.append(
        f"  1. The original's absolute dollars are a compute-only bill. "
        f"{a.fleet_size} x t3.micro with no\n"
        f"     storage is {_money(r.compute_only_always_on_730h)} at "
        f"{a.hours_per_month:,.0f} h/month and {_money(r.compute_only_always_on_744h)} at 744 h "
        f"(a 31-day month) -- and the\n"
        f"     744-hour reading lands within "
        f"{_money(abs(r.compute_only_always_on_744h - ORIGINAL_ALWAYS_ON_USD))} of the reported "
        f"{_money(ORIGINAL_ALWAYS_ON_USD)}. EBS is simply missing\n"
        "     from that arm, which is ironic: phantom storage is the mechanism the governor's\n"
        "     advantage rests on."
    )
    out.append("")
    out.append(
        "  2. That reading cannot be the whole story, because on a compute-only basis a\n"
        "     static warm pool is *cheaper* than the governor (one instance either way,\n"
        "     minus the duty window), so 'cheaper than a static pool' would be impossible.\n"
        "     Storage must have been counted for one comparison and not the other. The\n"
        "     original assumption set is not fully recoverable from the numbers it reports."
    )
    out.append("")
    out.append(
        f"  3. The percentages, unlike the dollars, reproduce almost exactly -- provided the\n"
        f"     controller is costed as the Lambda alone. At {_money(excl)}/month (Lambda counted,\n"
        f"     the CloudWatch calls it makes not) this model gives "
        f"{_pct(reduction(excl, r.model_always_on))} against always-on\n"
        f"     and {_pct(reduction(excl, r.model_static))} against a static pool, against the "
        f"original's {_pct(ORIGINAL_CUT_VS_ALWAYS_ON)} and {_pct(ORIGINAL_CUT_VS_STATIC)}.\n"
        f"     Both land within a rounding step. Adding the GetMetricData charge the Lambda\n"
        f"     incurs -- {_money(governor_metrics)}/month, the largest control-plane line -- "
        f"moves them to\n"
        f"     {_pct(cut_vs_fleet)} and {_pct(cut_vs_static)}."
    )
    out.append("")
    out.append(
        "  Takeaway: the original's ratio claims hold up under independently sourced rates.\n"
        "  Its absolute dollars do not, and the direction of the error is against the\n"
        "  governor's own story -- it under-counted the storage that makes its case."
    )
    return "\n".join(out)


def _observed_duty_cycle() -> float:
    return OBSERVED_ELEVATED_SAMPLES / OBSERVED_WINDOW_SAMPLES


def _render_duty_sweep(rates: Rates, a: Assumptions) -> str:
    """Sensitivity to the duty cycle -- the assumption the result leans on hardest."""
    fleet = always_on(rates, a).total
    static = static_warm_pool(rates, a).total
    observed = _observed_duty_cycle()
    points: list[tuple[float, str]] = [
        (0.0, "never leaves cost-saving"),
        (0.02, ""),
        (a.duty_cycle, "model default"),
        (0.10, ""),
        (observed, "the captured load-test window"),
        (0.25, ""),
        (0.50, "half the month under load"),
    ]
    rows = []
    for duty, label in sorted(points):
        total = dynamic_governor(rates, replace(a, duty_cycle=duty)).total
        rows.append(
            [
                _pct(duty),
                _money(total),
                _pct(reduction(total, fleet)),
                _pct(reduction(total, static)),
                label,
            ]
        )

    out = ["Sensitivity 1 -- duty cycle (share of the month above the scale-in threshold)"]
    out.append(_table(["duty cycle", "governor/mo", "vs always-on", "vs static", "note"], rows))
    crossover = break_even_duty_cycle(rates, a)
    if crossover is None:
        out.append("  The governor beats a static warm pool at every duty cycle in [0, 100 %].")
    else:
        out.append(
            f"  Break-even against a static warm pool: {_pct(crossover)} duty cycle. Past that\n"
            "  the governor's extra compute outweighs the phantom storage it avoids, and a\n"
            f"  static pool is the cheaper design. The captured window sits at {_pct(observed)}\n"
            "  -- but it is a deliberate load test, not a month: a ceiling, not an estimate."
        )
    out.append(
        "  Against always-on the ordering never reverses: one instance plus a duty window is\n"
        "  cheaper than three instances at any duty cycle."
    )
    return "\n".join(out)


def _render_volume_sweep(rates: Rates, a: Assumptions) -> str:
    """Sensitivity to root volume size -- the axis phantom storage scales on."""
    rows = []
    for volume in (8.0, 16.0, 30.0, 50.0, 100.0):
        scoped = replace(a, volume_gb=volume)
        fleet = always_on(rates, scoped).total
        static = static_warm_pool(rates, scoped).total
        governor = dynamic_governor(rates, scoped).total
        rows.append(
            [
                f"{volume:,.0f} GB",
                _money(fleet),
                _money(static),
                _money(governor),
                _pct(reduction(governor, fleet)),
                _pct(reduction(governor, static)),
            ]
        )

    out = ["Sensitivity 2 -- root volume size (per instance, gp3)"]
    out.append(
        _table(
            ["volume", "always-on", "static pool", "governor", "vs always-on", "vs static"],
            rows,
        )
    )
    out.append(
        "  The headline saving against always-on barely moves with volume size -- compute\n"
        "  dominates all three arms equally. The margin over a static warm pool does almost\n"
        "  nothing else: it IS the phantom storage, so it grows in lockstep with the disks.\n"
        "  At 8 GB roots the governor's advantage over a static pool is thin and real; at\n"
        "  100 GB roots it is the entire argument. Quoting one number without the volume\n"
        "  size attached is quoting half a result."
    )
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# Chart
# --------------------------------------------------------------------------------------

_SURFACE: Final = "#fcfcfb"
_INK: Final = "#0b0b0b"
_INK_SOFT: Final = "#52514e"
_INK_MUTED: Final = "#87857e"
_GRID: Final = "#e4e3df"
_COMPUTE: Final = "#2a78d6"
_STORAGE: Final = "#eb6834"
_CONTROL: Final = "#1baf7a"


def write_chart(rates: Rates, a: Assumptions, path: Path) -> Path:
    """Render the comparison chart to `path` and return it.

    `savefig(metadata={"Date": None})` keeps the PNG byte-identical across runs -- the
    creation timestamp is the only non-deterministic thing matplotlib writes, and a
    figure that changes checksum every time it is regenerated is a figure nobody can
    review in a diff.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["DejaVu Sans"],
            "text.parse_math": False,
            "figure.facecolor": _SURFACE,
            "axes.facecolor": _SURFACE,
            "axes.edgecolor": _GRID,
            "text.color": _INK,
            "axes.labelcolor": _INK_SOFT,
            "xtick.color": _INK_SOFT,
            "ytick.color": _INK_SOFT,
        }
    )

    fleet, static, governor = all_scenarios(rates, a)
    figure, (left, right) = plt.subplots(
        1, 2, figsize=(13.0, 6.0), dpi=150, gridspec_kw={"width_ratios": [1.30, 1.0]}
    )

    _draw_stack(left, (fleet, static, governor), rates, a)
    _draw_duty_sweep(right, rates, a)

    figure.suptitle(
        f"Projected monthly cost of three warm-pool strategies \u00b7 {rates.region}",
        fontsize=14,
        fontweight="bold",
        color=_INK,
        x=0.012,
        y=0.975,
        ha="left",
    )
    figure.text(
        0.012,
        0.923,
        "Projections from published list prices, not metered spend. No AWS bill was captured.",
        fontsize=9.5,
        color=_INK_SOFT,
        ha="left",
    )
    figure.text(
        0.012,
        0.018,
        f"{a.fleet_size} x t3.micro  ·  {a.volume_gb:,.0f} GB gp3 roots  ·  "
        f"{a.hours_per_month:,.0f} h/month  ·  governor duty cycle {_pct(a.duty_cycle)}  ·  "
        "rates and sources in finops/pricing.json",
        fontsize=8.5,
        color=_INK_MUTED,
        ha="left",
    )

    figure.tight_layout(rect=(0.0, 0.055, 1.0, 0.905))
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=150, facecolor=_SURFACE, metadata={"Date": None})
    plt.close(figure)
    return path


def _draw_stack(axes, scenarios, rates: Rates, a: Assumptions) -> None:
    """Stacked bars: one per strategy, split into the three cost behaviours.

    The stack, not a grouped bar, is the right form here because the question is not
    "which is bigger" -- it is "which *part* is bigger", and specifically whether the
    static pool's storage block survives into the governor's bar. A grouped chart would
    show the same six numbers and answer none of that.
    """
    labels = [
        "always-on\n3 in service",
        "static warm pool\n1 in service + 2 stopped",
        "dynamic governor\n1 in service, pool at zero",
    ]
    x = list(range(len(scenarios)))
    compute = [b.compute for b in scenarios]
    storage = [b.storage for b in scenarios]
    control = [b.controller for b in scenarios]
    ceiling = scenarios[0].total * 1.46

    # edgecolor=surface at 1pt is the 2px gap between stacked fills at 150 dpi: the
    # segments read as separate blocks rather than one bar with a colour change.
    bar = {"width": 0.54, "edgecolor": _SURFACE, "linewidth": 1.0}
    axes.bar(x, compute, label="compute (instance-hours)", color=_COMPUTE, **bar)
    axes.bar(x, storage, bottom=compute, label="storage (EBS GB-months)", color=_STORAGE, **bar)
    axes.bar(
        x,
        control,
        bottom=[c + s for c, s in zip(compute, storage, strict=True)],
        label="control plane (Lambda + CloudWatch)",
        color=_CONTROL,
        **bar,
    )

    # A segment needs roughly 4 % of the axis to hold a 9 pt label without clipping;
    # anything thinner gets a callout instead of a label crammed inside it.
    legible = ceiling * 0.04
    for index, b in enumerate(scenarios):
        axes.text(
            index,
            b.total + ceiling * 0.018,
            _money(b.total),
            ha="center",
            va="bottom",
            fontsize=12.5,
            fontweight="bold",
            color=_INK,
        )
        axes.text(
            index,
            b.compute / 2,
            _money(b.compute),
            ha="center",
            va="center",
            fontsize=9.5,
            color="#ffffff",
        )
        if b.storage >= legible:
            axes.text(
                index,
                b.compute + b.storage / 2,
                _money(b.storage),
                ha="center",
                va="center",
                fontsize=9.5,
                color=_INK,
            )

    static = scenarios[1]
    axes.annotate(
        f"{_money(phantom_storage(rates, a))} of that storage is phantom:\n"
        f"{a.fleet_size - 1} stopped instances whose volumes\nbill every hour anyway",
        xy=(0.73, static.compute + static.storage / 2),
        xytext=(0.40, ceiling * 0.93),
        fontsize=9.5,
        color=_INK_SOFT,
        ha="left",
        va="top",
        arrowprops={"arrowstyle": "-", "color": _INK_MUTED, "linewidth": 1.0, "shrinkB": 3},
    )

    governor = scenarios[2]
    axes.annotate(
        f"storage {_money(governor.storage)}: the pool drains to zero,\n"
        f"so the phantom volumes are gone, not idle\n"
        f"control plane {_money(governor.controller)} "
        f"({_pct(governor.controller / governor.total)} of the bill),\n"
        f"of which {_money(governor.controller_metrics)} is CloudWatch, not Lambda",
        xy=(1.74, governor.compute + governor.storage + governor.controller / 2),
        xytext=(1.02, ceiling * 0.62),
        fontsize=9.5,
        color=_INK_SOFT,
        ha="left",
        va="top",
        arrowprops={"arrowstyle": "-", "color": _INK_MUTED, "linewidth": 1.0, "shrinkB": 3},
    )

    axes.set_xticks(x)
    axes.set_xticklabels(labels, fontsize=9.5, color=_INK)
    axes.set_ylabel("USD per month (projected)", fontsize=10)
    axes.set_ylim(0, ceiling)
    axes.set_axisbelow(True)
    axes.grid(axis="y", color=_GRID, linewidth=0.8)
    axes.tick_params(axis="x", length=0)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    # The legend sits below the tick labels so the whole upper half of the plot is free
    # for callouts; inside the axes it was fighting the annotations for the same space.
    axes.legend(
        frameon=False,
        fontsize=9.5,
        labelcolor=_INK_SOFT,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.13),
        ncols=3,
        columnspacing=1.6,
        handlelength=1.3,
    )


def _draw_duty_sweep(axes, rates: Rates, a: Assumptions) -> None:
    """The governor's cost against the one assumption it is most sensitive to."""
    static = static_warm_pool(rates, a).total
    steps = [i / 100.0 for i in range(0, 51)]
    totals = [dynamic_governor(rates, replace(a, duty_cycle=d)).total for d in steps]
    floor = min(totals) - 0.45
    ceiling = max(totals) + 0.30

    axes.plot([d * 100 for d in steps], totals, color=_COMPUTE, linewidth=2.0)
    axes.axhline(static, color=_STORAGE, linewidth=2.0, linestyle=(0, (5, 3)))
    # Below the reference line, not above it: past the crossover the governor's own line
    # occupies the space above, and a label a line passes through is a label nobody reads.
    axes.text(
        48.5,
        static - (ceiling - floor) * 0.025,
        f"static warm pool, flat at {_money(static)}",
        fontsize=9.5,
        color=_STORAGE,
        ha="right",
        va="top",
    )
    mid = 36
    axes.text(
        mid,
        totals[mid] + (ceiling - floor) * 0.03,
        "dynamic governor",
        fontsize=9.5,
        color=_COMPUTE,
        ha="center",
        va="bottom",
    )

    default_total = dynamic_governor(rates, a).total
    axes.plot([a.duty_cycle * 100], [default_total], marker="o", markersize=8, color=_COMPUTE)
    axes.annotate(
        f"model default: {_pct(a.duty_cycle)} -> {_money(default_total)}",
        xy=(a.duty_cycle * 100 + 0.8, default_total),
        xytext=(a.duty_cycle * 100 + 5.5, floor + (ceiling - floor) * 0.10),
        fontsize=9.5,
        color=_INK_SOFT,
        ha="left",
        va="center",
        arrowprops={"arrowstyle": "-", "color": _INK_MUTED, "linewidth": 1.0, "shrinkB": 3},
    )

    crossover = break_even_duty_cycle(rates, a)
    if crossover is not None and crossover <= 0.50:
        axes.plot([crossover * 100], [static], marker="o", markersize=8, color=_INK, zorder=5)
        axes.annotate(
            f"break-even at {_pct(crossover)}: past here\nthe static pool is cheaper",
            xy=(crossover * 100, static + (ceiling - floor) * 0.03),
            xytext=(2.0, ceiling - (ceiling - floor) * 0.04),
            fontsize=9.5,
            color=_INK,
            ha="left",
            va="top",
            arrowprops={"arrowstyle": "-", "color": _INK_MUTED, "linewidth": 1.0, "shrinkB": 3},
        )

    axes.set_title(
        "Sensitivity: the duty-cycle assumption",
        fontsize=11.5,
        color=_INK,
        loc="left",
        fontweight="bold",
        pad=10,
    )
    axes.set_xlabel("share of the month above the scale-in threshold (%)", fontsize=10)
    axes.set_ylabel("USD per month (projected)", fontsize=10)
    axes.set_xlim(0, 50)
    axes.set_ylim(floor, ceiling)
    axes.set_axisbelow(True)
    axes.grid(color=_GRID, linewidth=0.8)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

_DEFAULT_CHART: Final = Path(__file__).resolve().parent.parent / "results" / "finops.png"


def _parser() -> argparse.ArgumentParser:
    defaults = Assumptions()
    parser = argparse.ArgumentParser(
        prog="python -m finops.cost_model",
        description=(
            "Project the monthly cost of three warm-pool strategies from AWS list prices. "
            "Output is a projection, not metered spend."
        ),
    )
    parser.add_argument("--duty-cycle", type=float, default=defaults.duty_cycle)
    parser.add_argument("--mitigate-share", type=float, default=defaults.mitigate_share)
    parser.add_argument("--volume-gb", type=float, default=defaults.volume_gb)
    parser.add_argument("--hours", type=float, default=defaults.hours_per_month)
    parser.add_argument("--poll-seconds", type=float, default=defaults.poll_seconds)
    parser.add_argument(
        "--public-ipv4",
        action="store_true",
        help="charge an in-use public IPv4 address per running instance",
    )
    parser.add_argument("--chart", type=Path, default=_DEFAULT_CHART)
    parser.add_argument("--no-chart", action="store_true", help="print the tables only")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    rates = Rates.load()
    assumptions = Assumptions(
        hours_per_month=args.hours,
        volume_gb=args.volume_gb,
        duty_cycle=args.duty_cycle,
        mitigate_share=args.mitigate_share,
        poll_seconds=args.poll_seconds,
        charge_public_ipv4=args.public_ipv4,
    )

    print(render_report(rates, assumptions))

    if args.no_chart:
        return 0
    try:
        written = write_chart(rates, assumptions, args.chart)
    except ImportError:
        print(
            "\nmatplotlib is not installed, so the chart was not regenerated.\n"
            "Install the dev extras, or re-run with --no-chart for the tables alone.",
            file=sys.stderr,
        )
        return 1
    print(f"\nChart written to {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
