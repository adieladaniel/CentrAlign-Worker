"""Structured logging for every process (API, workers): JSON or human-readable lines, run/org/user context, and a
redaction safety net.

* **Format** (``LOG_FORMAT``): ``json`` writes one object per line (``ts``, ``level``, ``logger``, ``msg`` plus the context
  fields and any ``extra=`` fields; a traceback goes in ``exc``). ``text`` is the human-readable line the project always
  had, with the context appended when there is one. Empty = ``json`` in DEPLOY_MODE, ``text`` otherwise.
* **Context** (``contextvars``): ``run_id``, ``org_id``, ``user_id``, ``worker_id`` and ``request_id`` are attached to every
  record emitted while they are bound. The HTTP middleware binds ``request_id`` (and the auth dependency the caller's org
  and user); a worker binds the run, org, user and worker around each run's tasks (``log_context``). asyncio copies the
  context into every task it creates, so child tasks inherit it.
* **Redaction safety net**: secrets must never reach the logs. Besides the per-run ``Redactor`` (``register_run_redactor``,
  filled by the runner for the length of a run, so a tool or a library that logs a vault secret is masked), every message,
  its arguments, tracebacks and string ``extra`` fields are scrubbed of database URL passwords, ``X-API-Key`` /
  ``Authorization`` / ``Cookie`` header values, bearer tokens, ``password=`` style pairs, Google API keys and the exact
  values of the process's own secrets (``register_secret``; ``setup_logging`` registers the configured ones). The scrub
  runs in a log *record factory*, i.e. when the record is created, so every handler (stderr, pytest's caplog, a
  collector) only ever sees clean records. ``RedactionFilter`` is the same logic as a ``logging.Filter``.

Call ``setup_logging(settings)`` once at process start (``worker.server.main``, ``worker.jobs.main``).
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping
from urllib.parse import quote

CONTEXT_FIELDS = ("run_id", "org_id", "user_id", "worker_id", "request_id")
MASK = "[REDACTED]"
MIN_SECRET_LEN = 6  # shorter registered values would shred ordinary text

_VARS: dict[str, contextvars.ContextVar[str | None]] = {
    name: contextvars.ContextVar(f"log_{name}", default=None) for name in CONTEXT_FIELDS
}


# --------------------------------------------------------------------------- context


def bind_context(**fields: Any) -> dict[str, contextvars.Token]:
    """Bind context fields (``None`` values are skipped) and return the tokens for ``reset_context``."""
    tokens: dict[str, contextvars.Token] = {}
    for name, value in fields.items():
        if name not in _VARS:
            raise ValueError(f"unknown log context field {name!r}")
        if value is not None:
            tokens[name] = _VARS[name].set(str(value))
    return tokens


def reset_context(tokens: Mapping[str, contextvars.Token]) -> None:
    for name, token in tokens.items():
        try:
            _VARS[name].reset(token)
        except ValueError:  # reset from another context (e.g. a task boundary): nothing to restore there
            pass


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind fields for the duration of a ``with`` block (tasks created inside inherit them)."""
    tokens = bind_context(**fields)
    try:
        yield
    finally:
        reset_context(tokens)


def current_context() -> dict[str, str]:
    """The context fields bound right now."""
    return {name: value for name, var in _VARS.items() if (value := var.get()) is not None}


def isolate_identity() -> dict[str, contextvars.Token]:
    """Pin ``org_id`` and ``user_id`` to their current values and return tokens that restore them. The HTTP middleware
    uses this so identity bound by an auth dependency cannot outlive its request when the app runs inside the caller's
    task (as under ``httpx.ASGITransport``)."""
    return {name: _VARS[name].set(_VARS[name].get()) for name in ("org_id", "user_id")}


# --------------------------------------------------------------------------- redaction safety net

_URL_PASSWORD = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@/]+)(@)")
_AUTH_HEADER = re.compile(
    r"""(?ix)\b(authorization|proxy-authorization)(["']?\s*[:=]\s*["']?)((?:bearer|basic|token)\s+)?([^\s"',;}]+)""")
_KEY_HEADER = re.compile(
    r"""(?ix)\b(x-api-key|x-admin-token|x-goog-api-key|x-auth-token)(["']?\s*[:=]\s*["']?)([^\s"',;}]+)""")
_COOKIE_HEADER = re.compile(r"""(?ix)\b(cookie|set-cookie)(["']?\s*[:=]\s*["']?)([^\r\n"']+)""")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}")
_GOOGLE_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{30,}")
_PAIR = re.compile(
    r"""(?ix)(["']?\b(?:password|passwd|api[_-]?key|secret|client_secret|access_token|session_secret)["']?)"""
    r"""(\s*=\s*|\s*:\s*["'])([^\s"'&,;}]+)""")

_static_secrets: dict[str, None] = {}  # the process's own secrets (insertion ordered, used as a set)
_run_redactors: dict[str, Any] = {}  # run id -> worker.redaction.Redactor of the run


def register_secret(value: str | None) -> None:
    """Mask the exact value (and its URL-quoted form) in every log record from now on."""
    if value and len(value.strip()) >= MIN_SECRET_LEN:
        _static_secrets[value.strip()] = None
        _static_secrets[quote(value.strip(), safe="")] = None


def register_run_redactor(run_id: str, redactor: Any) -> None:
    """Mask the secrets a run's ``Redactor`` knows, until ``clear_run_redactor``."""
    if redactor:
        _run_redactors[run_id] = redactor


