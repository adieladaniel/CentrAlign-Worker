"""Secret-safe tools: the agent can use vault credentials without ever seeing their values.

* ``list_credentials`` shows what exists (names, kinds, descriptions, where each is used, field names).
* ``browser_fill_secret`` types one credential field straight into an input of the run's own page. The value goes from
  the vault to the DOM inside this process; the observation, the step and the events only say
  ``•••• (credential erp.password)``.
* ``http_request(auth_credential=...)`` (see ``http.py``) attaches the credential's header the same way.

The run's credentials are the ``VaultScope`` in ``ctx.extras["vault_scope"]`` (set by ``AgentRunner``). Tools that handle a
value also apply the run's ``Redactor`` (``ctx.extras["redactor"]``) to what they return, in case the page echoes it.
"""
from __future__ import annotations

from urllib.parse import urlparse

from worker.tools.base import Tool, ToolContext, ToolResult
from worker.tools.browser import ACTION_TIMEOUT_MS, BrowserTool, BrowserToolError, _ref_schema
from worker.tools.registry import ToolRegistry

# <input type=...> values a credential may be typed into. Textareas are refused on purpose: a free-text box (a comment,
# a note) is where a prompt-injected agent would copy a secret to.
FILLABLE_INPUT_TYPES = frozenset({"", "text", "password", "email", "search", "tel", "url"})
NO_VAULT_NOTE = "No credentials are stored in the vault for this run."


def mask(credential: str, field: str) -> str:
    """How a secret is referred to in observations, steps and events."""
    return f"•••• (credential {credential}.{field})"


class ListCredentialsTool(Tool):
    """Names and hints of the credentials the run may use."""

    name = "list_credentials"
    description = (
        "List the credentials in the secure vault that you may use: name, kind, description, where it is used and "
        "which fields it has. Values are never shown. Use browser_fill_secret to type one into a login form, or "
        "http_request with auth_credential to authenticate an API call."
    )
    parameters = {"type": "object", "properties": {}}

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        return "List the credentials available in the vault"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        scope = ctx.extras.get("vault_scope")
        if not scope or not scope.names():
            return ToolResult(ok=True, observation=f"{NO_VAULT_NOTE} If a login is needed, look for it in the shared "
                              "workspace or ask the user.", data={"credentials": []})
        lines = ["Credentials in the secure vault (values are never shown):"]
        for info in scope.public():
            where = info["url"] or info["host"]
            lines.append(
                f"- {info['name']} ({info['kind']})"
                + (f": {info['description']}" if info["description"] else "")
                + (f" | used at {where}" if where else "")
                + f" | fields: {', '.join(info['fields'])}"
            )
        return ToolResult(ok=True, observation="\n".join(lines), data={"credentials": scope.public()})


class FillSecretTool(BrowserTool):
    """Type a vault credential field into a text or password input without revealing it."""

    name = "browser_fill_secret"
    description = (
        "Type a stored credential field into a text or password input, by element ref. Example: credential 'erp', "
        "field 'password'. The value is filled in directly and is never shown to you. Use list_credentials to see "
        "what exists. Filling does not submit anything: click the submit button as a separate step."
    )
    parameters = {
        "type": "object",
        "properties": {
            "ref": _ref_schema(),
            "credential": {"type": "string", "description": "Credential name from list_credentials, e.g. 'erp'."},
            "field": {"type": "string", "description": "Field of that credential, e.g. 'username' or 'password'."},
        },
        "required": ["ref", "credential", "field"],
    }
    read_only_safe = False  # the verifier cannot type
    max_retries = 1

    # Filling is not committing, so the risk stays READ (the submit click is classified and gated as usual).

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        return (f"Fill {mask(str(args.get('credential')), str(args.get('field')))} into "
                f"{self._element_desc(args, ctx)} on {self._page_path(ctx)}")

    def refusal(self, args, session):
        return "Filling credentials is not allowed in read-only mode."

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        result = await super().run(args, ctx)  # session handling, read-only refusal and error mapping
        redactor = ctx.extras.get("redactor")  # error texts of the driver may quote the value
        return redactor.redact_result(result) if redactor else result

    async def execute(self, args, ctx, session):
        name, field = str(args["credential"]), str(args["field"])
        scope = ctx.extras.get("vault_scope")
        credential = scope.get(name) if scope else None
        if credential is None:
            available = ", ".join(scope.names()) if scope else ""
            raise BrowserToolError(
                f"No credential named '{name}' in the vault. " + (f"Available: {available}." if available else NO_VAULT_NOTE)
            )
        value = credential.fields.get(field)
        if value is None:
            raise BrowserToolError(
                f"Credential '{name}' has no field '{field}'. Its fields: {', '.join(sorted(credential.fields))}."
            )
        locator = await session.locate(args["ref"])
        info = session.info(args["ref"]) or {}
        if info.get("tag") != "input" or (info.get("type") or "text") not in FILLABLE_INPUT_TYPES:
            raise BrowserToolError(
                f"[{args['ref']}] is a {info.get('type') or info.get('tag') or 'non-text'} element. "
                "Credentials can only be typed into text or password inputs."
            )
        page_host = (urlparse(session.page.url).hostname or "").lower()
        if not credential.allows_host(page_host):
            raise BrowserToolError(
                f"Credential '{name}' is only for {credential.host}, but the current page is on {page_host or 'an unknown host'}. "
                "Not filled."
            )
        await locator.fill(value, timeout=ACTION_TIMEOUT_MS)
        observation = f"Filled {self._element_desc(args, ctx)} with {mask(name, field)}.\n{await session.observe()}"
        redactor = ctx.extras.get("redactor")
        return ToolResult(ok=True, observation=redactor.redact(observation) if redactor else observation,
                          data={"url": session.url})


def register_secret_tools(registry: ToolRegistry) -> list[Tool]:
    """Register ``list_credentials`` and ``browser_fill_secret``."""
    return [registry.register(tool) for tool in (ListCredentialsTool(), FillSecretTool())]
