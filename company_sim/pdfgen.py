"""Invoice PDF generation (reportlab, standard fonts so pypdf extracts text cleanly)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BILL_TO = ["Acme Corporation", "Accounts Payable", "500 Market Street, Suite 12", "San Francisco, CA 94105"]


def human_date(d: date) -> str:
    """Format as '15 November 2026' (deliberately not ISO)."""
    return f"{d.day} {d.strftime('%B %Y')}"


def money(value: float) -> str:
    return f"{value:,.2f}"


def totals(items: list[tuple[str, float, float]], tax_rate: float) -> tuple[float, float, float]:
    """Return (subtotal, tax, total) for line items of (description, qty, unit_price)."""
    subtotal = round(sum(q * p for _, q, p in items), 2)
    tax = round(subtotal * tax_rate, 2)
    return subtotal, tax, round(subtotal + tax, 2)


def generate_invoice_pdf(
    path: Path,
    *,
    vendor_name: str,
    vendor_address: str,
    number: str,
    issue: date,
    due: date,
    currency: str,
    tax_rate: float,
    items: list[tuple[str, float, float]],
) -> None:
    """Write an invoice PDF to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    name_style = ParagraphStyle("vendor", parent=styles["Title"], alignment=0, fontSize=20, spaceAfter=2)
    title_style = ParagraphStyle("inv", parent=styles["Heading1"], fontSize=26, textColor=colors.HexColor("#334155"))
    right = ParagraphStyle("right", parent=normal, alignment=2)
    bold_right = ParagraphStyle("boldright", parent=right, fontName="Helvetica-Bold", fontSize=12)

    subtotal, tax, total = totals(items, tax_rate)
    tax_label = f"{'Sales tax' if currency == 'USD' else 'VAT'} ({tax_rate * 100:.0f}%)"

    story = [
        Paragraph(vendor_name, name_style),
        Paragraph(vendor_address.replace("\n", "<br/>"), normal),
        Spacer(1, 10 * mm),
        Paragraph("INVOICE", title_style),
        Paragraph(f"<b>Invoice number:</b> {number}", normal),
        Paragraph(f"<b>Issue date:</b> {human_date(issue)}", normal),
        Paragraph(f"<b>Due date:</b> {human_date(due)}", normal),
        Paragraph(f"<b>Currency:</b> {currency}", normal),
        Spacer(1, 6 * mm),
        Paragraph("<b>Bill to:</b><br/>" + "<br/>".join(BILL_TO), normal),
        Spacer(1, 8 * mm),
    ]

    rows = [["Description", "Qty", f"Unit price ({currency})", f"Amount ({currency})"]]
    for desc, qty, price in items:
        rows.append([desc, f"{qty:g}", money(price), money(qty * price)])
    table = Table(rows, colWidths=[85 * mm, 18 * mm, 37 * mm, 37 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#94a3b8")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story += [
        table,
        Spacer(1, 6 * mm),
        Paragraph(f"Subtotal: {currency} {money(subtotal)}", right),
        Paragraph(f"{tax_label}: {currency} {money(tax)}", right),
        Spacer(1, 2 * mm),
        Paragraph(f"Total due: {currency} {money(total)}", bold_right),
        Spacer(1, 12 * mm),
        Paragraph(
            f"Payment terms: please pay by {human_date(due)}. Quote invoice number {number} with your payment.",
            normal,
        ),
        Paragraph(f"Thank you for your business. - {vendor_name}", normal),
    ]
    SimpleDocTemplate(
        str(path), pagesize=A4, title=f"Invoice {number}", author=vendor_name,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm, bottomMargin=20 * mm,
    ).build(story)
