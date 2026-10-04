"""Assembles the default tool registry for a run."""
from __future__ import annotations

import logging

from worker.config import Settings
from worker.tools.control import FinishTool, UpdatePlanTool
from worker.tools.files import ListFilesTool, ReadFileTool, WriteFileTool
from worker.tools.http import HttpRequestTool
from worker.tools.interaction import AskUserTool
from worker.tools.memory_tools import RememberTool
from worker.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


def build_registry(settings: Settings) -> ToolRegistry:
    """Files, HTTP, memory, control and interaction tools, plus browser tools when Playwright support is present."""
    registry = ToolRegistry()
    for tool in (
        ListFilesTool(), ReadFileTool(), WriteFileTool(), HttpRequestTool(),
        RememberTool(), UpdatePlanTool(), FinishTool(), AskUserTool(),
    ):
        registry.register(tool)
    try:
        from worker.tools.browser import register_browser_tools
    except ImportError as err:
        log.warning("Browser tools unavailable: %s", err)
    else:
        register_browser_tools(registry)
    return registry
