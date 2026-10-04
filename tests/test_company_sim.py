"""Tests for the simulated company environment (FastAPI TestClient, isolated temp DB)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from company_sim import chaos, config, state

API_KEY = {"X-API-Key": config.ERP_API_KEY}
VALID_FORM = {
    "vendor": "CX-001", "invoice_number": "CX-TEST-1", "amount": "1234.50", "currency": "EUR",
    "issue_date": "2026-09-28", "due_date": "2026-10-28", "notes": "",
}


@pytest.fixture()
def sim(tmp_path, monkeypatch):
    """Fresh, isolated simulator (own DB) with no chaos."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "sim.db")
    monkeypatch.setattr(config, "PDF_DIR", tmp_path / "pdfs")
    from company_sim.app import app

    chaos.set_flags([])
    with TestClient(app) as client:
        client.post("/admin/reset", json={"chaos": []})
        yield client
    chaos.set_flags([])


@pytest.fixture()
def erp(sim):
    sim.post("/erp/login", data={"username": config.ERP_USER, "password": config.ERP_PASSWORD})
    return sim


def test_login_required_redirects(sim):
    r = sim.get("/erp/bills", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/erp/login")


def test_bad_date_format_shows_alert_and_keeps_values(erp):
    r = erp.post("/erp/bills/new", data={**VALID_FORM, "issue_date": "28 September 2026"})
    assert 'role="alert"' in r.text and "YYYY-MM-DD" in r.text
    assert 'value="28 September 2026"' in r.text and 'value="1234.50"' in r.text
    assert state.get_bills(invoice_number="CX-TEST-1") == []


@pytest.mark.parametrize("amount,message", [("0", "greater than 0"), ("-3", "greater than 0"), ("abc", "must be a number"), ("", "required")])
def test_amount_validation(erp, amount, message):
    r = erp.post("/erp/bills/new", data={**VALID_FORM, "amount": amount})
    assert message in r.text and state.get_bills(invoice_number="CX-TEST-1") == []


def test_required_fields(erp):
    r = erp.post("/erp/bills/new", data={})
    for text in ("Vendor is required", "Invoice number is required", "Issue date is required", "Due date is required"):
        assert text in r.text


def test_create_bill_and_duplicate_detection(erp):
    r = erp.post("/erp/bills/new", data=VALID_FORM)
    assert r.status_code == 200 and r.url.path == "/erp/bills/1006" and "Bill #1006 created" in r.text
    bill = state.get_bills("Company X Ltd", "CX-TEST-1")[0]
    assert bill["amount"] == 1234.50 and bill["due_date"] == "2026-10-28" and bill["currency"] == "EUR"
    dup = erp.post("/erp/bills/new", data=VALID_FORM)
    assert "Bill for this vendor and invoice number already exists (Bill #1006)" in dup.text
    assert len(state.get_bills(invoice_number="CX-TEST-1")) == 1


def test_approve_bill(erp):
    r = erp.post("/erp/bills/new", data=VALID_FORM)
    assert "Approve for payment" in r.text
    r = erp.post("/erp/bills/1006/approve")
    assert "approved for payment" in r.text and state.get_bills(invoice_number="CX-TEST-1")[0]["status"] == "approved"


def test_api_auth_and_validation(sim):
    assert sim.get("/erp/api/vendors").status_code == 401
    assert len(sim.get("/erp/api/vendors", headers=API_KEY).json()) == 6
    assert "X-API-Key" in sim.get("/erp/api/docs-summary").text
    body = {"vendor": "Globex Corporation", "invoice_number": "G-1", "amount": 10, "currency": "USD",
            "issue_date": "2026-09-01", "due_date": "2026-10-01"}
    assert sim.post("/erp/api/bills", headers=API_KEY, json={**body, "due_date": "1/10/2026"}).status_code == 422
    created = sim.post("/erp/api/bills", headers=API_KEY, json=body)
    assert created.status_code == 201 and created.json()["id"] == 1006
    dup = sim.post("/erp/api/bills", headers=API_KEY, json=body)
    assert dup.status_code == 409 and "already exists (Bill #1006)" in dup.json()["detail"]
    assert sim.get("/erp/api/bills/1006", headers=API_KEY).json()["invoice_number"] == "G-1"


def test_chaos_500_once_form_and_api(sim):
    sim.post("/admin/reset", json={"chaos": ["erp_submit_500_once"]})
    sim.post("/erp/login", data={"username": config.ERP_USER, "password": config.ERP_PASSWORD})
    first = sim.post("/erp/bills/new", data=VALID_FORM)
    assert first.status_code == 500 and "Internal Server Error" in first.text
    assert state.get_bills(invoice_number="CX-TEST-1") == []
    assert sim.post("/erp/bills/new", data=VALID_FORM).status_code == 200
    body = {"vendor": "CX-001", "invoice_number": "CX-TEST-2", "amount": 5, "currency": "USD",
            "issue_date": "2026-09-01", "due_date": "2026-10-01"}
    assert sim.post("/erp/api/bills", headers=API_KEY, json=body).status_code == 500
    assert sim.post("/erp/api/bills", headers=API_KEY, json=body).status_code == 201


def test_session_expiry_chaos(sim):
    sim.post("/admin/reset", json={"chaos": ["erp_session_expiry"]})
    sim.post("/erp/login", data={"username": config.ERP_USER, "password": config.ERP_PASSWORD})  # view 1
    sim.get("/erp/bills/new")
    sim.get("/erp/bills/1001")
    r = sim.get("/erp/bills")
    assert r.url.path == "/erp/login" and "Your session has expired, please sign in again" in r.text


def test_reset_wipes_data_and_applies_chaos(erp):
    erp.post("/erp/bills/new", data=VALID_FORM)
    assert len(state.get_bills()) == 6
    erp.post("/admin/reset", json={"chaos": ["duplicate_bill"]})
    latest = state.latest_invoice_for("Company X Ltd")
    assert state.get_bills(invoice_number="CX-TEST-1") == []
    assert [b["invoice_number"] for b in state.get_bills("Company X Ltd")][-1] == latest["number"]
    erp.post("/admin/reset", json={"chaos": []})
    assert len(state.get_bills()) == 5 and state.get_sent_mail() == []


def test_seed_facts_and_portal_ordering(sim):
    latest = state.latest_invoice_for("Company X Ltd")
    cx = sorted(state.list_invoices("Company X Ltd"), key=lambda i: i["id"])
    assert latest["number"] == [i for i in cx if i["issue_date"] == max(x["issue_date"] for x in cx)][0]["number"]
    assert cx[0]["number"] != latest["number"]  # latest is not first in list order
    sim.post("/portal/login", data={"username": config.PORTAL_USER, "password": config.PORTAL_PASSWORD})
    page1 = sim.get("/portal/invoices?page=1").text
    page2 = sim.get("/portal/invoices?page=2").text
    assert latest["number"] not in page1 and latest["number"] in page2
    assert "Download PDF" in sim.get(f"/portal/invoices/{latest['number']}").text
    assert sim.get(f"/portal/invoices/{latest['number']}/pdf").headers["content-type"] == "application/pdf"


def test_ambiguous_and_missing_invoice_chaos(sim):
    sim.post("/admin/chaos", json={"chaos": ["ambiguous_vendor", "portal_missing_invoice"]})
    latest = state.latest_invoice_for("Company X Ltd")
    holdings = state.latest_invoice_for("Company X Holdings")
    assert holdings["issue_date"] == latest["issue_date"]
    sim.post("/portal/login", data={"username": config.PORTAL_USER, "password": config.PORTAL_PASSWORD})
    assert sim.get(f"/portal/invoices/{latest['number']}").status_code == 404
    assert sim.get(f"/portal/invoices/{holdings['number']}").status_code == 200


def test_mail_reply_stored_in_sent(sim):
    sim.post("/mail/login", data={"username": config.MAIL_USER, "password": config.MAIL_PASSWORD})
    form = sim.get("/mail/compose?reply_to=1").text
    assert "Re:" in form
    r = sim.post("/mail/compose", data={"to": "accounts@initech.example", "subject": "Re: payment", "body": "Paid on 5th"})
    assert "Message sent" in r.text
    sent = state.get_sent_mail()
    assert len(sent) == 1 and sent[0]["recipient"] == "accounts@initech.example"
