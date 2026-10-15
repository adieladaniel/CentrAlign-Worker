"""Controllable "chaos" configuration used for reliability testing. Flags and one-time counters are per tenant."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Iterable

from . import tenants

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


@dataclass
class _State:
    active: set[str] = field(default_factory=set)
    used_once: set[str] = field(default_factory=set)


_states: dict[str, _State] = {}  # tenant id -> state; the current tenant selects which one every function uses


def validate(names: Iterable[str]) -> set[str]:
    names = {n.strip() for n in names if n and n.strip()}
    unknown = names - set(FLAGS)
    if unknown:
        raise ValueError(f"Unknown chaos flag(s): {sorted(unknown)}. Valid: {sorted(FLAGS)}")
    return names


def _state() -> _State:
    """State of the current tenant (caller holds ``_lock``). A tenant's flags are restored from the index on first use."""
    tid = tenants.current_tenant.get()
    state = _states.get(tid)
    if state is None:
        tenant = tenants.get_tenant(tid) if tid != tenants.DEFAULT else None
        state = _states[tid] = _State(active=set(tenant.chaos) if tenant else set())
    return state


def _persist(active: set[str]) -> None:
    tenants.save_chaos(tenants.current_tenant.get(), active)


def forget(tenant_id: str) -> None:
    """Drop the in-memory state of a tenant (it was deleted or re-created)."""
    with _lock:
        _states.pop(tenant_id, None)


def load_from_env() -> None:
    """Load flags from the SIM_CHAOS env var (comma separated), if set. Applies to the default tenant."""
    raw = os.environ.get("SIM_CHAOS")
    if raw is not None:
        set_flags(raw.split(","))


def active() -> set[str]:
    """Return a copy of the current tenant's active flags."""
    with _lock:
        return set(_state().active)


def is_on(flag: str) -> bool:
    with _lock:
        return flag in _state().active


def set_flags(names: Iterable[str]) -> set[str]:
    """Replace the active flag set. Resets one-time counters."""
    names = validate(names)
    with _lock:
        state = _state()
        state.active = set(names)
        state.used_once.clear()
        current = set(state.active)
    _persist(current)
    return names


def update_flags(changes: dict[str, bool]) -> set[str]:
    """Turn individual flags on/off. Resets one-time counters."""
    validate(changes)
    with _lock:
        state = _state()
        for name, on in changes.items():
            (state.active.add if on else state.active.discard)(name)
        state.used_once.clear()
        current = set(state.active)
    _persist(current)
    return current


def reset_counters() -> None:
    with _lock:
        _state().used_once.clear()


def consume_once(key: str) -> bool:
    """Return True the first time `key` is consumed (until counters reset)."""
    with _lock:
        used = _state().used_once
        if key in used:
            return False
        used.add(key)
        return True


def snapshot() -> dict:
    """JSON-friendly view of the chaos state."""
    with _lock:
        state = _state()
        return {
            "flags": {name: name in state.active for name in FLAGS},
            "active": sorted(state.active),
            "one_time_used": sorted(state.used_once),
            "descriptions": FLAGS,
        }
