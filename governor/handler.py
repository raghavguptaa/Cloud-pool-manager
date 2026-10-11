"""Lambda entrypoint: `governor.handler.lambda_handler`.

Everything environment-shaped lives here -- reading the six environment variables,
validating them, configuring logging, building the backend -- so that `aws_adapter.py`
takes its configuration as arguments and `fsm.py` takes its as a value. The handler itself
is four statements.

The six variables, and what they do:

| Variable             | Default | Effect                                              |
|----------------------|---------|-----------------------------------------------------|
| `ASG_NAME`           | --      | Required. The Auto Scaling group to control.         |
| `SCALE_IN_THRESHOLD` | `20`    | Below this, cost-saving. See `governor/config.py`.   |
| `PRE_WARM_THRESHOLD` | `40`    | At or above this, mitigate.                          |
| `METRIC_PERIOD`      | `60`    | CloudWatch period to query, in seconds.              |
| `DRY_RUN`            | `false` | Decide and log, write nothing.                       |
| `LOG_LEVEL`          | `INFO`  | Level for the `governor` logger.                     |

Configuration errors are raised, not defaulted around. A function that silently controls
the wrong group, or silently makes writes an operator meant to suppress, is worse than a
function that fails its invocation and shows up in the error metric a minute later -- with
one deliberate exception, an unreadable `LOG_LEVEL`, which cannot affect what gets written
and so falls back to INFO with a warning instead of taking the function down.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from governor.aws_adapter import DEFAULT_PERIOD_SECONDS, AwsScalingBackend, run_once
from governor.config import Config

__all__ = ["Settings", "lambda_handler"]

LOGGER = logging.getLogger(__name__)

_PACKAGE_LOGGER = "governor"

# The record is already a complete JSON object, so the formatter must add nothing to it.
_LOG_FORMAT = "%(message)s"

_TRUE_TOKENS = frozenset({"1", "t", "true", "y", "yes", "on"})
_FALSE_TOKENS = frozenset({"", "0", "f", "false", "n", "no", "off"})

# CloudWatch accepts 1, 5, 10, 20 and 30 seconds for high-resolution metrics, and any
# multiple of 60 otherwise. Checking here converts what would be a ValidationError on every
# invocation, once a minute, forever, into one failure at the moment of misconfiguration.
_HIGH_RESOLUTION_PERIODS = frozenset({1, 5, 10, 20, 30})


@dataclass(frozen=True, slots=True)
class Settings:
    """The validated environment, as a value.

    Separate from `Config` because `Config` configures the *decision* and is shared with
    the offline replay, while these are deployment concerns that only the Lambda has.
    """

    asg_name: str
    config: Config
    period_seconds: int
    dry_run: bool
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read and validate the six variables. Defaults to `os.environ`.

        Raises:
            ValueError: A variable is missing, blank where it is required, or unreadable.
        """
        source = os.environ if env is None else env
        asg_name = (source.get("ASG_NAME") or "").strip()
        if not asg_name:
            raise ValueError("ASG_NAME is unset or blank; the controller has no group to act on")
        return cls(
            asg_name=asg_name,
            config=Config.from_env(source),
            period_seconds=_metric_period(source.get("METRIC_PERIOD")),
            dry_run=_dry_run(source.get("DRY_RUN")),
            log_level=(source.get("LOG_LEVEL") or "INFO").strip() or "INFO",
        )


def lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    """Run one control cycle and return what it decided.

    Args:
        event: The EventBridge schedule event. Unread: the controller's inputs are the
            metric and the group's current configuration, and nothing about one scheduled
            tick distinguishes it from the next. Dry-run in particular is deliberately not
            overridable per-invocation -- suppressing writes is a change to the function's
            configuration, where CloudTrail records who did it, not a payload field.
        context: The Lambda context. Unread, for the same reason.

    Returns:
        The decision as JSON-serialisable primitives -- identical to the object written to
        the log -- so a console test invoke shows the outcome without opening Logs.
    """
    settings = Settings.from_env()
    _configure_logging(settings.log_level)
    backend = AwsScalingBackend.for_asg(settings.asg_name)
    decision = run_once(
        backend,
        cfg=settings.config,
        period_seconds=settings.period_seconds,
        dry_run=settings.dry_run,
    )
    return decision.to_dict()


def _configure_logging(level_name: str) -> None:
    """Point the `governor` logger at one bare JSON object per line.

    The Lambda runtime installs a root handler whose formatter prefixes every record with
    a timestamp and the request id. That prefix turns each line into something that is
    *nearly* JSON, which Logs Insights will not parse into fields -- so the formatter is
    replaced rather than the handler added to, which would double every line instead.
    Outside Lambda there is no root handler and `basicConfig` supplies one, so the same
    output appears when the module is exercised locally.
    """
    level = logging.getLevelNamesMapping().get(level_name.strip().upper())
    logging.getLogger(_PACKAGE_LOGGER).setLevel(logging.INFO if level is None else level)

    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    for handler in root.handlers:
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))

    if level is None:
        LOGGER.warning(
            json.dumps(
                {
                    "event": "governor.config_warning",
                    "variable": "LOG_LEVEL",
                    "value": level_name,
                    "detail": "unrecognised level name, falling back to INFO",
                }
            )
        )


def _metric_period(raw: str | None) -> int:
    """Parse `METRIC_PERIOD` into a period CloudWatch will actually accept."""
    if raw is None or not raw.strip():
        return DEFAULT_PERIOD_SECONDS
    try:
        period = int(raw)
    except ValueError:
        raise ValueError(f"METRIC_PERIOD={raw!r} is not an integer") from None
    if period not in _HIGH_RESOLUTION_PERIODS and (period <= 0 or period % 60 != 0):
        raise ValueError(
            f"METRIC_PERIOD={period} is not a CloudWatch period; "
            f"use {sorted(_HIGH_RESOLUTION_PERIODS)} or a positive multiple of 60"
        )
    return period


def _dry_run(raw: str | None) -> bool:
    """Parse `DRY_RUN`, refusing anything that is not unambiguously a boolean.

    True:  `1 t true y yes on`. False: unset, empty, `0 f false n no off`. Case and
    surrounding whitespace are ignored.

    Anything else raises, rather than falling back to false. This flag is the only thing
    standing between the controller and live mutating API calls: reading `DRY_RUN=off!` or
    `DRY_RUN=disabled` as "write for real" is a one-character typo away from a surprise
    scaling activity, and the failure would be silent. Failing the invocation is loud, and
    the fix is obvious from the message.
    """
    if raw is None:
        return False
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise ValueError(
        f"DRY_RUN={raw!r} is not a recognised boolean; "
        f"use one of {sorted(_TRUE_TOKENS)} or {sorted(_FALSE_TOKENS - {''})}"
    )
