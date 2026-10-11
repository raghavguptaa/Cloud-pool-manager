"""Readers for the captured telemetry in `data/`.

The files in `data/` are byte-identical copies of what came out of the AWS console
in March 2026, and the account they came from is closed. They cannot be re-exported,
so nothing in this package ever writes to them and nothing here repairs, resamples or
back-fills a value. A file that does not parse raises; it is never guessed at.

Two shapes are handled:

* **Metric exports** (`*_1min.csv`, `*_5min.csv`) -- the CloudWatch console's
  ``Download as .csv``. A few `key,value` header lines, then `timestamp,value` rows.
  The header is *detected*, not counted: the reader takes the first line whose first
  field parses as a CloudWatch timestamp as the start of the data. Counting lines
  would silently eat a data row the day the console adds a header field.
* **Event timelines** (`*_events.csv`) -- ordinary CSVs with a header row,
  transcribed from the spreadsheets kept during the runs.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "BASELINE_EVENTS",
    "BASELINE_METRICS",
    "DATA_DIR",
    "GOVERNOR_EVENTS",
    "GOVERNOR_METRICS",
    "REPO_ROOT",
    "RESULTS_DIR",
    "BaselineEvent",
    "MetricSeries",
    "RecordedState",
    "Sample",
    "find_baseline_event",
    "read_baseline_events",
    "read_governor_states",
    "read_metric_export",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
RESULTS_DIR = REPO_ROOT / "results"

BASELINE_METRICS = DATA_DIR / "baseline_5min.csv"
GOVERNOR_METRICS = DATA_DIR / "governor_1min.csv"
BASELINE_EVENTS = DATA_DIR / "baseline_events.csv"
GOVERNOR_EVENTS = DATA_DIR / "governor_events.csv"

_EXPORT_TIME_FORMAT = "%Y/%m/%d %H:%M:%S"

# "ap-south-1:AWS/EC2 InstanceId:i-0da018dd62e55fada CPUUtilization Maximum 60"
# -- the console's own provenance line. Parsed so the replay report can state which
# instance, statistic and period it is talking about instead of asserting it.
_FULL_LABEL_RE = re.compile(
    r"^(?P<region>[\w-]+):(?P<namespace>\S+)\s+InstanceId:(?P<instance_id>\S+)\s+"
    r"(?P<metric>\S+)\s+(?P<statistic>\S+)\s+(?P<period>\d+)\s*$"
)


@dataclass(frozen=True, slots=True)
class Sample:
    """One CloudWatch datapoint: an aggregation period and the value over it."""

    timestamp: datetime
    """Start of the aggregation period, timezone-aware UTC."""

    cpu: float
    """`CPUUtilization` as a percentage. The statistic is a property of the series,
    not of the sample -- see `MetricSeries.statistic`."""


@dataclass(frozen=True, slots=True)
class MetricSeries:
    """A parsed metric export: its samples plus the provenance from its header."""

    path: Path
    samples: tuple[Sample, ...]
    region: str | None
    namespace: str | None
    instance_id: str | None
    metric: str | None
    statistic: str | None
    period_seconds: int | None

    def __len__(self) -> int:
        return len(self.samples)

    def __iter__(self) -> Iterator[Sample]:
        return iter(self.samples)

    @property
    def start(self) -> datetime:
        return self.samples[0].timestamp

    @property
    def end(self) -> datetime:
        return self.samples[-1].timestamp

    @property
    def peak(self) -> Sample:
        return max(self.samples, key=lambda s: s.cpu)

    @property
    def period(self) -> int:
        """The aggregation period in seconds.

        Raises rather than defaulting: the period is what separates the two arms of
        the experiment, so an export whose header did not name one must not quietly
        be treated as though it had.
        """
        if self.period_seconds is None:
            raise ValueError(f"{self.path.name}: the export header names no period")
        return self.period_seconds

    def describe(self) -> str:
        """One line of provenance, for the head of the replay report."""
        stat = f"{self.metric} {self.statistic} / {self.period_seconds} s"
        return f"{self.region} · instance {self.instance_id} · {stat}"


@dataclass(frozen=True, slots=True)
class RecordedState:
    """One row of `data/governor_events.csv` -- a hand-recorded observation.

    `time_ist_recorded` is kept verbatim as the original label even though it is
    wrong: it was computed with a UTC+06:00 offset rather than IST's UTC+05:30 (the
    evidence is in `data/PROVENANCE.md`). `time_utc_start` / `time_utc_end` are the
    corrected values and are what the replay joins on.

    `max_cpu_pct` is kept as text because one row records a bound ("<15.0") rather
    than a measurement; `cpu_value` is the number when there is one, and
    `cpu_upper_bound` is the number when the cell is a bound.
    """

    time_ist_recorded: str
    time_utc_start: datetime
    time_utc_end: datetime | None
    system_state: str
    max_cpu_text: str
    cpu_value: float | None
    cpu_upper_bound: float | None
    orchestrator_action: str
    warm_pool_size: str
    active_instances: str

    @property
    def window_end(self) -> datetime:
        """The row's closing timestamp -- its own start when it names a single moment."""
        return self.time_utc_end or self.time_utc_start

    @property
    def is_range(self) -> bool:
        return self.time_utc_end is not None


@dataclass(frozen=True, slots=True)
class BaselineEvent:
    """One row of `data/baseline_events.csv`.

    Wall-clock times were read off the console by hand during the run, so they are
    good to roughly a second, not better.
    """

    event: str
    time_utc: datetime
    time_ist: datetime
    delta: str


