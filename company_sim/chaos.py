"""Global, controllable "chaos" configuration used for reliability testing."""
from __future__ import annotations

import os
import threading
from typing import Iterable

FLAGS: dict[str, str] = {
    "erp_submit_500_once": "First POST of the ERP new-bill form (and first ERP API POST) returns HTTP 500, then works.",
    "erp_session_expiry": "ERP session expires after the 3rd authenticated page view (once per reset).",
    "duplicate_bill": "[seed] Latest Company X Ltd invoice already exists as an ERP bill.",
    "ambiguous_vendor": "[seed] Company X Holdings has a recent invoice in the portal (same issue date as Company X Ltd's latest).",
    "portal_missing_invoice": "[seed] Latest Company X Ltd invoice is hidden from the portal (email attachment only).",
    "slow_pages": "Random 1-3 s latency on page loads.",
}
# Flags that change seeded data: toggling them triggers a reseed.
DATA_FLAGS = {"duplicate_bill", "ambiguous_vendor", "portal_missing_invoice"}

_lock = threading.Lock()
_active: set[str] = set()
_used_once: set[str] = set()


def _validate(names: Iterable[str]) -> set[str]:
    names = {n.strip() for n in names if n and n.strip()}
    unknown = names - set(FLAGS)
    if unknown:
        raise ValueError(f"Unknown chaos flag(s): {sorted(unknown)}. Valid: {sorted(FLAGS)}")
    return names


def load_from_env() -> None:
    """Load flags from the SIM_CHAOS env var (comma separated), if set."""
    raw = os.environ.get("SIM_CHAOS")
    if raw is not None:
        set_flags(raw.split(","))


def active() -> set[str]:
    """Return a copy of the currently active flags."""
    with _lock:
        return set(_active)


def is_on(flag: str) -> bool:
    with _lock:
        return flag in _active


def set_flags(names: Iterable[str]) -> set[str]:
    """Replace the active flag set. Resets one-time counters."""
    names = _validate(names)
    with _lock:
        _active.clear()
        _active.update(names)
        _used_once.clear()
    return names


def update_flags(changes: dict[str, bool]) -> set[str]:
    """Turn individual flags on/off. Resets one-time counters."""
    _validate(changes)
    with _lock:
        for name, on in changes.items():
            (_active.add if on else _active.discard)(name)
        _used_once.clear()
        return set(_active)


def reset_counters() -> None:
    with _lock:
        _used_once.clear()


def consume_once(key: str) -> bool:
    """Return True the first time `key` is consumed (until counters reset)."""
    with _lock:
        if key in _used_once:
            return False
        _used_once.add(key)
        return True


def snapshot() -> dict:
    """JSON-friendly view of the chaos state."""
    with _lock:
        return {
            "flags": {name: name in _active for name in FLAGS},
            "active": sorted(_active),
            "one_time_used": sorted(_used_once),
            "descriptions": FLAGS,
        }
