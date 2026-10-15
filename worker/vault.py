"""Credential vault: encrypted login/API credentials the agent can *use* without ever *seeing* them.

* ``Vault`` encrypts the secret part of every credential with Fernet (key: ``VAULT_KEY``) and keeps it in a
  ``VaultBackend``. ``FileVaultBackend`` stores one JSON file; a PostgreSQL backend implements the same async
  interface later, so callers do not change. The record keeps only non-secret metadata (name, kind, description,
  url/host hint, field *names*) in clear; every field *value* lives inside one encrypted token.
* Credentials are scoped by ``owner`` (an organisation id, or ``"default"``).
* ``VaultScope`` is the per-run snapshot (decrypted in memory) that tools read from ``ctx.extras["vault_scope"]``.
  Nothing in it is ever shown to the model: the tools in ``worker/tools/secrets.py`` use the values internally and
  ``worker/redaction.py`` scrubs them from everything that is stored or sent to the model.

Manage credentials with ``python -m worker.vault put|list|delete|rotate-key|generate-key`` (``--field password=-``
prompts for the value, so it never lands in shell history).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from worker.agent.state import utcnow
from worker.config import Settings, get_settings

log = logging.getLogger(__name__)

DEFAULT_OWNER = "default"
# Internal credentials: stored like any other, but never offered to the agent or listed in the credentials UI. Their
# values are still redacted. ``sim_entry`` holds the org's simulated-company entry token (see worker/provisioning.py).
HIDDEN_CREDENTIALS = frozenset({"sim_entry"})
KINDS = ("login", "api_key", "header")
# Fields that identify rather than authenticate: they are listed and may be shown, everything else is secret.
NON_SECRET_FIELDS = frozenset({"username", "user", "login", "email", "header_name"})
_OWNER_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_FIELD_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
VAULT_FILE = "vault.json"
KEY_FILE = ".vault_key"
DEFAULT_API_KEY_HEADER = "X-API-Key"


class VaultError(Exception):
    """A vault operation failed (bad input, missing or wrong key, unreadable file). Messages never contain secrets."""


# --------------------------------------------------------------------------- credentials


@dataclass
class Credential:
    """One stored credential. ``fields`` holds the values (secret ones included): never log or print it."""

    owner: str
    name: str
    kind: str
    fields: dict[str, str] = field(default_factory=dict, repr=False)
    description: str = ""
    url: str = ""
    host: str = ""
    created_at: str = ""
    updated_at: str = ""

    def secret_values(self) -> dict[str, str]:
        """``{"<name>.<field>": value}`` for everything that must never reach a log, the store or the model.
        Includes the Basic-auth token a login credential can produce."""
        secrets = {f"{self.name}.{key}": value for key, value in self.fields.items()
                   if key not in NON_SECRET_FIELDS and value}
        user, password = self.fields.get("username"), self.fields.get("password")
        if self.kind == "login" and user and password:
            secrets[f"{self.name}.basic_auth"] = base64.b64encode(f"{user}:{password}".encode()).decode()
        return secrets

    def auth_header(self) -> tuple[str, str] | None:
        """The HTTP header this credential authenticates with, or None when it has none.

        api_key: ``<header_name or X-API-Key>: <api_key>``; header: ``<header_name>: <value>``; login: Basic auth."""
        if self.kind == "api_key" and self.fields.get("api_key"):
            return self.fields.get("header_name") or DEFAULT_API_KEY_HEADER, self.fields["api_key"]
        if self.kind == "header":
            value = self.fields.get("value") or self.fields.get("header_value")
            return (self.fields["header_name"], value) if self.fields.get("header_name") and value else None
        if self.kind == "login" and self.fields.get("username") and self.fields.get("password"):
            token = base64.b64encode(f"{self.fields['username']}:{self.fields['password']}".encode()).decode()
            return "Authorization", f"Basic {token}"
        return None

    def allows_host(self, host: str) -> bool:
        """May this credential be sent to, or typed into a page on, ``host``? Always yes when it has no host hint;
        otherwise the host must be the hint or a subdomain of it, so a secret cannot be exfiltrated elsewhere."""
        host = (host or "").lower()
        return not self.host or host == self.host or host.endswith("." + self.host)

    def public(self) -> dict[str, Any]:
        """Metadata safe to show anywhere: names and hints, never values."""
        return {"owner": self.owner, "name": self.name, "kind": self.kind, "description": self.description,
                "url": self.url, "host": self.host, "fields": sorted(self.fields),
                "created_at": self.created_at, "updated_at": self.updated_at}


def is_hidden(name: str) -> bool:
    """Is ``name`` an internal credential that the agent and the credentials UI must not see?"""
    return name in HIDDEN_CREDENTIALS


def _host_of(url: str) -> str:
    return (urlparse(url if "//" in url else f"//{url}").hostname or "").lower() if url else ""


def _validate(owner: str, name: str, kind: str, fields: dict[str, str]) -> None:
    if not _OWNER_RE.match(owner or ""):
        raise VaultError("owner must be 1-64 characters of letters, digits and _ . @ -")
    if not _NAME_RE.match(name or ""):
        raise VaultError("name must be 1-64 characters of letters, digits and _ . -")
    if kind not in KINDS:
        raise VaultError(f"kind must be one of: {', '.join(KINDS)}")
    bad = [k for k in fields if not _FIELD_RE.match(k)]
    if bad:
        raise VaultError(f"invalid field name(s): {', '.join(bad)}")
    required = {"login": ["password"], "api_key": ["api_key"], "header": ["header_name"]}[kind]
    missing = [k for k in required if not fields.get(k)]
    if kind == "header" and not (fields.get("value") or fields.get("header_value")):
        missing.append("value")
    if missing:
        raise VaultError(f"a '{kind}' credential needs the field(s): {', '.join(missing)}")


# --------------------------------------------------------------------------- backends


class VaultBackend(Protocol):
    """Storage of opaque records ``{owner, name, kind, description, url, host, field_names, created_at, updated_at,
    secret}`` where ``secret`` is the encrypted token. A backend never sees a plaintext value."""

    async def put(self, record: dict) -> None:
        """Insert or replace the record identified by ``(owner, name)``."""

    async def get(self, owner: str, name: str) -> dict | None:
        """The record, or None."""

    async def list(self, owner: str) -> list[dict]:
        """All records of ``owner``, ordered by name."""

    async def delete(self, owner: str, name: str) -> bool:
        """Remove a record; False when there was none."""

    async def all(self) -> list[dict]:
        """Every record of every owner (key rotation)."""

    async def replace_all(self, records: list[dict]) -> None:
        """Atomically replace the whole content (key rotation)."""


class FileVaultBackend:
    """One JSON file, ``<directory>/vault.json``, written atomically. Safe for several threads of one process; use the
    PostgreSQL backend when several processes write."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.path = self.directory / VAULT_FILE
        self.key_file = self.directory / KEY_FILE
        self._lock = threading.Lock()

    def exists(self) -> bool:
        """Does the vault file exist (i.e. may it hold credentials that need the existing key)?"""
        return self.path.is_file()

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise VaultError(f"cannot read the vault file {self.path}: {exc}") from exc
        records = data.get("credentials") if isinstance(data, dict) else None
        return [r for r in records or [] if isinstance(r, dict)]

    def _write(self, records: list[dict]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "credentials": records}, indent=2), encoding="utf-8")
        _restrict(tmp)
        os.replace(tmp, self.path)

    async def put(self, record: dict) -> None:
        with self._lock:
            records = [r for r in self._read() if (r["owner"], r["name"]) != (record["owner"], record["name"])]
            self._write(sorted([*records, record], key=lambda r: (r["owner"], r["name"])))

    async def get(self, owner: str, name: str) -> dict | None:
        with self._lock:
            return next((r for r in self._read() if r["owner"] == owner and r["name"] == name), None)

    async def list(self, owner: str) -> list[dict]:
        with self._lock:
            return sorted((r for r in self._read() if r["owner"] == owner), key=lambda r: r["name"])

    async def delete(self, owner: str, name: str) -> bool:
        with self._lock:
            records = self._read()
            kept = [r for r in records if (r["owner"], r["name"]) != (owner, name)]
            if len(kept) == len(records):
                return False
            self._write(kept)
            return True

    async def all(self) -> list[dict]:
        with self._lock:
            return self._read()

    async def replace_all(self, records: list[dict]) -> None:
        with self._lock:
            self._write(records)


