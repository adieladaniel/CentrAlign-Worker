"""Deterministic seed data. Run with `python -m company_sim.seed [flag,flag]`."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, time, timedelta

from . import chaos, config, db
from .pdfgen import generate_invoice_pdf, totals

VENDORS = [
    # id, name, erp_code, address, email
    (1, "Company X Ltd", "CX-001", "14 Harbour Road\nBristol BS1 4QA\nUnited Kingdom", "billing@companyx.example"),
    (2, "Company X Holdings", "CXH-002", "2 Canary Wharf Tower\nLondon E14 5AB\nUnited Kingdom", "finance@companyx-holdings.example"),
    (3, "Globex Corporation", "GLX-003", "1 Globex Plaza\nSpringfield, IL 62701\nUnited States", "invoices@globex.example"),
    (4, "Initech Supplies", "INI-004", "4120 Freidrich Lane\nAustin, TX 78744\nUnited States", "accounts@initech.example"),
    (5, "Umbrella Logistics", "UMB-005", "77 Raccoon Street\nRaccoon City, MO 64101\nUnited States", "ar@umbrella-logistics.example"),
    (6, "Stark Components", "STK-006", "200 Park Avenue\nNew York, NY 10166\nUnited States", "billing@stark-components.example"),
]
VENDOR_ID = {name: vid for vid, name, *_ in VENDORS}
TAX_BY_CURRENCY = {"USD": 0.08, "EUR": 0.19, "GBP": 0.20}
EMAIL_RECIPIENT = "ap@acme.example"


def month_end(d: date) -> date:
    return ((d.replace(day=28) + timedelta(days=4)).replace(day=1)) - timedelta(days=1)


def invoice_specs(today: date, flags: set[str]) -> list[dict]:
    """Portal invoices in internal-id order (deliberately not date order)."""
    ago = lambda n: today - timedelta(days=n)  # noqa: E731
    this_month_due = lambda k: max(month_end(today) - timedelta(days=k), today.replace(day=1))  # noqa: E731

    def spec(vendor, number, issue, due, status, currency, items, visible=True):
        return dict(vendor=vendor, number=number, issue=issue, due=due, status=status,
                    currency=currency, items=items, visible=visible)

    cx_latest_issue = ago(5)
    cx_mid_issue = ago(41)
    cx_old_issue = ago(75)
    glx_due = this_month_due(12)
    umb_due = this_month_due(5)

    specs = [
        spec("Globex Corporation", "GLX-88213", glx_due - timedelta(days=30), glx_due, "open", "USD",
             [("Industrial sensors, model G-200", 40, 112.50), ("Calibration service", 1, 640.00)]),
        spec("Initech Supplies", "INI-20417", ago(62), ago(32), "paid", "GBP",
             [("Printer toner cartridges (box of 10)", 12, 184.00), ("Delivery", 1, 45.00)]),
        spec("Company X Ltd", f"CX-{cx_old_issue.year}-0388", cx_old_issue, cx_old_issue + timedelta(days=30), "paid", "EUR",
             [("Managed IT services - monthly retainer", 1, 3600.00), ("Software licences (seats)", 25, 18.00)]),
        spec("Umbrella Logistics", "UMB-5531", umb_due - timedelta(days=30), umb_due, "open", "USD",
             [("Freight - pallet shipment, west coast", 8, 410.00), ("Fuel surcharge", 1, 215.50)]),
        spec("Stark Components", "STK-1207", ago(33), ago(3), "paid", "EUR",
             [("Titanium fasteners (pack of 100)", 30, 64.00), ("Expedited handling", 1, 120.00)]),
        spec("Globex Corporation", "GLX-87990", ago(48), ago(18), "paid", "USD",
             [("Hydraulic valves, model H-7", 16, 230.00)]),
        spec("Company X Ltd", f"CX-{cx_latest_issue.year}-0412", cx_latest_issue, cx_latest_issue + timedelta(days=30), "open", "EUR",
             [("Managed IT services - monthly retainer", 1, 3800.00), ("On-site engineering support (hours)", 12, 95.00),
              ("Security monitoring add-on", 1, 450.00)], visible="portal_missing_invoice" not in flags),
        spec("Initech Supplies", "INI-20502", ago(14), ago(14) + timedelta(days=30), "open", "GBP",
             [("Ergonomic office chairs", 6, 245.00), ("Standing desks", 3, 520.00)]),
        spec("Stark Components", "STK-1244", ago(8), ago(8) + timedelta(days=30), "open", "EUR",
             [("Carbon fibre panels", 20, 148.00), ("Custom machining", 1, 975.00)]),
        spec("Umbrella Logistics", "UMB-5470", ago(55), ago(25), "paid", "USD",
             [("Warehousing - monthly", 1, 2800.00), ("Pallet handling", 120, 6.50)]),
        spec("Company X Ltd", f"CX-{cx_mid_issue.year}-0401", cx_mid_issue, cx_mid_issue + timedelta(days=30), "open", "EUR",
             [("Managed IT services - monthly retainer", 1, 3800.00), ("Backup storage (TB)", 8, 42.00)]),
        spec("Globex Corporation", "GLX-88301", ago(3), ago(3) + timedelta(days=30), "open", "USD",
             [("Control panels, model C-12", 10, 389.00), ("Installation manual set", 10, 12.00)]),
        spec("Initech Supplies", "INI-20388", ago(90), ago(60), "paid", "GBP",
             [("Stationery bundle", 25, 38.00)]),
        spec("Stark Components", "STK-1180", ago(71), ago(41), "paid", "EUR",
             [("Steel brackets (pack of 50)", 22, 71.00), ("Packaging", 1, 60.00)]),
    ]
    if "ambiguous_vendor" in flags:
        specs.append(spec("Company X Holdings", f"CXH-{cx_latest_issue.year}-0077", cx_latest_issue,
                          cx_latest_issue + timedelta(days=30), "open", "EUR",
                          [("Corporate advisory services", 1, 1950.00), ("Travel expenses", 1, 360.00)]))
    return specs


def _dt(today: date, days_ago: int, hour: int, minute: int) -> str:
    return datetime.combine(today - timedelta(days=days_ago), time(hour, minute)).isoformat(timespec="minutes")


def email_specs(today: date, latest_cx: str) -> list[dict]:
    """Inbox messages (dict keys match the emails table)."""
    def mail(sender, subject, body, days_ago, hour, minute, attachment=None, is_read=0):
        return dict(folder="inbox", sender=sender, recipient=EMAIL_RECIPIENT, subject=subject, body=body,
                    sent_at=_dt(today, days_ago, hour, minute), attachment_invoice=attachment, is_read=is_read)

    return [
        mail("Dana Whitfield <billing@companyx.example>", "Latest invoice from Company X Ltd",
             "Hello,\n\nPlease find attached our latest invoice.\n\nPayment terms are net 30 days from the issue date. "
             "Let us know if you have any questions.\n\nKind regards,\nDana Whitfield\nAccounts Receivable, Company X Ltd",
             4, 9, 14, attachment=latest_cx),
        mail("Globex Billing <invoices@globex.example>", "Invoice GLX-88213 from Globex Corporation",
             "Dear Accounts Payable,\n\nAttached is invoice GLX-88213 for recent deliveries.\n\nRegards,\nGlobex Billing Team",
             18, 14, 2, attachment="GLX-88213", is_read=1),
        mail("Umbrella Logistics AR <ar@umbrella-logistics.example>", "Umbrella Logistics - invoice UMB-5531",
             "Hello,\n\nPlease see the attached invoice UMB-5531 covering last month's shipments.\n\nThanks,\nUmbrella AR",
             6, 11, 40, attachment="UMB-5531"),
        mail("Acme Communications <newsletter@acme.example>", "Acme Weekly Digest: Q4 finance calendar",
             "This week at Acme:\n- Q4 close timeline published\n- New expense policy FAQ\n- Town hall recording available\n\n"
             "To unsubscribe, reply with 'unsubscribe'.", 2, 7, 30),
        mail("Priya Nair <priya.nair@acme.example>", "AP team sync - Thursday 10:00",
             "Hi all,\n\nReminder that we have the AP team sync on Thursday at 10:00 in meeting room 3. "
             "Agenda: month-end accruals, vendor onboarding.\n\nPriya", 1, 16, 5, is_read=1),
        mail("IT Helpdesk <helpdesk@acme.example>", "Reminder: password rotation due",
             "Your shared mailbox password is due for rotation within 30 days. Contact the helpdesk if you need assistance.",
             9, 8, 0, is_read=1),
        mail("Initech Accounts <accounts@initech.example>", "Question: payment date for INI-20502",
             "Hello,\n\nCould you please confirm the expected payment date for invoice INI-20502? "
             "Our finance team is reconciling this week.\n\nThank you,\nMarcus\nInitech Supplies - Accounts", 3, 15, 22),
        mail("HR Team <hr@acme.example>", "Office closed this Friday",
             "Just a reminder that the office will be closed this Friday for the company offsite. Enjoy the long weekend!",
             12, 10, 10, is_read=1),
    ]


def seed_database(flags: set[str] | None = None) -> dict:
    """Wipe and reseed the database and PDFs. Returns a short summary."""
    flags = chaos.active() if flags is None else set(flags)
    today = config.SIM_TODAY
    specs = invoice_specs(today, flags)
    vendors = {v[1]: v for v in VENDORS}

    config.PDF_DIR.mkdir(parents=True, exist_ok=True)
    for old in config.PDF_DIR.glob("*.pdf"):
        old.unlink()

    with db.transaction() as conn:
        db.recreate_schema(conn)
        conn.executemany("INSERT INTO vendors VALUES (?,?,?,?,?)", VENDORS)

        for i, s in enumerate(specs, start=1):
            _, _, total = totals(s["items"], TAX_BY_CURRENCY[s["currency"]])
            s["amount"] = total
            s["id"] = i
            conn.execute(
                "INSERT INTO invoices (id, number, vendor_id, issue_date, due_date, amount, currency, tax_rate, status, portal_visible)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (i, s["number"], VENDOR_ID[s["vendor"]], s["issue"].isoformat(), s["due"].isoformat(), total,
                 s["currency"], TAX_BY_CURRENCY[s["currency"]], s["status"], int(s["visible"])),
            )
            conn.executemany(
                "INSERT INTO invoice_items (invoice_id, description, quantity, unit_price) VALUES (?,?,?,?)",
                [(i, d, q, p) for d, q, p in s["items"]],
            )
            v = vendors[s["vendor"]]
            generate_invoice_pdf(
                config.PDF_DIR / f"{s['number']}.pdf", vendor_name=v[1], vendor_address=v[3], number=s["number"],
                issue=s["issue"], due=s["due"], currency=s["currency"], tax_rate=TAX_BY_CURRENCY[s["currency"]],
                items=s["items"],
            )

        by_number = {s["number"]: s for s in specs}
        cx_ltd = [s for s in specs if s["vendor"] == "Company X Ltd"]
        cx_latest = max(cx_ltd, key=lambda s: s["issue"])
        cx_oldest = min(cx_ltd, key=lambda s: s["issue"])

        for e in email_specs(today, cx_latest["number"]):
            conn.execute(
                "INSERT INTO emails (folder, sender, recipient, subject, body, sent_at, attachment_invoice, is_read)"
                " VALUES (:folder,:sender,:recipient,:subject,:body,:sent_at,:attachment_invoice,:is_read)", e)

        def add_bill(bill_id: int, s: dict, days_ago: int, status: str, notes: str = "") -> None:
            conn.execute(
                "INSERT INTO bills (id, vendor_id, invoice_number, amount, currency, issue_date, due_date, notes, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (bill_id, VENDOR_ID[s["vendor"]], s["number"], s["amount"], s["currency"], s["issue"].isoformat(),
                 s["due"].isoformat(), notes, status, _dt(today, days_ago, 10, 0)))

        add_bill(1001, by_number["GLX-87990"], 40, "approved")
        add_bill(1002, by_number["UMB-5470"], 48, "approved")
        add_bill(1003, by_number["INI-20388"], 80, "approved")
        add_bill(1004, by_number["STK-1180"], 60, "approved")
        add_bill(1005, cx_oldest, 70, "approved", "Entered from vendor email")
        if "duplicate_bill" in flags:
            add_bill(1006, cx_latest, 1, "pending", "Entered from vendor email")

    return {"flags": sorted(flags), "invoices": len(specs), "latest_company_x": cx_latest["number"]}


def write_workspace(overwrite: bool = True) -> None:
    """Write workspace/credentials.json and README.txt for the agent to discover."""
    config.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    creds = {
        "mail": {"url": f"{config.BASE_URL}/mail", "username": config.MAIL_USER, "password": config.MAIL_PASSWORD,
                 "description": "Shared AP mailbox"},
        "vendor_portal": {"url": f"{config.BASE_URL}/portal", "username": config.PORTAL_USER,
                          "password": config.PORTAL_PASSWORD, "description": "Supplier billing portal (vendor invoices)"},
        "erp": {"url": f"{config.BASE_URL}/erp", "username": config.ERP_USER, "password": config.ERP_PASSWORD,
                "api_base_url": f"{config.BASE_URL}/erp/api", "api_key_header": "X-API-Key",
                "api_key": config.ERP_API_KEY, "description": "Ledger: internal accounting system (AP bills)"},
    }
    files = {
        "credentials.json": json.dumps(creds, indent=2) + "\n",
        "README.txt": "Company shared drive. credentials.json has system logins.\n",
    }
    for name, content in files.items():
        path = config.WORKSPACE_DIR / name
        if overwrite or not path.exists():
            path.write_text(content, encoding="utf-8")


def main() -> None:
    if len(sys.argv) > 1:
        chaos.set_flags(sys.argv[1].split(","))
    chaos.load_from_env()
    print(json.dumps(seed_database(), indent=2))
    write_workspace()


if __name__ == "__main__":
    main()
