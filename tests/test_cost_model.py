"""Tests for the cost model.

These are as much documentation as verification: the assertions below are where the
model's claims are written down precisely enough to fail. In particular, the reproduction
tests pin *which* of the original projection's claims this model reproduces and which it
does not, so that a future edit cannot quietly turn a documented gap into a silent
agreement.
"""

from __future__ import annotations

import json
from dataclasses import replace
from itertools import pairwise

import pytest

from finops.cost_model import (
    ORIGINAL_ALWAYS_ON_USD,
    ORIGINAL_CUT_VS_ALWAYS_ON,
    ORIGINAL_CUT_VS_STATIC,
    ORIGINAL_GOVERNOR_USD,
    PROJECTION_NOTICE,
    Assumptions,
    Rates,
    all_scenarios,
    always_on,
    break_even_duty_cycle,
    dynamic_governor,
    main,
    phantom_storage,
    reconcile,
    reduction,
    render_report,
    static_warm_pool,
    write_chart,
)


@pytest.fixture(scope="module")
def rates() -> Rates:
    return Rates.load()


@pytest.fixture
def assumptions() -> Assumptions:
    return Assumptions()


# --------------------------------------------------------------------------------------
# The rate sheet
# --------------------------------------------------------------------------------------


def test_every_published_rate_carries_a_citation(rates: Rates) -> None:
    """A rate with no source and no as-of date is a guess. Loading must reject one."""
    from importlib import resources

    document = json.loads(resources.files("finops").joinpath("pricing.json").read_text("utf-8"))
    for name, entry in document["rates"].items():
        assert entry["source"].startswith("https://aws.amazon.com/"), name
        assert entry["as_of"], name
        assert entry["unit"], name
        assert isinstance(entry["value"], int | float), name


def test_uncited_rate_is_rejected() -> None:
    document = {
        "region": "ap-south-1",
        "region_name": "Asia Pacific (Mumbai)",
        "rates": {
            "ec2_on_demand_t3_micro": {"value": 0.0112, "unit": "USD/h", "as_of": "2026-01-01"}
        },
    }
    with pytest.raises(ValueError, match="missing source"):
        Rates.from_document(document)


def test_missing_rate_is_named_in_the_error() -> None:
    document = {
        "region": "ap-south-1",
        "region_name": "Asia Pacific (Mumbai)",
        "rates": {
            "ec2_on_demand_t3_micro": {
                "value": 0.0112,
                "unit": "USD/h",
                "as_of": "2026-01-01",
                "source": "https://aws.amazon.com/ec2/pricing/on-demand/",
            }
        },
    }
    with pytest.raises(ValueError, match="ebs_gp3_storage"):
        Rates.from_document(document)


def test_rates_are_the_mumbai_sheet(rates: Rates) -> None:
    assert rates.region == "ap-south-1"
    assert rates.instance_hour == pytest.approx(0.0112)
    assert rates.gp3_gb_month == pytest.approx(0.0912)


# --------------------------------------------------------------------------------------
# Scenario mechanics
# --------------------------------------------------------------------------------------


def test_scenarios_rank_as_the_design_predicts(rates: Rates, assumptions: Assumptions) -> None:
    fleet, static, governor = all_scenarios(rates, assumptions)
    assert fleet.total > static.total > governor.total


def test_static_pool_saves_compute_but_not_one_cent_of_storage(
    rates: Rates, assumptions: Assumptions
) -> None:
    """The defining property of a permanently stopped warm pool.

    Stopping two of three instances removes two instances' worth of compute and exactly
    nothing from the storage line, because a stopped instance's root volume bills
    identically to a running one's.
    """
    fleet = always_on(rates, assumptions)
    static = static_warm_pool(rates, assumptions)
    assert static.storage == pytest.approx(fleet.storage)
    assert static.compute == pytest.approx(fleet.compute / assumptions.fleet_size)


def test_draining_the_pool_removes_the_phantom_volumes(
    rates: Rates, assumptions: Assumptions
) -> None:
    """At a zero duty cycle the governor never leaves COST_SAVING, where
    `warm_pool_min_size = 0`. Exactly one volume should be billing."""
    idle = dynamic_governor(rates, replace(assumptions, duty_cycle=0.0))
    one_volume = assumptions.volume_gb * rates.gp3_gb_month
    assert idle.storage == pytest.approx(one_volume)
    assert idle.compute == pytest.approx(assumptions.hours_per_month * rates.instance_hour)


def test_phantom_storage_is_what_separates_the_two_pool_strategies(
    rates: Rates, assumptions: Assumptions
) -> None:
    static = static_warm_pool(rates, assumptions)
    idle_governor = dynamic_governor(rates, replace(assumptions, duty_cycle=0.0))
    assert static.storage - idle_governor.storage == pytest.approx(
        phantom_storage(rates, assumptions)
    )


