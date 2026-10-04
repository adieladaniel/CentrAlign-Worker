"""Sandboxed file tools: list_files, read_file (text/CSV/JSON/PDF), write_file.

Visible roots: the workspace directory, plus the run's ``downloads/`` folder exposed under the
virtual prefix ``downloads/``. Writes are limited to the workspace.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from pypdf import PdfReader

from worker.tools.base import Risk, Tool, ToolContext, ToolResult

DOWNLOADS_PREFIX = "downloads"


class SandboxError(ValueError):
    """A path was outside the allowed roots."""


def _downloads_dir(ctx: ToolContext) -> Path:
    return ctx.run_dir / DOWNLOADS_PREFIX


def resolve_path(raw: str, ctx: ToolContext, *, for_write: bool = False) -> Path:
    """Map a user-supplied path to a real path inside the sandbox, or raise SandboxError.

    ``downloads`` / ``downloads/<file>`` map to the run's downloads folder (read-only);
    everything else is relative to the workspace. Absolute paths are accepted only inside a root.
    """
    raw = (raw or ".").replace("\\", "/")
    workspace = ctx.workspace.resolve()
    downloads = _downloads_dir(ctx).resolve()

    first, _, rest = raw.lstrip("/").partition("/")
    if not Path(raw).is_absolute() and first == DOWNLOADS_PREFIX:
        root, candidate = downloads, downloads / rest
    elif Path(raw).is_absolute():
        candidate = Path(raw).resolve()
        root = downloads if candidate.is_relative_to(downloads) else workspace
    else:
        root, candidate = workspace, workspace / raw

    candidate = candidate.resolve()
    if not candidate.is_relative_to(root):
        raise SandboxError(f"Path '{raw}' is outside the allowed folders (workspace and downloads/).")
    if for_write and root == downloads:
        raise SandboxError("downloads/ is read-only; write files into the workspace instead.")
    return candidate


def _display(path: Path, ctx: ToolContext) -> str:
    downloads = _downloads_dir(ctx).resolve()
    if path.is_relative_to(downloads):
        return f"{DOWNLOADS_PREFIX}/{path.relative_to(downloads).as_posix()}".rstrip("/.")
    return path.relative_to(ctx.workspace.resolve()).as_posix()


def _extract_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join(
        f"--- page {i} of {len(reader.pages)} ---\n{(page.extract_text() or '').strip()}"
        for i, page in enumerate(reader.pages, start=1)
    )


class ListFilesTool(Tool):
    """List files in the workspace or in downloads/."""

    name = "list_files"
    description = (
        "List files and folders. Path is relative to the workspace; use 'downloads' for files "
        "the browser downloaded during this run."
    )
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Folder to list. Defaults to '.' (workspace root)."}},
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        target = resolve_path(args.get("path", "."), ctx)
        if not target.is_dir():
            return ToolResult(ok=False, observation=f"Not a folder: {args.get('path', '.')}", error="not_a_directory")
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        lines = [f"{_display(p, ctx)}{'/' if p.is_dir() else ''}" + (f"  ({p.stat().st_size} bytes)" if p.is_file() else "") for p in entries]
        at_root = target == ctx.workspace.resolve()
        if at_root and _downloads_dir(ctx).is_dir():
            lines.append(f"{DOWNLOADS_PREFIX}/  (files downloaded in this run)")
        observation = "\n".join(lines) if lines else "(empty folder)"
        return ToolResult(ok=True, observation=observation, data={"count": len(lines)})


class ReadFileTool(Tool):
    """Read a text, JSON, CSV or PDF file."""

    name = "read_file"
    description = (
        "Read a file from the workspace or downloads/. Supports text, JSON, CSV and PDF "
        "(PDF text is extracted with page markers). Long files are truncated to max_chars."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path, e.g. 'notes.txt' or 'downloads/invoice.pdf'."},
            "max_chars": {"type": "integer", "minimum": 1, "description": "Maximum characters to return (default 8000)."},
        },
        "required": ["path"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = resolve_path(args["path"], ctx)
        if not path.is_file():
            return ToolResult(ok=False, observation=f"File not found: {args['path']}", error="not_found")
        max_chars = int(args.get("max_chars") or 8000)

        if path.suffix.lower() == ".pdf":
            text = await asyncio.to_thread(_extract_pdf, path)
        else:
            raw = await asyncio.to_thread(path.read_bytes)
            if b"\x00" in raw[:4096]:
                return ToolResult(ok=False, observation=f"{args['path']} looks like a binary file.", error="binary_file")
            text = raw.decode("utf-8", errors="replace")

        truncated = len(text) > max_chars
        shown = text[:max_chars] + (f"\n[truncated: showing {max_chars} of {len(text)} chars]" if truncated else "")
        return ToolResult(
            ok=True,
            observation=shown or "(file is empty)",
            data={"path": _display(path, ctx), "chars": len(text), "truncated": truncated},
        )


class WriteFileTool(Tool):
    """Create or overwrite a text file in the workspace."""

    name = "write_file"
    description = "Write text content to a file in the workspace (creates folders; overwrites existing files)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Destination path relative to the workspace."},
            "content": {"type": "string", "description": "Full text content to write."},
        },
        "required": ["path", "content"],
    }
    risk = Risk.WRITE
    read_only_safe = False

    def describe_action(self, args: dict) -> str:
        return f"Write {len(args.get('content', ''))} characters to '{args.get('path')}'"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = resolve_path(args["path"], ctx, for_write=True)
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, args["content"].encode("utf-8"))  # bytes: no newline translation
        verb = "Overwrote" if existed else "Created"
        return ToolResult(
            ok=True,
            observation=f"{verb} {_display(path, ctx)} ({len(args['content'])} chars)",
            data={"path": _display(path, ctx), "overwritten": existed},
        )
