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
* ``load_org_environment`` gives a starting run what it needs: the vault scope of the org and the cookie to pre-seed in the
  run's browser context (``ctx.extras["browser_init_cookies"]``), so the agent starts inside its own tenant and cannot
  end up in another one even if it skips the entry link.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.config import Settings
from worker.db.models import Org
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