def test_mitigate_is_the_only_phase_that_adds_running_capacity(
    rates: Rates, assumptions: Assumptions
) -> None:
    """PRE_WARM boots instances into the pool *stopped*: storage moves, compute does not."""
    pre_warm_only = replace(assumptions, duty_cycle=0.20, mitigate_share=0.0)
    idle = dynamic_governor(rates, replace(assumptions, duty_cycle=0.0))
    warmed = dynamic_governor(rates, pre_warm_only)
    assert warmed.compute == pytest.approx(idle.compute)
    assert warmed.storage > idle.storage


def test_controller_is_charged_for_every_poll(rates: Rates, assumptions: Assumptions) -> None:
    assert assumptions.invocations == pytest.approx(43_800.0)
    governor = dynamic_governor(rates, assumptions)
    assert governor.controller > 0.0


def test_controller_is_a_rounding_error_but_is_mostly_cloudwatch(
    rates: Rates, assumptions: Assumptions
) -> None:
    """Both halves of the claim the report makes about the control plane.

    It really is negligible against the fleet -- and it is not shaped the way anyone
    guesses, because `GetMetricData` bills per metric requested and the CloudWatch free
    tier excludes it, while the Lambda's own 43,800 invocations cost about a dime.
    """
    governor = dynamic_governor(rates, assumptions)
    assert governor.controller / governor.total < 0.10
    assert governor.controller_metrics > 4 * governor.controller_lambda


def test_control_plane_scales_with_the_poll_rate(rates: Rates, assumptions: Assumptions) -> None:
    fast = dynamic_governor(rates, assumptions).controller
    slow = dynamic_governor(rates, replace(assumptions, poll_seconds=300.0)).controller
    assert slow == pytest.approx(fast / 5.0, rel=1e-6)


def test_public_ipv4_is_off_by_default_and_material_when_on(
    rates: Rates, assumptions: Assumptions
) -> None:
    assert always_on(rates, assumptions).public_ipv4 == 0.0
    charged = always_on(rates, replace(assumptions, charge_public_ipv4=True))
    assert charged.public_ipv4 == pytest.approx(3 * 730.0 * 0.005)


# --------------------------------------------------------------------------------------
# Reproducing the original projection
# --------------------------------------------------------------------------------------
#
# The original projection reported: always-on $24.95/month, governor $8.66/month, a 65 %
# cut against always-on and 11 % against a static warm pool. This model does NOT hit the
# two dollar figures, and the tests below pin that gap open rather than papering over it.
# What it does reproduce, to within a rounding step, is both percentage claims -- and
# only under one specific reading of what "the governor costs" includes.


def test_percentage_claims_reproduce_when_the_controller_is_costed_as_lambda_alone(
    rates: Rates, assumptions: Assumptions
) -> None:
    """The assumption set: `ap-south-1` list prices, 730 h/month, 8 GB gp3 roots, a 5 %
    duty cycle of which 40 % is MITIGATE, no public IPv4, and the controller counted as
    the Lambda only -- not the CloudWatch API calls the Lambda makes.

    Under exactly that set both original percentages fall out within one point.
    """
    reconciliation = reconcile(rates, assumptions)
    lambda_only = reconciliation.governor_lambda_only

    vs_fleet = reduction(lambda_only, reconciliation.model_always_on)
    vs_static = reduction(lambda_only, reconciliation.model_static)

    assert vs_fleet == pytest.approx(ORIGINAL_CUT_VS_ALWAYS_ON, abs=0.01)
    assert vs_static == pytest.approx(ORIGINAL_CUT_VS_STATIC, abs=0.01)


def test_counting_the_whole_control_plane_costs_the_headline_a_few_points(
    rates: Rates, assumptions: Assumptions
) -> None:
    """The honest figures, and the reason they differ from the ones above."""
    fleet, static, governor = all_scenarios(rates, assumptions)
    assert reduction(governor.total, fleet.total) == pytest.approx(0.638, abs=0.005)
    assert reduction(governor.total, static.total) == pytest.approx(0.067, abs=0.005)


def test_the_dollar_gap_is_a_missing_storage_line_not_a_pricing_error(
    rates: Rates, assumptions: Assumptions
) -> None:
    """Where the original's $24.95 most plausibly comes from.

    Three `t3.micro` with no EBS at all, over a 744-hour (31-day) month, is $25.00 --
    within five cents of the reported figure. Adding the storage those instances cannot
    run without is what moves this model to $26.72.
    """
    reconciliation = reconcile(rates, assumptions)
    assert reconciliation.compute_only_always_on_744h == pytest.approx(
        ORIGINAL_ALWAYS_ON_USD, abs=0.10
    )
    gap = reconciliation.model_always_on - ORIGINAL_ALWAYS_ON_USD
    storage_line = always_on(rates, assumptions).storage
    assert 0.0 < gap < storage_line


def test_the_original_governor_figure_is_below_this_model_at_every_duty_cycle(
    rates: Rates, assumptions: Assumptions
) -> None:
    """No duty cycle reconciles $8.66, so no amount of tuning would close that gap.

    Worth asserting because it rules out the tempting move: the difference is not a
    duty-cycle disagreement, it is a line item the original did not bill.
    """
    floor = dynamic_governor(rates, replace(assumptions, duty_cycle=0.0)).total
    assert floor > ORIGINAL_GOVERNOR_USD