def _restrict(path: Path) -> None:
    """Owner-only permissions where the platform supports them (a no-op on most Windows setups)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------- the vault


def generate_key() -> str:
    """A new Fernet key (urlsafe base64 of 32 random bytes)."""
    return Fernet.generate_key().decode()


def _fernet(key: str | bytes) -> Fernet:
    try:
        return Fernet(key.strip() if isinstance(key, (str, bytes)) else key)
    except (ValueError, TypeError) as exc:
        raise VaultError("VAULT_KEY is not a valid Fernet key (32 url-safe base64 bytes). "
                         "Create one with: python -m worker.vault generate-key") from exc


class Vault:
    """Encrypting front-end over a ``VaultBackend``. All methods are async (the PostgreSQL backend will be)."""

    def __init__(self, backend: VaultBackend, key: str | bytes):
        self.backend = backend
        self._fernet = _fernet(key)

    # ---- encryption of the secret part
    def _seal(self, fields: dict[str, str]) -> str:
        return self._fernet.encrypt(json.dumps(fields, ensure_ascii=False).encode()).decode()

    def _open(self, record: dict) -> Credential:
        try:
            fields = json.loads(self._fernet.decrypt(record["secret"].encode()))
        except InvalidToken as exc:
            raise VaultError(f"cannot decrypt credential '{record.get('name')}': wrong VAULT_KEY?") from exc
        return Credential(owner=record["owner"], name=record["name"], kind=record["kind"], fields=fields,
                          description=record.get("description", ""), url=record.get("url", ""),
                          host=record.get("host", ""), created_at=record.get("created_at", ""),
                          updated_at=record.get("updated_at", ""))

    # ---- API
    async def put(
        self,
        owner: str,
        name: str,
        kind: str,
        fields: dict[str, str],
        *,
        description: str = "",
        url: str = "",
        host: str = "",
    ) -> dict:
        """Create or replace a credential and return its public metadata. ``host`` defaults to the host of ``url``."""
        fields = {str(k): str(v) for k, v in fields.items()}
        _validate(owner, name, kind, fields)
        existing = await self.backend.get(owner, name)
        now = utcnow()
        record = {
            "owner": owner, "name": name, "kind": kind, "description": description.strip(), "url": url.strip(),
            "host": (host or _host_of(url)).strip().lower(), "field_names": sorted(fields),
            "created_at": existing["created_at"] if existing else now, "updated_at": now,
            "secret": self._seal(fields),
        }
        await self.backend.put(record)
        return self._public(record)

    async def get(self, owner: str, name: str) -> Credential | None:
        """The decrypted credential. Internal use only: never hand the result to the model, a log or the store."""
        record = await self.backend.get(owner, name)
        return self._open(record) if record else None

    async def credentials(self, owner: str) -> list[Credential]:
        """Every decrypted credential of ``owner`` (internal use only)."""
        return [self._open(r) for r in await self.backend.list(owner)]

    async def list_public(self, owner: str) -> list[dict]:
        """Name, kind, description, url/host hint and field *names* of each credential; never a value."""
        return [self._public(r) for r in await self.backend.list(owner)]

    async def delete(self, owner: str, name: str) -> bool:
        return await self.backend.delete(owner, name)

    async def rotate_key(self, new_key: str | bytes | None = None) -> str:
        """Re-encrypt every credential under ``new_key`` (generated when omitted), switch this vault to it and
        return it. Persist the returned key (``VAULT_KEY``) before the old one is discarded."""
        new_key = new_key or generate_key()
        new = _fernet(new_key)
        records = []
        for record in await self.backend.all():
            fields = self._open(record).fields
            records.append({**record, "secret": new.encrypt(json.dumps(fields, ensure_ascii=False).encode()).decode()})
        await self.backend.replace_all(records)
        self._fernet = new
        return new_key.decode() if isinstance(new_key, bytes) else new_key

    @staticmethod
    def _public(record: dict) -> dict:
        return {"owner": record["owner"], "name": record["name"], "kind": record["kind"],
                "description": record.get("description", ""), "url": record.get("url", ""),
                "host": record.get("host", ""), "fields": list(record.get("field_names", [])),
                "created_at": record.get("created_at", ""), "updated_at": record.get("updated_at", "")}


def resolve_key(settings: Settings, key_store: FileVaultBackend, has_data: bool) -> str:
    """The Fernet key for the vault described by ``settings``.

    ``VAULT_KEY`` if set, else the file ``<VAULT_DIR>/.vault_key``. If neither exists and the vault holds nothing yet
    (``has_data`` false), a development key is generated into that file (with a warning; the key itself is never
    logged). In deploy mode, or when the vault already holds data without a key, this raises instead of inventing one."""
    key = settings.VAULT_KEY.strip()
    if not key and key_store.key_file.is_file():
        key = key_store.key_file.read_text(encoding="utf-8").strip()
    if not key:
        if settings.DEPLOY_MODE:
            raise VaultError("VAULT_KEY must be set in deploy mode.")
        if has_data:
            raise VaultError(f"the vault {key_store.path} exists but no key was found: set VAULT_KEY or restore "
                             f"{key_store.key_file}.")
        key_store.directory.mkdir(parents=True, exist_ok=True)
        key_store.key_file.write_text(generate_key(), encoding="utf-8")
        _restrict(key_store.key_file)
        log.warning("VAULT_KEY is not set: generated a development key in %s. Set VAULT_KEY for anything shared.",
                    key_store.key_file)
        key = key_store.key_file.read_text(encoding="utf-8").strip()
    return key


def open_vault(settings: Settings | None = None) -> Vault:
    """The file-backed vault described by the settings (key: see ``resolve_key``)."""
    settings = settings or get_settings()
    backend = FileVaultBackend(settings.VAULT_DIR)
    return Vault(backend, resolve_key(settings, backend, backend.exists()))


def vault_exists(settings: Settings | None = None) -> bool:
    """Has a file vault been created for these settings? (Cheap; never creates a key or a file.)"""
    return FileVaultBackend((settings or get_settings()).VAULT_DIR).exists()


# --------------------------------------------------------------------------- per-run scope


class VaultScope:
    """The credentials one run may use: a decrypted snapshot of one owner's vault entries, taken when the run starts.

    Lives in ``ctx.extras["vault_scope"]``. ``public()`` / ``names()`` / ``get()`` / ``credentials()`` cover the
    credentials the agent may use and are safe to show (``get()`` returns values and is for tools and the redactor only).
    Internal credentials (``HIDDEN_CREDENTIALS``) are invisible to all of those and reachable only through
    ``internal()``; their values are still part of ``secret_values()`` so they are redacted like everything else."""

    def __init__(self, owner: str = DEFAULT_OWNER, credentials: Iterable[Credential] = ()):
        self.owner = owner
        self._credentials = {c.name: c for c in credentials}

    @classmethod
    async def load(cls, vault: Vault, owner: str = DEFAULT_OWNER) -> VaultScope:
        return cls(owner, await vault.credentials(owner))

    def _visible(self) -> list[Credential]:
        return [c for c in self._credentials.values() if not is_hidden(c.name)]

    def get(self, name: str) -> Credential | None:
        return None if is_hidden(str(name)) else self._credentials.get(str(name))

    def internal(self, name: str) -> Credential | None:
        """An internal credential (or any other) by name, for server code only: never expose the result to the agent."""
        return self._credentials.get(str(name))

    def credentials(self) -> list[Credential]:
        return self._visible()

    def names(self) -> list[str]:
        return sorted(c.name for c in self._visible())

    def public(self) -> list[dict]:
        return [self._credentials[n].public() for n in self.names()]

    def secret_values(self) -> dict[str, str]:
        """``{label: value}`` of every secret in scope, internal ones included (for the redactor)."""
        return {label: value for c in self._credentials.values() for label, value in c.secret_values().items()}

    def __len__(self) -> int:
        return len(self._credentials)

    def __bool__(self) -> bool:
        return bool(self._credentials)


async def default_scope(
    settings: Settings | None = None, owner: str = DEFAULT_OWNER, vault: Vault | None = None
) -> VaultScope | None:
    """The scope for a new run, or None when the vault is absent or empty (then the run behaves exactly as in v1).

    Without an explicit ``vault`` the file vault is only opened if it already exists. Raises ``VaultError`` when it
    exists but cannot be decrypted."""
    settings = settings or get_settings()
    if vault is None:
        if not vault_exists(settings):
            return None
        vault = open_vault(settings)
    scope = await VaultScope.load(vault, owner)
    return scope if scope else None


# --------------------------------------------------------------------------- command line


def _parse_fields(pairs: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise VaultError(f"--field expects name=value (or name=- to be prompted), got '{key or pair}'")
        fields[key] = getpass.getpass(f"{key}: ") if value == "-" else value
    return fields


async def _run_command(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "generate-key":
        print(generate_key())
        return 0
    vault = open_vault(settings)
    if args.command == "put":
        info = await vault.put(args.owner, args.name, args.kind, _parse_fields(args.field),
                               description=args.description, url=args.url, host=args.host)
        print(f"Stored credential '{info['name']}' for owner '{info['owner']}' "
              f"({info['kind']}; fields: {', '.join(info['fields'])}).")
    elif args.command == "list":
        rows = await vault.list_public(args.owner)
        if not rows:
            print(f"No credentials for owner '{args.owner}'.")
        for row in rows:
            print(f"{row['name']:<20} {row['kind']:<8} {row['host'] or '-':<24} fields: {', '.join(row['fields'])}"
                  + (f"  # {row['description']}" if row["description"] else ""))
    elif args.command == "delete":
        removed = await vault.delete(args.owner, args.name)
        print(f"Deleted '{args.name}'." if removed else f"No credential '{args.name}' for owner '{args.owner}'.")
        return 0 if removed else 1
    elif args.command == "rotate-key":
        return await _rotate(vault, settings)
    return 0


async def _rotate(vault: Vault, settings: Settings) -> int:
    new_key = generate_key()
    key_file = vault.backend.key_file if isinstance(vault.backend, FileVaultBackend) else None
    if settings.VAULT_KEY or key_file is None:
        await vault.rotate_key(new_key)
        print(f"Credentials re-encrypted. Set VAULT_KEY to the new key now:\n{new_key}")
        return 0
    pending = key_file.with_name(KEY_FILE + ".next")  # survives a crash between re-encrypting and replacing the key
    pending.write_text(new_key, encoding="utf-8")
    _restrict(pending)
    await vault.rotate_key(new_key)
    os.replace(pending, key_file)
    print(f"Credentials re-encrypted; the new key is in {key_file}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m worker.vault", description="Manage the credential vault.")
    sub = parser.add_subparsers(dest="command", required=True)
    put = sub.add_parser("put", help="add or replace a credential")
    put.add_argument("--owner", default=DEFAULT_OWNER)
    put.add_argument("--name", required=True)
    put.add_argument("--kind", choices=KINDS, required=True)
    put.add_argument("--url", default="", help="where it is used; its host limits where the secret may be sent")
    put.add_argument("--host", default="", help="host hint (default: the host of --url)")
    put.add_argument("--description", default="")
    put.add_argument("--field", action="append", default=[], metavar="NAME=VALUE",
                     help="a field; use NAME=- to be prompted for the value without echo")
    listing = sub.add_parser("list", help="list credentials (names and metadata only)")
    listing.add_argument("--owner", default=DEFAULT_OWNER)
    delete = sub.add_parser("delete", help="delete a credential")
    delete.add_argument("--owner", default=DEFAULT_OWNER)
    delete.add_argument("--name", required=True)
    sub.add_parser("rotate-key", help="re-encrypt everything under a new key")
    sub.add_parser("generate-key", help="print a new VAULT_KEY")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: ``python -m worker.vault``."""
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run_command(args, get_settings()))
    except VaultError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
