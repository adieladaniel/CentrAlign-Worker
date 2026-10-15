"""PostgreSQL ``VaultBackend``: the ``credentials`` table.

The ``Vault`` front-end encrypts before calling the backend, so this module only ever handles the Fernet token
(``record["secret"]``, stored as bytea) and non-secret metadata. Use it with
``Vault(PgVaultBackend(sessionmaker), key)``; owners are org ids as text (or ``default``).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.config import Settings, get_settings
from worker.db.models import Credential
from worker.db.util import iso_ts, parse_ts
from worker.vault import FileVaultBackend, Vault, resolve_key


def _row(record: dict) -> dict[str, Any]:
    """A vault record as ``credentials`` columns."""
    values = {"owner": record["owner"], "name": record["name"], "kind": record["kind"],
              "description": record.get("description", ""), "url_hint": record.get("url", ""),
              "host": record.get("host", ""), "field_names": list(record.get("field_names", [])),
              "ciphertext": record["secret"].encode()}
    for column, key in (("created_at", "created_at"), ("updated_at", "updated_at")):
        if parse_ts(record.get(key)) is not None:
            values[column] = parse_ts(record[key])
    return values


def _record(row: Credential) -> dict:
    return {"owner": row.owner, "name": row.name, "kind": row.kind, "description": row.description,
            "url": row.url_hint, "host": row.host, "field_names": list(row.field_names),
            "created_at": iso_ts(row.created_at), "updated_at": iso_ts(row.updated_at),
            "secret": bytes(row.ciphertext).decode()}


class PgVaultBackend:
    """``VaultBackend`` over PostgreSQL (safe for any number of processes: every operation is one transaction)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self.sessionmaker = sessionmaker

    async def put(self, record: dict) -> None:
        values = _row(record)
        update = {k: v for k, v in values.items() if k not in ("owner", "name", "created_at")}
        async with self.sessionmaker() as session, session.begin():
            await session.execute(pg_insert(Credential).values(**values).on_conflict_do_update(
                index_elements=[Credential.owner, Credential.name], set_=update))

    async def get(self, owner: str, name: str) -> dict | None:
        async with self.sessionmaker() as session:
            row = await session.scalar(select(Credential).where(Credential.owner == owner, Credential.name == name))
        return _record(row) if row else None

    async def list(self, owner: str) -> list[dict]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(select(Credential).where(Credential.owner == owner).order_by(Credential.name))
            return [_record(r) for r in rows]

    async def delete(self, owner: str, name: str) -> bool:
        async with self.sessionmaker() as session, session.begin():
            result = await session.execute(delete(Credential).where(Credential.owner == owner, Credential.name == name))
        return result.rowcount > 0

    async def all(self) -> list[dict]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(select(Credential).order_by(Credential.owner, Credential.name))
            return [_record(r) for r in rows]

    async def replace_all(self, records: list[dict]) -> None:
        async with self.sessionmaker() as session, session.begin():
            await session.execute(delete(Credential))
            if records:
                await session.execute(insert(Credential), [_row(r) for r in records])


def open_pg_vault(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings | None = None) -> Vault:
    """The PostgreSQL vault of the deployment: ``VAULT_KEY``, else a development key file in ``VAULT_DIR`` (generated on
    first use outside deploy mode, like the file vault). Rows encrypted under another key fail to decrypt."""
    settings = settings or get_settings()
    return Vault(PgVaultBackend(sessionmaker), resolve_key(settings, FileVaultBackend(settings.VAULT_DIR), has_data=False))
