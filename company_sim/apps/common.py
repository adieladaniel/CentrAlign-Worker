"""Shared helpers for the three apps: templates, flash messages, login routes."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .. import config
from ..auth import AppAuth

templates = Jinja2Templates(directory=str(config.TEMPLATE_DIR))

APPS: dict[str, dict] = {
    "mail": {
        "title": "Acme Mail",
        "nav": [("Inbox", "/mail/inbox"), ("Sent", "/mail/sent"), ("Compose", "/mail/compose")],
        "logout": "/mail/logout",
    },
    "portal": {
        "title": "Supplier Billing Portal",
        "nav": [("Invoices", "/portal/invoices")],
        "logout": "/portal/logout",
    },
    "erp": {
        "title": "Ledger - Accounts Payable",
        "nav": [("Bills", "/erp/bills"), ("New AP bill", "/erp/bills/new")],
        "logout": "/erp/logout",
    },
    "landing": {"title": "Acme Corp Intranet", "nav": [], "logout": ""},
}


def _fmt_dt(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%d %b %Y %H:%M")


templates.env.filters["fmt_dt"] = _fmt_dt
templates.env.filters["money"] = lambda v: f"{float(v):,.2f}"


def flash(session: dict | None, message: str, kind: str = "success") -> None:
    """Queue a flash message shown on the next rendered page."""
    if session is not None:
        session["flash"].append({"message": message, "kind": kind})


def render(request: Request, app_key: str, template: str, context: dict | None = None, status: int = 200) -> Response:
    """Render a template with the app chrome (nav, flash messages) injected."""
    session = getattr(request.state, "session", None)
    flashes = []
    if session is not None:
        flashes, session["flash"] = session["flash"], []
    app = APPS[app_key]
    ctx = {
        "app_key": app_key, "app_title": app["title"], "nav": app["nav"], "logout_url": app["logout"],
        "signed_in": session is not None, "flashes": flashes, **(context or {}),
    }
    return templates.TemplateResponse(request, template, ctx, status_code=status)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def add_login_routes(router: APIRouter, auth: AppAuth, app_key: str, username_label: str, home: str) -> None:
    """Attach GET/POST /login and POST /logout to a router."""

    def page(request: Request, error: str | None = None, expired: bool = False, next_url: str = "", status: int = 200):
        return render(request, app_key, "login.html", {
            "username_label": username_label, "login_url": auth.login_url, "error": error,
            "expired": expired, "next_url": next_url, "email_input": app_key == "mail",
        }, status)

    @router.get("/login", include_in_schema=False)
    async def login_form(request: Request, next: str = "", expired: int = 0):
        if auth.current(request) and not expired:
            return redirect(auth.safe_next(next) or home)
        return page(request, expired=bool(expired), next_url=next)

    @router.post("/login", include_in_schema=False)
    async def login_submit(request: Request):
        form = await request.form()
        username, password = str(form.get("username", "")), str(form.get("password", ""))
        next_url = str(form.get("next", ""))
        if not auth.check_credentials(username, password):
            return page(request, error="Invalid username or password.", next_url=next_url)
        response = redirect(auth.safe_next(next_url) or home)
        auth.start_session(response)
        return response

    @router.post("/logout", include_in_schema=False)
    async def logout(request: Request):
        response = redirect(auth.login_url)
        auth.end_session(request, response)
        return response
