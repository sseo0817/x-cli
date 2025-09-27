from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional


@dataclass(frozen=True)
class PrimeSlot:
    label: str
    start_hour: int
    end_hour: int


# Non-overlapping prime time slots in UTC, ordered as displayed in the grid
PRIME_SLOTS: tuple[PrimeSlot, ...] = (
    PrimeSlot("NY evening", 22, 1),   # wraps to next day
    PrimeSlot("CA evening", 1, 5),
    PrimeSlot("Asia morning", 5, 8),
    PrimeSlot("EU morning", 8, 11),
    PrimeSlot("EU noon", 11, 12),
    PrimeSlot("NY morning", 12, 15),
    PrimeSlot("CA morning", 15, 19),
    PrimeSlot("CA noon", 19, 22),
)


def _normalize_label(label: str) -> str:
    return " ".join(label.strip().lower().replace("_", " ").replace("-", " ").split())


_PRIME_SLOT_LOOKUP: Dict[str, PrimeSlot] = {
    _normalize_label(slot.label): slot for slot in PRIME_SLOTS
}


def resolve_prime_slot(label: str) -> Optional[PrimeSlot]:
    """Return the canonical PrimeSlot for the given label (case-insensitive)."""
    if not isinstance(label, str):
        return None
    return _PRIME_SLOT_LOOKUP.get(_normalize_label(label))


def prime_slot_bounds_utc(day0: datetime, slot: PrimeSlot) -> tuple[datetime, datetime]:
    """Return (start,end) in UTC for a prime slot labelled by day0 (UTC midnight of label day).

    For wrap slots (e.g., 22→01), the label corresponds to the END date, so
    start = (day0 - 1day) at 22:00, end = day0 at 01:00.
    """
    if day0.tzinfo is None:
        day0 = day0.replace(tzinfo=timezone.utc)
    else:
        day0 = day0.astimezone(timezone.utc)
    start_h, end_h = slot.start_hour, slot.end_hour
    if start_h <= end_h:
        start = day0.replace(hour=start_h, minute=0, second=0, microsecond=0)
        end = day0.replace(hour=end_h, minute=0, second=0, microsecond=0)
    else:
        prev = day0 - timedelta(days=1)
        start = prev.replace(hour=start_h, minute=0, second=0, microsecond=0)
        end = day0.replace(hour=end_h, minute=0, second=0, microsecond=0)
    # Ensure outputs are UTC-aware
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

