"""ERP "Ledger": accounts-payable bills (HTML UI + REST API under /erp/api)."""
from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .. import chaos, config, db, state, tenants
from ..auth import erp_auth
from .common import add_login_routes, flash, redirect, render

router = APIRouter(prefix="/erp", tags=["erp"])
add_login_routes(router, erp_auth, "erp", "Username", "/erp/bills")

CURRENCIES = ("USD", "EUR", "GBP")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SERVER_ERROR_MESSAGE = "Internal Server Error – please try again"
FORM_FIELDS = ("vendor", "invoice_number", "amount", "currency", "issue_date", "due_date", "notes")


# --------------------------------------------------------------------------- session handling
def user(request: Request) -> dict:
    """Page dependency: require login and apply the optional session-expiry chaos."""
    session = erp_auth.require(request)
    if chaos.is_on("erp_session_expiry"):
        if session["views"] >= 3 and chaos.consume_once("erp_session_expired"):
            erp_auth.expire(request)
        if request.method == "GET":
            session["views"] += 1
    return session


# --------------------------------------------------------------------------- validation
class DuplicateBill(Exception):
    def __init__(self, bill_id: int) -> None:
        self.bill_id = bill_id
        super().__init__(f"Bill for this vendor and invoice number already exists (Bill #{bill_id})")


def _parse_date(value: Any, label: str, field: str, errors: list[tuple[str, str]]) -> date | None:
    text = str(value or "").strip()
    if not text:
        errors.append((field, f"{label} is required"))
    elif not DATE_RE.match(text):
        errors.append((field, f"{label} must be in YYYY-MM-DD format (got '{text}')"))
    else:
        try:
            return date.fromisoformat(text)
        except ValueError:
            errors.append((field, f"{label} is not a valid calendar date ('{text}')"))
    return None


def _parse_amount(value: Any, errors: list[tuple[str, str]]) -> float | None:
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        errors.append(("amount", "Amount is required"))
        return None
    try:
        amount = Decimal(str(value).strip())
        if not amount.is_finite():
            raise InvalidOperation
    except InvalidOperation:
        errors.append(("amount", "Amount must be a number (e.g. 1234.50)"))
        return None
    if amount <= 0:
        errors.append(("amount", "Amount must be greater than 0"))
    elif amount.as_tuple().exponent < -2:
        errors.append(("amount", "Amount can have at most 2 decimal places"))
    else:
        return float(amount)
    return None


def find_vendor(data: dict) -> dict | None:
    """Resolve a vendor from `vendor_id`, or `vendor` as ERP code or exact name."""
    if str(data.get("vendor_id", "")).strip().isdigit():
        return db.query_one("SELECT * FROM vendors WHERE id = ?", (int(data["vendor_id"]),))
    ref = str(data.get("vendor", "")).strip()
    if not ref:
        return None
    return db.query_one("SELECT * FROM vendors WHERE LOWER(erp_code) = LOWER(?) OR LOWER(name) = LOWER(?)", (ref, ref))


def validate_bill(data: dict) -> tuple[dict | None, list[tuple[str, str]]]:
    """Validate raw bill input. Returns (clean_values, errors); errors are (field, message)."""
    errors: list[tuple[str, str]] = []
    vendor = find_vendor(data)
    if vendor is None:
        errors.append(("vendor", "Vendor is required" if not (data.get("vendor") or data.get("vendor_id"))
                       else f"Unknown vendor '{data.get('vendor') or data.get('vendor_id')}'"))
    invoice_number = str(data.get("invoice_number") or "").strip()
    if not invoice_number:
        errors.append(("invoice_number", "Invoice number is required"))
    amount = _parse_amount(data.get("amount"), errors)
    currency = str(data.get("currency") or "").strip().upper()
    if currency not in CURRENCIES:
        errors.append(("currency", f"Currency must be one of {', '.join(CURRENCIES)}"))
    issue = _parse_date(data.get("issue_date"), "Issue date", "issue_date", errors)
    due = _parse_date(data.get("due_date"), "Due date", "due_date", errors)
    if issue and due and due < issue:
        errors.append(("due_date", "Due date cannot be before the issue date"))
    if errors:
        return None, errors
    return {
        "vendor_id": vendor["id"], "invoice_number": invoice_number, "amount": amount, "currency": currency,
        "issue_date": issue.isoformat(), "due_date": due.isoformat(), "notes": str(data.get("notes") or "").strip(),
    }, []


