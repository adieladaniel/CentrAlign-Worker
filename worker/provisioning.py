"""Each organisation's own simulated company: create it, reset it, and prepare a run to use it.

The simulator models a customer's *external* systems (mail, vendor portal, ERP), one isolated tenant per org
(``POST /admin/tenants``). This module is the platform side of that:

* ``provision_org_environment(org)`` creates (or finds) the org's tenant, records ``orgs.sim_tenant_id`` and writes the
  tenant's logins into the vault under ``owner = str(org.id)`` as ``mail``, ``portal``, ``erp`` (kind login) and
  ``erp_api`` (kind api_key, header ``X-API-Key``). Re-running it is safe and does not reset the tenant's data.
* The tenant's **entry token** (it selects the tenant in a browser, via the ``sim_tenant`` cookie) is stored in the same
  vault as the *internal* credential ``sim_entry`` (kind header). It is encrypted at rest like the logins, never listed
  to the agent or in the credentials UI (``vault.HIDDEN_CREDENTIALS``), and redacted from anything stored or sent to the
  model. The alternative, a plain field in ``orgs.settings``, would keep it unencrypted and visible to admins.
* ``resync_org_environments`` provisions every org again; the container's start-up runs it
  (``python -m worker.provisioning resync``), because the simulator's data lives on the container's ephemeral disk while
  PostgreSQL and the vault outlive it: a fresh container has no tenants, but the database still names the old ones.
* ``load_org_environment`` gives a starting run what it needs: the vault scope of the org and the cookie to pre-seed in the
  run's browser context (``ctx.extras["browser_init_cookies"]``), so the agent starts inside its own tenant and cannot
  end up in another one even if it skips the entry link.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.config import Settings, get_settings
from worker.db.models import Org
from worker.logging_setup import setup_logging
from worker.vault import Vault, VaultScope

log = logging.getLogger("worker.provisioning")

SIM_TENANT_COOKIE = "sim_tenant"  # name of the cookie company_sim reads (its value is the tenant's entry token)
SIM_ENTRY = "sim_entry"
SIM_TIMEOUT_S = 10.0
_TENANT_ID_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_SLUG_CHARS = re.compile(r"[^a-z0-9-]+")


class ProvisioningError(RuntimeError):
    """The simulated company could not be reached or refused the request. The message never contains secrets."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def tenant_id_for(org: Org) -> str:
    """A stable, readable sim tenant id for the org (``org-<slug>``; ``org-<id>`` when the slug does not fit)."""
    slug = _SLUG_CHARS.sub("-", org.slug.lower()).strip("-")
    candidate = f"org-{slug}"
    return candidate if slug and _TENANT_ID_OK.match(candidate) else f"org-{org.id.hex[:16]}"


async def sim_request(settings: Settings, method: str, path: str, body: dict | None = None,
                      client: httpx.AsyncClient | None = None) -> httpx.Response:
    """A request to the simulator's ``/admin`` API (with the admin token, if configured). Raises ``ProvisioningError``."""
    url = settings.SIM_BASE_URL.rstrip("/") + path
    try:
        if client is not None:
            resp = await client.request(method, url, json=body, headers=settings.sim_admin_headers)
        else:
            async with httpx.AsyncClient(timeout=SIM_TIMEOUT_S) as own:
                resp = await own.request(method, url, json=body, headers=settings.sim_admin_headers)
    except httpx.HTTPError as err:
        raise ProvisioningError(f"The simulated company is not reachable at {settings.SIM_BASE_URL} "
                                f"({err.__class__.__name__}). Start it with: python -m company_sim") from err
    if resp.status_code >= 400:
        raise ProvisioningError(f"The simulated company answered {resp.status_code}: {resp.text[:200]}", resp.status_code)
    return resp


