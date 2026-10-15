"""Per-organisation settings (``orgs.settings``, JSONB): defaults, validation, and the approval-rights rules.

An org only stores what an admin changed; everything else falls back to ``ORG_SETTING_DEFAULTS`` (or, for the run limits,
to the deployment's own ``Settings``). The keys an admin can edit are exactly ``EDITABLE``; other keys of the JSON
document (``limits``, ``models``: reserved for the quota milestone) are never touched by this module.

Approval rights are a capability of their own, not just a rank. ``can_approve`` is the single place that decides it, and
``approval_block`` is the single place that applies separation of duties.
"""
from __future__ import annotations

from typing import Any

from worker.config import Settings

APPROVAL_MODES = ("auto", "confirm_writes", "confirm_all")
APPROVAL_STRICTNESS = {mode: rank for rank, mode in enumerate(APPROVAL_MODES)}

# key -> (type, min, max) for numbers; ints only. Booleans and the approval mode are checked separately.
_BOOLEANS = ("separation_of_duties", "sod_for_writes", "operators_can_approve")
_NUMBERS: dict[str, tuple[int, int]] = {
    "approval_timeout_s": (30, 7 * 24 * 3600),
    "max_runs_per_day": (0, 100_000),    # 0 = unlimited
    "max_steps": (1, 500),
    "run_timeout_s": (30, 24 * 3600),
}
EDITABLE = ("approval_mode", *_BOOLEANS, *_NUMBERS)

# What a missing key means. max_steps / run_timeout_s / approval_mode default to the deployment's settings instead.
ORG_SETTING_DEFAULTS: dict[str, Any] = {
    "separation_of_duties": True,
    "sod_for_writes": False,
    "operators_can_approve": False,
    "approval_timeout_s": 3600,
    "max_runs_per_day": 50,
}

# Roles that always hold approval rights; operators only when the org allows it.
APPROVER_ROLES = frozenset({"approver", "admin", "owner"})


def effective(stored: dict | None, deployment: Settings) -> dict[str, Any]:
    """Every editable setting with its effective value (stored value, else the default)."""
    stored = stored or {}
    base = {
        "approval_mode": deployment.APPROVAL_MODE, "max_steps": deployment.MAX_STEPS,
        "run_timeout_s": deployment.RUN_TIMEOUT_S, **ORG_SETTING_DEFAULTS,
    }
    return {key: stored[key] if key in stored and stored[key] is not None else base[key] for key in EDITABLE}


def validate_update(updates: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """The cleaned ``updates`` (only editable keys); raises ``ValueError`` with a readable message on bad input.

    ``current`` is the effective settings before the update, used for the cross-field rule."""
    if not isinstance(updates, dict) or not updates:
        raise ValueError("Send at least one setting to change.")
    unknown = sorted(set(updates) - set(EDITABLE))
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(unknown)}. Editable: {', '.join(EDITABLE)}.")
    clean: dict[str, Any] = {}
    for key, value in updates.items():
        if key == "approval_mode":
            if value not in APPROVAL_MODES:
                raise ValueError(f"approval_mode must be one of: {', '.join(APPROVAL_MODES)}.")
        elif key in _BOOLEANS:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false.")
        else:
            low, high = _NUMBERS[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{key} must be a whole number from {low} to {high}.")
        clean[key] = value
    merged = {**current, **clean}
    if merged["sod_for_writes"] and not merged["separation_of_duties"]:
        raise ValueError("sod_for_writes only applies when separation_of_duties is on.")
    return clean


def can_approve(role: str, settings: dict[str, Any]) -> bool:
    """May a member with ``role`` answer approval requests? Approvers, admins and owners always; operators only when
    the org sets ``operators_can_approve``; viewers never."""
    return role in APPROVER_ROLES or (role == "operator" and bool(settings.get("operators_can_approve")))


def approval_block(settings: dict[str, Any], risk: str, user_id: str, run_starter_id: str | None) -> str | None:
    """Why this user may not APPROVE an action of risk ``risk`` (separation of duties), or None when they may.

    With ``separation_of_duties`` the person who started the run cannot approve its irreversible actions; with
    ``sod_for_writes`` also not its writes. Denying is never blocked."""
    if not settings.get("separation_of_duties") or not run_starter_id or run_starter_id != user_id:
        return None
    if risk == "irreversible" or (risk == "write" and settings.get("sod_for_writes")):
        return (f"Separation of duties: you started this run, so someone else must approve its {risk} actions "
                "(you can still deny).")
    return None


def stricter_mode(org_mode: str, requested: str | None) -> str:
    """The approval mode of a run: the org's mode, or the requested one if it asks for more confirmation."""
    if requested and APPROVAL_STRICTNESS.get(requested, -1) > APPROVAL_STRICTNESS.get(org_mode, 0):
        return requested
    return org_mode


def policy_view(settings: dict[str, Any]) -> dict[str, Any]:
    """The part of the settings every member may see (the UI explains who can approve from it)."""
    keys = ("approval_mode", "separation_of_duties", "sod_for_writes", "operators_can_approve", "approval_timeout_s")
    return {key: settings[key] for key in keys}