def create_bill(clean: dict) -> int:
    """Insert a bill, raising DuplicateBill if vendor + invoice number already exist."""
    existing = db.query_one(
        "SELECT id FROM bills WHERE vendor_id = ? AND invoice_number = ?", (clean["vendor_id"], clean["invoice_number"]))
    if existing:
        raise DuplicateBill(existing["id"])
    try:
        return db.execute(
            "INSERT INTO bills (vendor_id, invoice_number, amount, currency, issue_date, due_date, notes, status, created_at)"
            " VALUES (:vendor_id, :invoice_number, :amount, :currency, :issue_date, :due_date, :notes, 'pending', :created_at)",
            {**clean, "created_at": datetime.now().isoformat(timespec="seconds")},
        )
    except sqlite3.IntegrityError:
        existing = db.query_one(
            "SELECT id FROM bills WHERE vendor_id = ? AND invoice_number = ?", (clean["vendor_id"], clean["invoice_number"]))
        raise DuplicateBill(existing["id"] if existing else 0) from None


# --------------------------------------------------------------------------- HTML UI
@router.get("", include_in_schema=False)
async def index(_: dict = Depends(user)):
    return redirect("/erp/bills")


@router.get("/bills")
async def bill_list(request: Request, q: str = "", _: dict = Depends(user)):
    sql, params = state.BILL_SELECT, []
    if q.strip():
        sql += " WHERE v.name LIKE ? OR b.invoice_number LIKE ?"
        params = [f"%{q.strip()}%"] * 2
    bills = db.query(sql + " ORDER BY b.id DESC", params)
    return render(request, "erp", "erp_bills.html", {"bills": bills, "q": q})


def _vendors() -> list[dict]:
    return db.query("SELECT id, name, erp_code FROM vendors ORDER BY name")


def _form_page(request: Request, values: dict, errors: list[tuple[str, str]], status: int = 200):
    return render(request, "erp", "erp_new_bill.html", {
        "values": values, "errors": errors, "error_fields": {f for f, _ in errors},
        "vendors": _vendors(), "currencies": CURRENCIES,
    }, status)


@router.get("/bills/new")
async def new_bill_form(request: Request, _: dict = Depends(user)):
    return _form_page(request, {"currency": "USD"}, [])


@router.post("/bills/new")
async def new_bill_submit(request: Request, session: dict = Depends(user)):
    form = await request.form()
    values = {f: str(form.get(f, "")).strip() for f in FORM_FIELDS}
    if chaos.is_on("erp_submit_500_once") and chaos.consume_once("erp_form_500"):
        return _form_page(request, values, [("", SERVER_ERROR_MESSAGE)], status=500)
    clean, errors = validate_bill(values)
    if clean is None:
        return _form_page(request, values, errors)
    try:
        bill_id = create_bill(clean)
    except DuplicateBill as dup:
        return _form_page(request, values, [("invoice_number", str(dup))])
    flash(session, f"Bill #{bill_id} created")
    return redirect(f"/erp/bills/{bill_id}")


@router.get("/bills/{bill_id}")
async def bill_detail(bill_id: int, request: Request, _: dict = Depends(user)):
    bill = state.get_bill(bill_id)
    if not bill:
        raise HTTPException(404, "Bill not found")
    return render(request, "erp", "erp_bill.html", {"bill": bill})