def test_headline_figures_are_pinned(rates: Rates, assumptions: Assumptions) -> None:
    """A regression guard on the numbers the README quotes."""
    fleet, static, governor = all_scenarios(rates, assumptions)
    assert round(fleet.total, 2) == 26.72
    assert round(static.total, 2) == 10.36
    assert round(governor.total, 2) == 9.67


# --------------------------------------------------------------------------------------
# Sensitivity
# --------------------------------------------------------------------------------------


def test_governor_cost_rises_monotonically_with_duty_cycle(
    rates: Rates, assumptions: Assumptions
) -> None:
    totals = [
        dynamic_governor(rates, replace(assumptions, duty_cycle=d / 20)).total for d in range(21)
    ]
    assert all(b > a for a, b in pairwise(totals))


def test_governor_beats_always_on_at_every_duty_cycle(
    rates: Rates, assumptions: Assumptions
) -> None:
    fleet = always_on(rates, assumptions).total
    worst = dynamic_governor(rates, replace(assumptions, duty_cycle=1.0)).total
    assert worst < fleet


def test_break_even_against_a_static_pool_is_where_the_two_curves_meet(
    rates: Rates, assumptions: Assumptions
) -> None:
    crossover = break_even_duty_cycle(rates, assumptions)
    assert crossover is not None
    static = static_warm_pool(rates, assumptions).total
    at_crossover = dynamic_governor(rates, replace(assumptions, duty_cycle=crossover)).total
    assert at_crossover == pytest.approx(static, abs=0.01)
    assert dynamic_governor(rates, replace(assumptions, duty_cycle=crossover - 0.05)).total < static
    assert dynamic_governor(rates, replace(assumptions, duty_cycle=crossover + 0.05)).total > static


def test_big_volumes_remove_the_break_even_entirely(
    rates: Rates, assumptions: Assumptions
) -> None:
    """Phantom storage grows with disk size; past a point the static pool never wins."""
    assert break_even_duty_cycle(rates, replace(assumptions, volume_gb=200.0)) is None


def test_margin_over_static_grows_with_volume_but_margin_over_always_on_does_not(
    rates: Rates, assumptions: Assumptions
) -> None:
    """The single most important sensitivity result, and the reason the 11 % figure
    should never be quoted without the volume size attached."""
    against_static = []
    against_fleet = []
    for volume in (8.0, 16.0, 30.0, 50.0, 100.0):
        scoped = replace(assumptions, volume_gb=volume)
        governor = dynamic_governor(rates, scoped).total
        against_static.append(reduction(governor, static_warm_pool(rates, scoped).total))
        against_fleet.append(reduction(governor, always_on(rates, scoped).total))

    assert all(b > a for a, b in pairwise(against_static))
    assert max(against_fleet) - min(against_fleet) < 0.01


# --------------------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["duty_cycle", "mitigate_share"])
@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_fractions_must_be_fractions(field: str, value: float) -> None:
    with pytest.raises(ValueError, match="must lie in"):
        Assumptions(**{field: value})


@pytest.mark.parametrize("field", ["hours_per_month", "volume_gb", "poll_seconds"])
def test_quantities_must_be_positive(field: str) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        Assumptions(**{field: 0.0})


def test_reduction_needs_a_positive_baseline() -> None:
    with pytest.raises(ValueError, match="baseline must be positive"):
        reduction(5.0, 0.0)


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------


def test_report_cannot_be_read_without_meeting_the_projection_caveat(
    rates: Rates, assumptions: Assumptions
) -> None:
    """The one thing this report must never let a skim-reader miss."""
    report = render_report(rates, assumptions)
    assert "NOT METERED SPEND" in report
    assert PROJECTION_NOTICE.splitlines()[0] in report
    assert report.rstrip().endswith("Not a bill. Not measured spend.")


def test_report_lines_fit_a_terminal(rates: Rates, assumptions: Assumptions) -> None:
    """The report is meant to be pasted into a README code block, so it has a width."""
    assert max(len(line) for line in render_report(rates, assumptions).splitlines()) <= 88


def test_cli_prints_the_tables_without_touching_the_filesystem(capsys) -> None:
    assert main(["--no-chart"]) == 0
    assert "NOT METERED SPEND" in capsys.readouterr().out


def test_chart_regenerates_byte_for_byte(rates: Rates, assumptions: Assumptions, tmp_path) -> None:
    """Determinism is what makes the figure reviewable in a diff.

    matplotlib stamps a creation date into PNG metadata unless told not to; without
    `metadata={"Date": None}` this file would change checksum on every regeneration and
    every `make finops` would show up as a spurious change.
    """
    pytest.importorskip("matplotlib")
    first = write_chart(rates, assumptions, tmp_path / "a.png")
    second = write_chart(rates, assumptions, tmp_path / "b.png")
    assert first.read_bytes() == second.read_bytes()
