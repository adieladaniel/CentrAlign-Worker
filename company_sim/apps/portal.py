"""Vendor portal: paginated, unsorted invoice list; amount/due date only on detail page and PDF."""
from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from .. import config, db
from ..auth import portal_auth
from .common import add_login_routes, redirect, render

router = APIRouter(prefix="/portal", tags=["portal"])
add_login_routes(router, portal_auth, "portal", "Username", "/portal/invoices")


def user(request: Request) -> dict:
    return portal_auth.require(request)


@router.get("", include_in_schema=False)
async def index(_: dict = Depends(user)):
    return redirect("/portal/invoices")


@router.get("/invoices")
async def invoice_list(request: Request, page: int = 1, vendor: str = "", _: dict = Depends(user)):
    vendors = [r["name"] for r in db.query(
        "SELECT DISTINCT v.name FROM invoices i JOIN vendors v ON v.id = i.vendor_id"
        " WHERE i.portal_visible = 1 ORDER BY v.name")]
    where, params = "WHERE i.portal_visible = 1", []
    if vendor:
        where += " AND v.name = ?"
        params.append(vendor)
    base = f"FROM invoices i JOIN vendors v ON v.id = i.vendor_id {where}"
    total = db.query_one(f"SELECT COUNT(*) AS n {base}", params)["n"]
    pages = max(1, math.ceil(total / config.PAGE_SIZE))
    page = min(max(page, 1), pages)
    # Internal id order, deliberately NOT sorted by date.
    rows = db.query(
        f"SELECT i.number, v.name AS vendor, i.issue_date, i.status {base} ORDER BY i.id LIMIT ? OFFSET ?",
        [*params, config.PAGE_SIZE, (page - 1) * config.PAGE_SIZE],
    )
    return render(request, "portal", "portal_list.html", {
        "invoices": rows, "vendors": vendors, "vendor": vendor, "page": page, "pages": pages, "total": total,
    })


def _visible_invoice(number: str) -> dict:
    inv = db.query_one(
        "SELECT i.*, v.name AS vendor, v.address AS vendor_address FROM invoices i JOIN vendors v ON v.id = i.vendor_id"
        " WHERE i.number = ? AND i.portal_visible = 1", (number,))
    if not inv:
        raise HTTPException(404, "Invoice not found")
    return inv


@router.get("/invoices/{number}")
async def invoice_detail(number: str, request: Request, _: dict = Depends(user)):
    inv = _visible_invoice(number)
    items = db.query("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY id", (inv["id"],))
    return render(request, "portal", "portal_detail.html", {"inv": inv, "items": items})


@router.get("/invoices/{number}/pdf")
async def invoice_pdf(number: str, _: dict = Depends(user)):
    inv = _visible_invoice(number)
    path = config.PDF_DIR / f"{inv['number']}.pdf"
    if not path.exists():
        raise HTTPException(404, "PDF not found")
    return FileResponse(path, media_type="application/pdf", filename=f"{inv['number']}.pdf")