@router.post("/bills/{bill_id}/approve")
async def approve_bill(bill_id: int, session: dict = Depends(user)):
    bill = state.get_bill(bill_id)
    if not bill:
        raise HTTPException(404, "Bill not found")
    if bill["status"] == "approved":
        flash(session, f"Bill #{bill_id} was already approved", "error")
    else:
        db.execute("UPDATE bills SET status = 'approved' WHERE id = ?", (bill_id,))
        flash(session, f"Bill #{bill_id} approved for payment")
    return redirect(f"/erp/bills/{bill_id}")


# --------------------------------------------------------------------------- REST API
api = APIRouter(prefix="/erp/api", tags=["erp-api"])

API_SUMMARY = """Ledger ERP REST API (base: {base_url}/erp/api)
Auth: send header "X-API-Key: {api_key}" on every request except this one.

GET  /erp/api/vendors
     List the vendor master: [{{id, name, code}}].
GET  /erp/api/bills?vendor=<name or code>&invoice_number=<number>
     List AP bills. Both filters are optional; vendor is an exact (case-insensitive) name or code match.
GET  /erp/api/bills/{{id}}
     Fetch one bill (404 if unknown).
POST /erp/api/bills   (Content-Type: application/json)
     Create an AP bill. Body: {{"vendor": "<name or code>" (or "vendor_id": <int>), "invoice_number": "...",
     "amount": 1234.50, "currency": "USD|EUR|GBP", "issue_date": "YYYY-MM-DD", "due_date": "YYYY-MM-DD", "notes": "..."}}
     Returns 201 with the bill; 422 {{"detail", "errors"}} on validation failure (dates must be YYYY-MM-DD,
     amount > 0); 409 if a bill for the same vendor + invoice number already exists. A transient 500 may occur: retry.
"""


def require_api_key(request: Request) -> None:
    """The key must be the current tenant's own (the tenant itself was picked from this very header)."""
    supplied = request.headers.get("X-API-Key", "")
    if not secrets.compare_digest(supplied.encode(), tenants.current().api_key.encode()):
        raise HTTPException(401, "Invalid or missing API key")


def _api_bill(bill: dict) -> dict:
    return {k: bill[k] for k in ("id", "vendor", "vendor_code", "invoice_number", "amount", "currency",
                                 "issue_date", "due_date", "notes", "status", "created_at")}


@api.get("/docs-summary", response_class=PlainTextResponse)
async def docs_summary() -> str:
    return API_SUMMARY.format(base_url=config.BASE_URL, api_key=tenants.current().api_key)


@api.get("/vendors", dependencies=[Depends(require_api_key)])
async def api_vendors() -> list[dict]:
    return db.query("SELECT id, name, erp_code AS code FROM vendors ORDER BY name")


@api.get("/bills", dependencies=[Depends(require_api_key)])
async def api_bills(vendor: str | None = None, invoice_number: str | None = None) -> list[dict]:
    return [_api_bill(b) for b in state.get_bills(vendor, invoice_number)]


@api.get("/bills/{bill_id}", dependencies=[Depends(require_api_key)])
async def api_bill(bill_id: int) -> dict:
    bill = state.get_bill(bill_id)
    if not bill:
        raise HTTPException(404, "Bill not found")
    return _api_bill(bill)


@api.post("/bills", dependencies=[Depends(require_api_key)])
async def api_create_bill(request: Request) -> JSONResponse:
    if chaos.is_on("erp_submit_500_once") and chaos.consume_once("erp_api_500"):
        return JSONResponse({"detail": SERVER_ERROR_MESSAGE}, status_code=500)
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return JSONResponse({"detail": "Body must be a JSON object", "errors": ["Invalid JSON body"]}, status_code=422)
    clean, errors = validate_bill(data)
    if clean is None:
        return JSONResponse({"detail": "Validation failed", "errors": [m for _, m in errors]}, status_code=422)
    try:
        bill_id = create_bill(clean)
    except DuplicateBill as dup:
        return JSONResponse({"detail": str(dup), "existing_bill_id": dup.bill_id}, status_code=409)
    return JSONResponse(_api_bill(state.get_bill(bill_id)), status_code=201)
