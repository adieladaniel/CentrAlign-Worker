"""Ground-truth snapshots of the simulated company, used by the checkers.

``fetch_admin_state`` reads ``GET /admin/state`` (bills, sent mail, SIM_TODAY, active chaos flags) over HTTP, so the
evals work against a sim that runs as a separate process; if the sim is unreachable it falls back to reading the sim
database directly (``company_sim.state``). ``build_sim_state`` then adds the facts the checkers compare against:
the invoices and inbox derived from the sim's deterministic seed for that date and flag set (``seed.invoice_specs``
and ``seed.email_specs``), i.e. what the sim holds, never what the agent claims. Nothing is hard-coded to a date.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from company_sim import seed
from company_sim.pdfgen import totals

HTTP_TIMEOUT_S = 10.0


def expected_invoices(today: date, flags: set[str]) -> list[dict]:
    """All invoices of the seed for ``today`` and ``flags`` (including portal-hidden ones), plain JSON-friendly dicts."""
    rows = []
    for spec in seed.invoice_specs(today, set(flags)):
        _, _, total = totals(spec["items"], seed.TAX_BY_CURRENCY[spec["currency"]])
        rows.append({
            "number": spec["number"], "vendor": spec["vendor"], "amount": total, "currency": spec["currency"],
            "issue_date": spec["issue"].isoformat(), "due_date": spec["due"].isoformat(),
            "status": spec["status"], "portal_visible": bool(spec["visible"]),
        })
    return rows


def latest_invoice(invoices: list[dict], vendor: str) -> dict | None:
    """Most recent invoice (by issue date) of an exact vendor name, like ``company_sim.state.latest_invoice_for``."""
    mine = [i for i in invoices if i["vendor"].lower() == vendor.strip().lower()]
    return max(mine, key=lambda i: i["issue_date"]) if mine else None


def expected_inbox(today: date, invoices: list[dict]) -> list[dict]:
    """Inbox messages of the seed (sender, subject, body, ...)."""
    latest = latest_invoice(invoices, "Company X Ltd")
    return seed.email_specs(today, latest["number"] if latest else "")


def _direct_admin_state(flags: list[str]) -> dict:
    """Fallback when the sim is not reachable over HTTP: read its database directly."""
    from company_sim import config, state

    return {"sim_today": config.SIM_TODAY.isoformat(), "chaos": sorted(flags),
            "bills": state.get_bills(), "sent_mail": state.get_sent_mail()}


def fetch_admin_state(base_url: str = "", http: Any = None, fallback_flags: list[str] | None = None) -> dict:
    """``GET /admin/state`` (``http`` may be any object with ``get`` such as an httpx or Starlette client)."""
    try:
        if http is not None:
            resp = http.get("/admin/state")
        else:
            resp = httpx.get(base_url.rstrip("/") + "/admin/state", timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return _direct_admin_state(fallback_flags or [])


def build_sim_state(admin_state: dict, baseline: dict | None = None) -> dict:
    """Combine an admin snapshot with seed-derived ground truth.

    ``baseline`` is the snapshot taken right after the reset, before the agent ran; it lets checkers tell the bills
    and mails the agent created from the ones that were seeded.
    """
    today = date.fromisoformat(admin_state["sim_today"])
    invoices = expected_invoices(today, set(admin_state.get("chaos") or []))
    return {
        "sim_today": admin_state["sim_today"],
        "chaos": list(admin_state.get("chaos") or []),
        "bills": list(admin_state.get("bills") or []),
        "sent_mail": list(admin_state.get("sent_mail") or []),
        "baseline_bills": list((baseline or admin_state).get("bills") or []),
        "baseline_sent_mail": list((baseline or admin_state).get("sent_mail") or []),
        "invoices": invoices,
        "inbox": expected_inbox(today, invoices),
    }
