"""Admin endpoints for test harnesses (no auth): chaos config, reset, ground-truth state."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import auth, chaos, config, seed, state

router = APIRouter(prefix="/admin", tags=["admin"])


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def reseed() -> dict:
    """Wipe and reseed data using the active chaos flags; reset counters and sessions."""
    summary = seed.seed_database()
    chaos.reset_counters()
    auth.SESSIONS.clear()
    return summary


@router.get("/chaos")
async def get_chaos() -> dict:
    return chaos.snapshot()


@router.post("/chaos")
async def set_chaos(request: Request):
    """Body: {"flag": true/false, ...} to toggle flags, or {"chaos": ["flag", ...]} to replace the set.

    Changing a data-affecting flag (duplicate_bill, ambiguous_vendor, portal_missing_invoice) reseeds the DB.
    """
    body = await _json_body(request)
    before = chaos.active()
    try:
        if "chaos" in body:
            chaos.set_flags(body["chaos"])
        else:
            chaos.update_flags({k: bool(v) for k, v in body.items()})
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)
    reseeded = bool((before ^ chaos.active()) & chaos.DATA_FLAGS)
    if reseeded:
        reseed()
    return {**chaos.snapshot(), "reseeded": reseeded}


@router.post("/reset")
async def reset(request: Request):
    """Wipe and reseed. Optional body {"chaos": [...]} replaces the active flags first."""
    body = await _json_body(request)
    if "chaos" in body:
        try:
            chaos.set_flags(body["chaos"])
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
    return {"status": "reset", **reseed(), "chaos": chaos.snapshot()["active"]}


@router.get("/state")
async def get_state() -> dict:
    return {
        "sim_today": config.SIM_TODAY.isoformat(),
        "chaos": chaos.snapshot()["active"],
        "bills": state.get_bills(),
        "sent_mail": state.get_sent_mail(),
    }