def clear_run_redactor(run_id: str) -> None:
    _run_redactors.pop(run_id, None)


def clear_secrets() -> None:
    """Forget every registered secret and run redactor (tests)."""
    _static_secrets.clear()
    _run_redactors.clear()


def redact_text(text: str) -> str:
    """``text`` with secrets masked: known values first, then anything that looks like a credential."""
    if not text:
        return text
    for secret in _static_secrets:
        if secret in text:
            text = text.replace(secret, MASK)
    for redactor in list(_run_redactors.values()):
        text = redactor.redact(text)
    text = _URL_PASSWORD.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", text)
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}{MASK}", text)
    text = _KEY_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    text = _COOKIE_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    text = _BEARER.sub(f"Bearer {MASK}", text)
    text = _GOOGLE_KEY.sub(MASK, text)
    return _PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)


_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("x", 0, "x", 0, "", (), None)).keys()) | {
    "message", "asctime", "taskName", "ctx", "color_message"}  # color_message: uvicorn's ANSI duplicate of msg


class RedactionFilter(logging.Filter):
    """Scrubs a record's message (arguments already applied), traceback, stack and string ``extra`` fields. Passes every
    record. Idempotent: running it twice (record factory, then handler) changes nothing the second time."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed format string must not break logging
            message = str(record.msg)
        record.msg, record.args = redact_text(message), ()
        if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        for key, value in list(record.__dict__.items()):
            if key not in _STANDARD_ATTRS and isinstance(value, str):
                setattr(record, key, redact_text(value))
        return True


_filter = RedactionFilter()
_factory_marker = "_centralign_factory"


def install_record_factory() -> None:
    """Make every new ``LogRecord`` carry the bound context (``record.ctx``) and arrive already redacted."""
    current = logging.getLogRecordFactory()
    if getattr(current, _factory_marker, False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = current(*args, **kwargs)
        record.ctx = current_context()
        _filter.filter(record)
        return record

    setattr(factory, _factory_marker, True)
    logging.setLogRecordFactory(factory)


# --------------------------------------------------------------------------- formatters


def _iso(created: float) -> str:
    return datetime.fromtimestamp(created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    """One JSON object per record: ts, level, logger, msg, context fields, extras, exc."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {"ts": _iso(record.created), "level": record.levelname, "logger": record.name,
                                "msg": record.getMessage()}
        data.update(getattr(record, "ctx", None) or current_context())
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in data and not key.startswith("_"):
                data[key] = value
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            data["exc"] = record.exc_text
        if record.stack_info:
            data["stack"] = record.stack_info
        return json.dumps(data, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """The classic ``time LEVEL logger: message`` line, with `` [run=… org=…]`` appended when a context is bound."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        ctx = getattr(record, "ctx", None) or {}
        if not ctx:
            return line
        suffix = " ".join(f"{k.removesuffix('_id')}={v[:8] if k != 'worker_id' else v}" for k, v in ctx.items())
        head, sep, tail = line.partition("\n")  # keep the traceback after the context
        return f"{head} [{suffix}]{sep}{tail}"


# --------------------------------------------------------------------------- setup

_HANDLER_NAME = "centralign"


def resolve_format(settings: Any) -> str:
    chosen = (getattr(settings, "LOG_FORMAT", "") or "").strip().lower()
    if chosen in ("json", "text"):
        return chosen
    return "json" if getattr(settings, "DEPLOY_MODE", False) else "text"


def setup_logging(settings: Any = None, *, stream: Any = None, adopt_uvicorn: bool = True) -> logging.Handler:
    """Install the process's logging: one root handler in the chosen format, the context, the redaction. Idempotent (a
    second call replaces the handler). Returns the handler."""
    if settings is None:
        from worker.config import get_settings

        settings = get_settings()
    for secret in (settings.GEMINI_API_KEY, settings.VAULT_KEY, settings.SESSION_SECRET, settings.SIM_ADMIN_TOKEN,
                   settings.METRICS_TOKEN, settings.ACCESS_PASSWORD):
        register_secret(secret)
    register_secret(_database_password(settings.DATABASE_URL))
    install_record_factory()

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter() if resolve_format(settings) == "json" else TextFormatter())
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    for old in [h for h in root.handlers if h.get_name() == _HANDLER_NAME]:
        root.removeHandler(old)
    root.addHandler(handler)
    level = getattr(logging, str(settings.LOG_LEVEL).strip().upper(), None)
    root.setLevel(level if isinstance(level, int) else logging.INFO)
    if root.level > logging.DEBUG:
        for noisy in ("httpx", "httpcore", "websockets"):  # one line per HTTP call is noise (and sometimes a URL)
            logging.getLogger(noisy).setLevel(logging.WARNING)
    if adopt_uvicorn:
        for name in ("uvicorn", "uvicorn.error"):  # uvicorn's own handlers would print a second, unstructured format
            lg = logging.getLogger(name)
            lg.handlers.clear()
            lg.propagate = True
        logging.getLogger("uvicorn.access").disabled = True  # the request middleware logs every request itself
    return handler


def setup_if_unconfigured(settings: Any) -> None:
    """For servers started without ``main()`` (``uvicorn worker.server:app``): configure logging unless something already
    did (a root handler exists, e.g. under pytest)."""
    if not logging.getLogger().handlers:
        setup_logging(settings)


def _database_password(url: str) -> str | None:
    match = _URL_PASSWORD.search(url or "")
    return match.group(2) if match else None