async def provision_org_environment(
    org: Org,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    vault: Vault,
    settings: Settings,
    chaos: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Create the org's simulated company, store its logins in the vault and record the tenant on the org.

    ``chaos`` (flag names) is applied to the tenant when given; a tenant that already exists is otherwise left as it is.
    Returns ``{tenant_id, created, credentials: [names]}``: no secret values."""
    body: dict[str, Any] = {"tenant_id": tenant_id_for(org)}
    if chaos is not None:
        body["chaos"] = list(chaos)
    resp = await sim_request(settings, "POST", "/admin/tenants", body, client)
    info = resp.json()
    base, owner, creds = settings.SIM_BASE_URL.rstrip("/"), str(org.id), info["credentials"]
    await vault.put(owner, "mail", "login", creds["mail"], description="Company mailbox (webmail login)", url=f"{base}/mail")
    await vault.put(owner, "portal", "login", creds["portal"], description="Vendor billing portal login", url=f"{base}/portal")
    await vault.put(owner, "erp", "login", creds["erp"], description="ERP (accounts payable) login", url=f"{base}/erp")
    await vault.put(owner, "erp_api", "api_key", {"api_key": creds["erp_api_key"], "header_name": "X-API-Key"},
                    description="ERP REST API key, sent as the X-API-Key header", url=f"{base}/erp/api")
    await vault.put(owner, SIM_ENTRY, "header", {"header_name": "X-Sim-Tenant", "value": info["token"]},
                    description="Internal: selects this org's simulated company in the browser", url=base)
    async with sessionmaker() as session, session.begin():
        await session.execute(update(Org).where(Org.id == org.id).values(sim_tenant_id=info["tenant_id"]))
    return {"tenant_id": info["tenant_id"], "created": resp.status_code == 201,
            "credentials": ["mail", "portal", "erp", "erp_api"]}


async def resync_org_environments(
    sessionmaker: async_sessionmaker[AsyncSession], vault: Vault, settings: Settings, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """Provision the simulated company of **every** org (also orgs that never had one), one after the other.

    Safe to run at every start: a tenant the simulator already has is kept as it is, one it lost is created again with
    fresh logins, and the vault and ``orgs.sim_tenant_id`` are rewritten to match. A failing org is logged and skipped,
    the others still run. Returns ``{orgs, created, existing, failed, unreachable}`` (counts only: no secrets, no names
    of credentials); ``unreachable`` is True when any failure was a connection problem rather than a refusal."""
    async with sessionmaker() as session:
        orgs = (await session.scalars(select(Org).order_by(Org.slug))).all()
    summary = {"orgs": len(orgs), "created": 0, "existing": 0, "failed": 0, "unreachable": False}
    for org in orgs:
        try:
            result = await provision_org_environment(org, sessionmaker=sessionmaker, vault=vault, settings=settings, client=client)
        except ProvisioningError as err:
            summary["failed"] += 1
            summary["unreachable"] = summary["unreachable"] or err.status is None
            log.warning("could not provision the environment of org %s: %s", org.slug, err)
            continue
        except Exception:  # noqa: BLE001 - one org's vault or database problem must not stop the others
            summary["failed"] += 1
            log.exception("could not provision the environment of org %s", org.slug)
            continue
        summary["created" if result["created"] else "existing"] += 1
    log.info("environments synchronized: %d org(s), %d created, %d already there, %d failed",
             summary["orgs"], summary["created"], summary["existing"], summary["failed"])
    return summary


async def reset_org_environment(tenant_id: str, settings: Settings, chaos: list[str] | None = None,
                                client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Wipe and reseed one tenant (optionally replacing its chaos flags). Touches no other org's data."""
    body = {"chaos": list(chaos)} if chaos is not None else {}
    return (await sim_request(settings, "POST", f"/admin/tenants/{tenant_id}/reset", body, client)).json()


async def org_chaos(tenant_id: str, settings: Settings, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """The chaos flags (``flags``, ``descriptions``, ``active``) of one tenant."""
    return (await sim_request(settings, "GET", f"/admin/tenants/{tenant_id}/chaos", None, client)).json()


@dataclass
class OrgEnvironment:
    """What a starting run needs from its org: the tenant id and the org's vault scope (agent-visible logins plus the
    internal entry token)."""

    tenant_id: str
    scope: VaultScope

    def browser_cookies(self, sim_base_url: str) -> list[dict[str, Any]]:
        """The ``sim_tenant`` cookie for the run's browser context, scoped to the simulator's origin."""
        entry = self.scope.internal(SIM_ENTRY)
        if entry is None or not entry.fields.get("value"):
            return []
        parts = urlsplit(sim_base_url)
        return [{"name": SIM_TENANT_COOKIE, "value": entry.fields["value"], "url": f"{parts.scheme}://{parts.netloc}/",
                 "httpOnly": True, "sameSite": "Lax"}]


async def load_org_environment(org: Org, vault: Vault) -> OrgEnvironment | None:
    """The org's environment, or None when it has none yet (no tenant recorded, or no entry token in the vault)."""
    if not org.sim_tenant_id:
        return None
    scope = await VaultScope.load(vault, str(org.id))
    return OrgEnvironment(org.sim_tenant_id, scope) if scope.internal(SIM_ENTRY) else None


def vault_owner(org_id: uuid.UUID) -> str:
    """The vault owner string of an org."""
    return str(org_id)


# --------------------------------------------------------------------------- command line


async def _resync_command() -> int:
    from worker.db.session import dispose_engine, get_sessionmaker
    from worker.vault_pg import open_pg_vault

    settings = get_settings()
    if not settings.db_auth:
        print("worker.provisioning needs database mode: set DATABASE_URL (see .env.example).", file=sys.stderr)
        return 2
    sessionmaker = get_sessionmaker(settings)
    try:
        summary = await resync_org_environments(sessionmaker, open_pg_vault(sessionmaker, settings), settings)
    finally:
        await dispose_engine()
    print("resync: " + ", ".join(f"{name}={value}" for name, value in summary.items()))
    if summary["unreachable"]:
        print(f"error: the simulated company is not reachable at {settings.SIM_BASE_URL}", file=sys.stderr)
        return 1
    return 1 if summary["failed"] and summary["failed"] == summary["orgs"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker.provisioning", description="Simulated-company environments of the orgs.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("resync", help="create or refresh the environment of every org (exit 1 when the simulator is unreachable)")
    parser.parse_args(argv)
    setup_logging(get_settings())
    return asyncio.run(_resync_command())


if __name__ == "__main__":
    sys.exit(main())
