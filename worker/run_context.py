"""What starting a run needs, shared by every path that does it: the API process (file mode and the checks done when a
run is enqueued) and the worker process that executes a claimed run.

* ``settings_for_run``: the deployment settings with the run's own overrides (approval mode, limits, headless).
* ``load_org_run_context``: the organisation, its effective settings and its simulated company (vault scope).
* ``pin_environment``: pre-seed the run's browser with its organisation's simulator cookie.
* ``build_llm``: create the run's LLM, passing the accounting context to factories that accept it.
"""
from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.config import Settings
from worker.db.models import Org
from worker.llm.base import LLMClient
from worker.org_settings import effective, stricter_mode
from worker.provisioning import OrgEnvironment, load_org_environment
from worker.vault import Vault


def settings_for_run(base: Settings, *, org_cfg: dict[str, Any] | None, requested_mode: str | None, headless: bool) -> Settings:
    """``base`` with the run's overrides. In database mode (``org_cfg`` given) the approval mode is the org's, or the
    stricter one the operator asked for, and the limits are the org's; the agent is told the plain simulator URL (the
    tenant cookie is pre-seeded, see ``pin_environment``)."""
    overrides: dict[str, Any] = {"HEADLESS": headless}
    if org_cfg is None:
        if requested_mode:
            overrides["APPROVAL_MODE"] = requested_mode
    else:
        overrides.update(APPROVAL_MODE=stricter_mode(org_cfg["approval_mode"], requested_mode),
                         MAX_STEPS=org_cfg["max_steps"], RUN_TIMEOUT_S=org_cfg["run_timeout_s"], SIM_ENTRY_URL="")
    return base.model_copy(update=overrides)


def headless_for(base: Settings, headed_requested: bool) -> bool:
    """A server deployment is always headless; otherwise the user's choice."""
    return not (headed_requested and not base.DEPLOY_MODE)


@dataclass
class OrgRunContext:
    org: Org
    cfg: dict[str, Any]  # the org's effective settings
    environment: OrgEnvironment | None  # None: the org has no simulated company yet


async def load_org_run_context(sessionmaker: async_sessionmaker[AsyncSession], vault: Vault, base: Settings,
                               org_id: uuid.UUID) -> OrgRunContext:
    """Load the org, its effective settings and its environment. Raises ``LookupError`` for an unknown org and
    ``VaultError`` when the credential vault cannot be opened."""
    async with sessionmaker() as session:
        org = await session.get(Org, org_id)
    if org is None:
        raise LookupError("Unknown organization")
    return OrgRunContext(org, effective(org.settings, base), await load_org_environment(org, vault))


def pin_environment(runner: Any, environment: OrgEnvironment, settings: Settings) -> None:
    """Pin the run's browser to its org's own simulated company (the cookie is set before the first page)."""
    runner.ctx.extras["browser_init_cookies"] = environment.browser_cookies(settings.SIM_BASE_URL)


def build_llm(factory: Callable[..., LLMClient], settings: Settings, **context: Any) -> LLMClient:
    """``factory(settings, **context)`` with only the keyword arguments the factory accepts. ``context`` is the run's
    accounting and configuration context (``org_id``, ``user_id``, ``run_id``, ``sessionmaker``, ``vault_scope``,
    ``org_settings``): ``make_llm`` takes all of it, a plain ``factory(settings)`` (tests) none."""
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return factory(settings)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return factory(settings, **context)
    return factory(settings, **{k: v for k, v in context.items() if k in params})
