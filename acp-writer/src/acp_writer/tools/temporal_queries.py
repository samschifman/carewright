"""Temporal query primitives for clinical QA.

Few, named, precisely documented functions encoding clinical
temporal semantics. Each is @mlflow.trace'd and returns a
TemporalResult with provenance and data_quality metadata.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from acp_writer.tools.temporal_index import (
    IndexedObservation,
    TemporalIndex,
    _parse_datetime,
)
from cpg_contracts.tracing import trace_safe

logger = logging.getLogger(__name__)


@dataclass
class TemporalResult:
    """Result of a temporal query with provenance and quality metadata."""
    found: bool
    value: Any = None
    unit: str | None = None
    provenance: list[str] = field(default_factory=list)
    data_quality: str | None = None
    insufficient_data: bool = False


def _parse_duration(duration_str: str) -> timedelta:
    """Parse an ISO 8601 duration string (P-prefixed) to timedelta.

    Supports: PnD, PnW, PnM, PnY, and combinations.
    """
    if not duration_str or not duration_str.startswith("P"):
        raise ValueError(f"Invalid duration: {duration_str}")

    s = duration_str[1:]
    days = 0

    for suffix, multiplier in [("Y", 365), ("M", 30), ("W", 7), ("D", 1)]:
        if suffix in s:
            parts = s.split(suffix, 1)
            try:
                days += int(parts[0]) * multiplier
            except ValueError:
                pass
            s = parts[1] if len(parts) > 1 else ""

    return timedelta(days=days)


def _reference_datetime(reference_date: date) -> datetime:
    """Convert a date to an end-of-day datetime for window calculations.

    Returns a timezone-aware datetime (UTC) to safely compare with
    FHIR effectiveDateTime values which may include timezone offsets.
    """
    from datetime import timezone
    return datetime(reference_date.year, reference_date.month, reference_date.day,
                    23, 59, 59, tzinfo=timezone.utc)


def _filter_window(
    observations: list[IndexedObservation],
    window: timedelta,
    reference_date: date,
) -> list[IndexedObservation]:
    """Filter observations to those within the time window before reference_date."""
    ref_dt = _reference_datetime(reference_date)
    window_start = ref_dt - window
    return [
        o for o in observations
        if o.has_date and o.effective_dt is not None
        and window_start <= o.effective_dt <= ref_dt
    ]


@trace_safe(name="temporal_observations_in_window")
def observations_in_window(
    index: TemporalIndex,
    code: str,
    duration: str,
    reference_date: date,
) -> TemporalResult:
    """All observations matching code within reference_date - duration to reference_date."""
    all_obs = index.get_observations(code)
    if not all_obs:
        return TemporalResult(found=False, insufficient_data=True)

    window = _parse_duration(duration)
    filtered = _filter_window(all_obs, window, reference_date)

    undated = sum(1 for o in all_obs if not o.has_date)
    quality = f"{undated} undated observations excluded" if undated else None

    if not filtered:
        return TemporalResult(
            found=False, insufficient_data=True, data_quality=quality,
        )

    return TemporalResult(
        found=True,
        value=[{"value": o.value, "unit": o.unit, "date": o.effective_dt.isoformat() if o.effective_dt else None}
               for o in filtered],
        provenance=[o.fhir_reference for o in filtered],
        data_quality=quality,
    )


@trace_safe(name="temporal_observation_count")
def observation_count(
    index: TemporalIndex,
    code: str,
    duration: str,
    reference_date: date,
    threshold: float | None = None,
    comparator: str | None = None,
) -> TemporalResult:
    """Count observations matching code within window, optionally filtered by threshold."""
    all_obs = index.get_observations(code)
    if not all_obs:
        return TemporalResult(found=False, insufficient_data=True)

    window = _parse_duration(duration)
    filtered = _filter_window(all_obs, window, reference_date)

    if threshold is not None and comparator:
        filtered = _apply_threshold(filtered, threshold, comparator)

    undated = sum(1 for o in all_obs if not o.has_date)
    quality = f"{undated} undated observations excluded" if undated else None

    return TemporalResult(
        found=True,
        value=len(filtered),
        provenance=[o.fhir_reference for o in filtered],
        data_quality=quality,
    )


def _apply_threshold(
    observations: list[IndexedObservation],
    threshold: float,
    comparator: str,
) -> list[IndexedObservation]:
    """Filter observations by a numeric threshold comparison."""
    ops = {
        "ge": lambda v: v >= threshold,
        "gt": lambda v: v > threshold,
        "le": lambda v: v <= threshold,
        "lt": lambda v: v < threshold,
        "eq": lambda v: v == threshold,
    }
    op = ops.get(comparator)
    if not op:
        raise ValueError(f"unknown comparator {comparator!r}")

    result = []
    for o in observations:
        try:
            if op(float(o.value)):
                result.append(o)
        except (ValueError, TypeError):
            pass
    return result


@trace_safe(name="temporal_consecutive_above")
def consecutive_above(
    index: TemporalIndex,
    code: str,
    threshold: float,
    reference_date: date,
) -> TemporalResult:
    """Count consecutive readings above threshold from most recent backward.

    Sorted by date descending. Stops at the first reading at-or-below
    the threshold. Undated readings are excluded.
    """
    all_obs = index.get_observations(code)
    dated = [o for o in all_obs if o.has_date and o.effective_dt is not None]

    if not dated:
        undated = sum(1 for o in all_obs if not o.has_date)
        quality = f"{undated} undated observations excluded" if undated else None
        return TemporalResult(found=False, insufficient_data=True, data_quality=quality)

    dated.sort(key=lambda o: o.effective_dt, reverse=True)

    ref_dt = _reference_datetime(reference_date)
    dated = [o for o in dated if o.effective_dt <= ref_dt]

    count = 0
    provenance = []
    for o in dated:
        try:
            if float(o.value) > threshold:
                count += 1
                provenance.append(o.fhir_reference)
            else:
                break
        except (ValueError, TypeError):
            break

    undated = sum(1 for o in all_obs if not o.has_date)
    quality = f"{undated} undated observations excluded" if undated else None

    return TemporalResult(
        found=True,
        value=count,
        provenance=provenance,
        data_quality=quality,
    )


@trace_safe(name="temporal_rate_of_change")
def rate_of_change(
    index: TemporalIndex,
    code: str,
    duration: str,
    reference_date: date,
) -> TemporalResult:
    """Least-squares slope of values over time, normalized per year.

    Requires >= 2 dated numeric readings in the window.
    """
    all_obs = index.get_observations(code)
    if not all_obs:
        return TemporalResult(found=False, insufficient_data=True)

    window = _parse_duration(duration)
    filtered = _filter_window(all_obs, window, reference_date)

    points: list[tuple[float, float]] = []
    provenance: list[str] = []
    for o in filtered:
        if o.effective_dt is None:
            continue
        try:
            val = float(o.value)
        except (ValueError, TypeError):
            continue
        days_from_ref = (o.effective_dt - _reference_datetime(reference_date)).total_seconds() / 86400
        points.append((days_from_ref, val))
        provenance.append(o.fhir_reference)

    if len(points) < 2:
        return TemporalResult(
            found=False, insufficient_data=True,
            data_quality=f"Only {len(points)} dated numeric reading(s) in window; need >= 2",
        )

    n = len(points)
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    sum_x2 = sum(p[0] ** 2 for p in points)

    denom = n * sum_x2 - sum_x ** 2
    if abs(denom) < 1e-10:
        return TemporalResult(
            found=False, insufficient_data=True,
            data_quality="All readings at the same time — cannot compute slope",
        )

    slope_per_day = (n * sum_xy - sum_x * sum_y) / denom
    slope_per_year = slope_per_day * 365.25

    return TemporalResult(
        found=True,
        value=round(slope_per_year, 1),
        unit="per year",
        provenance=provenance,
    )


@trace_safe(name="temporal_cross_resource")
def cross_resource_temporal(
    index: TemporalIndex,
    bundle: dict,
    anchor_code: str,
    target_code: str,
    window: str,
) -> TemporalResult:
    """Check if a target observation exists within a time window after an anchor medication start."""
    meds = index.medications.get(anchor_code, [])
    if not meds:
        return TemporalResult(found=False, insufficient_data=True,
                              data_quality="Anchor medication not found")

    anchor_med = meds[0]
    if not anchor_med.start_date:
        return TemporalResult(found=False, insufficient_data=True,
                              data_quality="Anchor medication has no start date")

    window_td = _parse_duration(window)
    window_end = anchor_med.start_date + window_td

    target_obs = index.get_observations(target_code)
    for o in target_obs:
        if o.has_date and o.effective_dt is not None:
            if anchor_med.start_date <= o.effective_dt <= window_end:
                return TemporalResult(
                    found=True,
                    value=True,
                    provenance=[o.fhir_reference, anchor_med.fhir_reference],
                )

    return TemporalResult(
        found=True,
        value=False,
        provenance=[anchor_med.fhir_reference],
        data_quality=f"No {target_code} observation found within {window} of medication start",
    )
