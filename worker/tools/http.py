"""http_request tool restricted to an allowlist of hosts."""
from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx

from worker.tools.base import Risk, Tool, ToolContext, ToolResult

MAX_BODY_CHARS = 4000
TIMEOUT_S = 20.0
# Only these are re-sent automatically after a 5xx / network error; anything else might duplicate a write.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class HttpRequestTool(Tool):
    """Call an HTTP API on an allowlisted host (e.g. the internal ERP's REST API)."""

    name = "http_request"
    description = (
        "Make an HTTP request to an allowed internal host. Returns the status code and the (truncated) "
        "response body. Use headers/json_body/query as needed; omit them otherwise."
    )
    parameters = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]},
            "url": {"type": "string", "description": "Absolute http(s) URL on an allowed host."},
            "headers": {"type": "object", "description": "Optional request headers."},
            "json_body": {"type": "object", "description": "Optional JSON request body."},
            "query": {"type": "object", "description": "Optional query-string parameters."},
        },
        "required": ["method", "url"],
    }
    risk = Risk.WRITE

    def classify(self, args: dict) -> Risk:
        method = str(args.get("method", "GET")).upper()
        if method in ("GET", "HEAD"):
            return Risk.READ
        if method == "DELETE":
            return Risk.IRREVERSIBLE
        return Risk.WRITE

    def retry_safe(self, args: dict) -> bool:
        return str(args.get("method", "GET")).upper() in IDEMPOTENT_METHODS

    def no_retry_note(self, args: dict) -> str:
        return (
            "Server error on a non-idempotent request; it was NOT retried automatically. The write may or may not "
            "have happened: check whether the record exists (GET) before retrying."
        )

    def describe_action(self, args: dict) -> str:
        body = f" with body {json.dumps(args['json_body'], ensure_ascii=False)}" if args.get("json_body") else ""
        return f"{str(args.get('method', 'GET')).upper()} {args.get('url')}{body}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        method = str(args["method"]).upper()
        url = args["url"]
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or host not in ctx.settings.http_allowed_hosts:
            allowed = ", ".join(sorted(ctx.settings.http_allowed_hosts))
            return ToolResult(
                ok=False,
                observation=f"Host '{host or url}' is not allowed. Allowed hosts: {allowed}.",
                error="host_not_allowed",
            )
        if ctx.extras.get("read_only") and method != "GET":
            return ToolResult(
                ok=False,
                observation=f"{method} is not permitted in read-only mode; only GET is allowed.",
                error="read_only_mode",
            )

        async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False) as client:
            resp = await client.request(
                method, url, headers=args.get("headers") or None, json=args.get("json_body"), params=args.get("query") or None
            )

        body = self._format_body(resp)
        observation = f"HTTP {resp.status_code} {method} {url}\n{body}"
        data = {"status": resp.status_code, "url": str(resp.url)}
        if resp.status_code >= 500:
            if method not in IDEMPOTENT_METHODS:
                return ToolResult(
                    ok=False,
                    observation=f"{observation}\n{self.no_retry_note(args)}",
                    error=f"HTTP {resp.status_code}",
                    data=data,
                )
            return ToolResult(ok=False, observation=observation, error=f"HTTP {resp.status_code}", data=data, transient=True)
        if resp.status_code >= 400:
            return ToolResult(ok=False, observation=observation, error=f"HTTP {resp.status_code}", data=data)
        return ToolResult(ok=True, observation=observation, data=data)

    @staticmethod
    def _format_body(resp: httpx.Response) -> str:
        text = resp.text
        if "json" in resp.headers.get("content-type", ""):
            try:
                text = json.dumps(resp.json(), indent=2, ensure_ascii=False)
            except ValueError:
                pass
        if len(text) > MAX_BODY_CHARS:
            return text[:MAX_BODY_CHARS] + f"\n[truncated: showing {MAX_BODY_CHARS} of {len(text)} chars]"
        return text or "(empty body)"
