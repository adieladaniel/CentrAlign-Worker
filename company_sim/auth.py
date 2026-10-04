"""Per-app cookie sessions: signed session id in a cookie, state kept server-side."""
from __future__ import annotations

import secrets
from urllib.parse import quote

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

from . import config

SESSIONS: dict[str, dict] = {}


class LoginRequired(Exception):
    """Raised by dependencies to redirect an anonymous visitor to the login page."""

    def __init__(self, url: str) -> None:
        self.url = url


class AppAuth:
    """Login/session handling for one app (mail, portal or erp)."""

    def __init__(self, key: str, prefix: str, username: str, password: str) -> None:
        self.key, self.prefix = key, prefix
        self.username, self.password = username, password
        self.cookie = f"{key}_session"
        self._signer = URLSafeSerializer(config.SESSION_SECRET, salt=key)

    @property
    def login_url(self) -> str:
        return f"{self.prefix}/login"

    def check_credentials(self, username: str, password: str) -> bool:
        return secrets.compare_digest(username.strip(), self.username) and secrets.compare_digest(password, self.password)

    def start_session(self, response: Response) -> dict:
        sid = secrets.token_hex(16)
        session = {"app": self.key, "user": self.username, "views": 0, "flash": []}
        SESSIONS[sid] = session
        response.set_cookie(self.cookie, self._signer.dumps(sid), httponly=True, samesite="lax", path="/")
        return session

    def current(self, request: Request) -> dict | None:
        token = request.cookies.get(self.cookie)
        if not token:
            return None
        try:
            sid = self._signer.loads(token)
        except BadSignature:
            return None
        session = SESSIONS.get(sid)
        if session is not None:
            session["sid"] = sid
        return session

    def end_session(self, request: Request, response: Response) -> None:
        session = self.current(request)
        if session:
            SESSIONS.pop(session["sid"], None)
        response.delete_cookie(self.cookie, path="/")

    def login_redirect_url(self, request: Request, expired: bool = False) -> str:
        url = self.login_url
        params = []
        if expired:
            params.append("expired=1")
        if request.method == "GET":
            params.append("next=" + quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""), safe=""))
        return url + ("?" + "&".join(params) if params else "")

    def require(self, request: Request) -> dict:
        """Return the session or raise LoginRequired."""
        session = self.current(request)
        if session is None:
            raise LoginRequired(self.login_redirect_url(request))
        request.state.session = session
        return session

    def expire(self, request: Request) -> None:
        """Drop the current session and redirect to login with an 'expired' notice."""
        session = self.current(request)
        if session:
            SESSIONS.pop(session["sid"], None)
        raise LoginRequired(self.login_redirect_url(request, expired=True))

    def safe_next(self, value: str | None) -> str | None:
        """Only allow redirects back into this app."""
        return value if value and value.startswith(self.prefix + "/") and not value.startswith("//") else None


mail_auth = AppAuth("mail", "/mail", config.MAIL_USER, config.MAIL_PASSWORD)
portal_auth = AppAuth("portal", "/portal", config.PORTAL_USER, config.PORTAL_PASSWORD)
erp_auth = AppAuth("erp", "/erp", config.ERP_USER, config.ERP_PASSWORD)