def read_metric_export(path: Path) -> MetricSeries:
    """Parse a CloudWatch console metric export.

    Raises:
        ValueError: the file has no datapoints, repeats a timestamp, or carries a
            row whose value is blank. A blank value is a metric gap, and a gap is not
            a number the decision function is allowed to see -- `governor.fsm`
            refuses to classify one, so the reader refuses to invent one.
    """
    header: dict[str, str] = {}
    samples: list[Sample] = []
    seen: set[datetime] = set()

    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.reader(handle):
            if not row or not row[0].strip():
                continue
            timestamp = _as_export_timestamp(row[0])
            if timestamp is None:
                # Still in the header block: `Id,m21`, `Full label,...` and friends.
                header[row[0].strip()] = row[1].strip() if len(row) > 1 else ""
                continue
            if len(row) < 2 or not row[1].strip():
                raise ValueError(f"{path.name}: blank value at {row[0]!r} (metric gap)")
            if timestamp in seen:
                raise ValueError(f"{path.name}: duplicate timestamp {row[0]!r}")
            seen.add(timestamp)
            samples.append(Sample(timestamp=timestamp, cpu=float(row[1])))

    if not samples:
        raise ValueError(f"{path.name}: no datapoints found below the header")

    # The console emits ascending order, but its sort is a UI setting; sorting here
    # means the replay's "in timestamp order" guarantee does not depend on that.
    samples.sort(key=lambda sample: sample.timestamp)

    label = _FULL_LABEL_RE.match(header.get("Full label", ""))
    return MetricSeries(
        path=path,
        samples=tuple(samples),
        region=label["region"] if label else None,
        namespace=label["namespace"] if label else None,
        instance_id=label["instance_id"] if label else None,
        metric=label["metric"] if label else None,
        statistic=label["statistic"] if label else None,
        period_seconds=int(label["period"]) if label else None,
    )


def read_governor_states(path: Path = GOVERNOR_EVENTS) -> tuple[RecordedState, ...]:
    """Parse the hand-recorded governor state timeline."""
    rows: list[RecordedState] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not row.get("time_utc_start"):
                continue
            cpu_text = (row["max_cpu_pct"] or "").strip()
            value, bound = _as_cpu_reading(cpu_text)
            rows.append(
                RecordedState(
                    time_ist_recorded=row["time_ist_recorded"].strip(),
                    time_utc_start=_as_iso_timestamp(row["time_utc_start"]),
                    time_utc_end=(
                        _as_iso_timestamp(row["time_utc_end"])
                        if row["time_utc_end"].strip()
                        else None
                    ),
                    system_state=row["system_state"].strip(),
                    max_cpu_text=cpu_text,
                    cpu_value=value,
                    cpu_upper_bound=bound,
                    orchestrator_action=row["orchestrator_action"].strip(),
                    warm_pool_size=row["warm_pool_size"].strip(),
                    active_instances=row["active_instances"].strip(),
                )
            )
    if not rows:
        raise ValueError(f"{path.name}: no recorded states found")
    return tuple(rows)


def read_baseline_events(path: Path = BASELINE_EVENTS) -> tuple[BaselineEvent, ...]:
    """Parse the hand-recorded baseline event timeline."""
    rows: list[BaselineEvent] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not row.get("time_utc"):
                continue
            rows.append(
                BaselineEvent(
                    event=row["event"].strip(),
                    time_utc=_as_iso_timestamp(row["time_utc"]),
                    time_ist=_as_iso_timestamp(row["time_ist"]),
                    delta=row["delta"].strip(),
                )
            )
    if not rows:
        raise ValueError(f"{path.name}: no events found")
    return tuple(rows)


def find_baseline_event(events: Sequence[BaselineEvent], needle: str) -> BaselineEvent:
    """Return the single event whose description contains `needle`.

    Deliberately strict in both directions. The latency figures the replay reports are
    differences between specific rows of this file; if an edit ever made "the alarm"
    ambiguous or absent, a quietly-picked first match would turn into a wrong number
    printed with full confidence.

    Raises:
        LookupError: no event matches, or more than one does.
    """
    matches = [event for event in events if needle.lower() in event.event.lower()]
    if len(matches) != 1:
        raise LookupError(f"{needle!r} matched {len(matches)} events, expected exactly 1")
    return matches[0]


def _as_export_timestamp(field: str) -> datetime | None:
    """Parse a CloudWatch export timestamp, or return None if this is a header line."""
    try:
        naive = datetime.strptime(field.strip(), _EXPORT_TIME_FORMAT)
    except ValueError:
        return None
    # The console exports in the timezone the browser was set to display; these files
    # were pulled in UTC, which the `Full label` period/statistic and the event
    # timelines corroborate. See data/PROVENANCE.md.
    return naive.replace(tzinfo=UTC)


def _as_iso_timestamp(field: str) -> datetime:
    """Parse an ISO-8601 instant from an event timeline, normalised to UTC."""
    parsed = datetime.fromisoformat(field.strip())
    if parsed.tzinfo is None:
        raise ValueError(f"{field!r} has no UTC offset; event times must be unambiguous")
    return parsed.astimezone(UTC)


def _as_cpu_reading(text: str) -> tuple[float | None, float | None]:
    """Split a recorded CPU cell into `(value, upper_bound)`.

    The cell is a measurement in every row but one, where the observer wrote "<15.0"
    -- a bound, not a reading. Returning them in separate slots keeps the replay from
    comparing a bound as if it were a datapoint.
    """
    if text.startswith("<"):
        return None, float(text[1:])
    return float(text), None
