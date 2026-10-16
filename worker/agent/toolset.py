"""Assembles the default tool registry for a run."""
from __future__ import annotations

import logging

from worker.config import Settings
from worker.tools.control import FinishTool, UpdatePlanTool
from worker.tools.files import ListFilesTool, ReadFileTool, WriteFileTool
from worker.tools.http import HttpRequestTool
from worker.tools.interaction import AskUserTool
from worker.tools.memory_tools import RecallTool, RememberTool
from worker.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


def build_registry(settings: Settings) -> ToolRegistry:
    """Files, HTTP, memory, control and interaction tools, plus browser and secret-safe tools when Playwright is present."""
    registry = ToolRegistry()
    for tool in (
        ListFilesTool(), ReadFileTool(), WriteFileTool(), HttpRequestTool(),
        RememberTool(), RecallTool(), UpdatePlanTool(), FinishTool(), AskUserTool(),
    ):
        registry.register(tool)
    try:
        from worker.tools.browser import register_browser_tools
        from worker.tools.secrets import register_secret_tools
    except ImportError as err:
        log.warning("Browser tools unavailable: %s", err)
    else:
        register_browser_tools(registry)
        register_secret_tools(registry)  # list_credentials, browser_fill_secret (inert without vault credentials)
    return registry
