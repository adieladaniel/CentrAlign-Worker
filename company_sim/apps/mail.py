"""Mail app: the shared AP inbox (login, inbox/sent lists, message view, attachments, compose/reply)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from .. import config, db, tenants
from ..auth import mail_auth
from .common import add_login_routes, flash, redirect, render

router = APIRouter(prefix="/mail", tags=["mail"])
add_login_routes(router, mail_auth, "mail", "Email address", "/mail/inbox")


def user(request: Request) -> dict:
    return mail_auth.require(request)


@router.get("", include_in_schema=False)
async def index(_: dict = Depends(user)):
    return redirect("/mail/inbox")


def _folder(request: Request, folder: str):
    rows = db.query("SELECT * FROM emails WHERE folder = ? ORDER BY sent_at DESC, id DESC", (folder,))
    return render(request, "mail", "mail_list.html", {"folder": folder, "messages": rows})


@router.get("/inbox")
async def inbox(request: Request, _: dict = Depends(user)):
    return _folder(request, "inbox")


@router.get("/sent")
async def sent(request: Request, _: dict = Depends(user)):
    return _folder(request, "sent")


@router.get("/messages/{message_id}")
async def view_message(message_id: int, request: Request, _: dict = Depends(user)):
    msg = db.query_one("SELECT * FROM emails WHERE id = ?", (message_id,))
    if not msg:
        raise HTTPException(404, "Message not found")
    db.execute("UPDATE emails SET is_read = 1 WHERE id = ?", (message_id,))
    attachment = f"Invoice_{msg['attachment_invoice']}.pdf" if msg["attachment_invoice"] else None
    return render(request, "mail", "mail_message.html", {"msg": msg, "attachment": attachment})


@router.get("/messages/{message_id}/attachment")
async def download_attachment(message_id: int, _: dict = Depends(user)):
    msg = db.query_one("SELECT attachment_invoice FROM emails WHERE id = ?", (message_id,))
    if not msg or not msg["attachment_invoice"]:
        raise HTTPException(404, "Attachment not found")
    path = tenants.pdf_dir() / f"{msg['attachment_invoice']}.pdf"
    if not path.exists():
        raise HTTPException(404, "Attachment file missing")
    return FileResponse(path, media_type="application/pdf", filename=f"Invoice_{msg['attachment_invoice']}.pdf")


def _compose_page(request: Request, values: dict, error: str | None = None):
    return render(request, "mail", "mail_compose.html", {"values": values, "error": error})


@router.get("/compose")
async def compose_form(request: Request, reply_to: int | None = None, _: dict = Depends(user)):
    values = {"to": "", "subject": "", "body": "", "reply_to": ""}
    if reply_to:
        orig = db.query_one("SELECT * FROM emails WHERE id = ?", (reply_to,))
        if orig:
            subject = orig["subject"]
            quoted = "\n".join("> " + line for line in orig["body"].splitlines())
            values = {
                "to": orig["sender"], "reply_to": str(reply_to),
                "subject": subject if subject.lower().startswith("re:") else f"Re: {subject}",
                "body": f"\n\nOn {orig['sent_at'][:10]}, {orig['sender']} wrote:\n{quoted}",
            }
    return _compose_page(request, values)


@router.post("/compose")
async def compose_submit(request: Request, session: dict = Depends(user)):
    form = await request.form()
    values = {k: str(form.get(k, "")).strip() for k in ("to", "subject", "reply_to")}
    values["body"] = str(form.get("body", ""))
    if "@" not in values["to"]:
        return _compose_page(request, values, "Recipient must be a valid email address.")
    if not values["subject"]:
        return _compose_page(request, values, "Subject is required.")
    reply_to = int(values["reply_to"]) if values["reply_to"].isdigit() else None
    sent_at = datetime.combine(config.SIM_TODAY, datetime.now().time().replace(microsecond=0)).isoformat(timespec="minutes")
    db.execute(
        "INSERT INTO emails (folder, sender, recipient, subject, body, sent_at, in_reply_to, is_read)"
        " VALUES ('sent', ?, ?, ?, ?, ?, ?, 1)",
        (tenants.current_credentials("mail")["username"], values["to"], values["subject"], values["body"], sent_at, reply_to),
    )
    flash(session, "Message sent")
    return redirect("/mail/sent")
