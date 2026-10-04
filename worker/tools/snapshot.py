"""Formatting of the raw DOM snapshot (produced by snapshot.js) into a compact, token-cheap string.

Pure functions only (no browser needed), so they are easy to unit test.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

SNAPSHOT_JS = (Path(__file__).with_name("snapshot.js")).read_text(encoding="utf-8")

DEFAULT_MAX_TEXT = 3500
DEFAULT_MAX_ELEMENTS = 80
TRUNCATION_NOTE = "(truncated, use scroll or read_page with `find` to see more)"


@dataclass
class View:
    """A rendered snapshot plus paging info (used by the scroll tool)."""

    text: str
    page: int
    pages: int


def _q(value: str) -> str:
    return '"' + " ".join(str(value).replace('"', "'").split()) + '"'


def element_line(e: dict) -> str:
    """One line per interactive element, e.g. ``[3] combobox "Vendor" selected="X" options=[A, B, ...(+3)]``."""
    parts = [f"[{e['ref']}] {e['role']}"]
    if e.get("name"):
        parts.append(_q(e["name"]))
    else:
        parts.append('""')
    if e.get("type") and e["type"] not in ("text", "checkbox", "radio", "button", "submit", "reset", "image"):
        parts.append(f"type={e['type']}")
    flags = e.get("flags") or []
    for flag in flags:
        if flag not in ("invalid",):
            parts.append(flag)
    if "selected" in e and e.get("options") is not None:
        parts.append(f"selected={_q(e['selected'])}")
        options = list(e["options"])
        extra = e.get("optionCount", len(options)) - len(options)
        if extra > 0:
            options.append(f"...(+{extra})")
        parts.append("options=[" + ", ".join(options) + "]")
    if e.get("value"):
        parts.append(f"value={_q(e['value'])}")
    if e.get("placeholder") and e.get("placeholder") != e.get("name"):
        parts.append(f"placeholder={_q(e['placeholder'])}")
    if "invalid" in flags:
        parts.append(f"INVALID{': ' + e['error'] if e.get('error') else ''}")
    if e.get("href"):
        parts.append(f"-> {e['href']}")
    return " ".join(parts)


def chunk_lines(lines: list[str], budget: int) -> list[list[str]]:
    """Split ``lines`` into chunks of roughly ``budget`` characters (never splitting a line)."""
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if len(line) > budget:
            line = line[: budget - 1] + "…"
        if current and size + len(line) + 1 > budget:
            chunks.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append(current)
    return chunks


def _header(data: dict, status: int | None, notes: list[str]) -> list[str]:
    lines = [f"URL: {data['url']}", f"Title: {data['title']}"]
    if status is not None and status >= 400:
        lines.append(f"HTTP status of last navigation: {status}")
    lines.extend(notes)
    alerts = data.get("alerts") or []
    if alerts:
        lines.append("Alerts: " + " ; ".join(f"{a['kind']} {_q(a['text'])}" for a in alerts))
    return lines


def format_snapshot(
    data: dict,
    *,
    page: int = 0,
    max_text: int = DEFAULT_MAX_TEXT,
    max_elements: int = DEFAULT_MAX_ELEMENTS,
    status: int | None = None,
    notes: list[str] | None = None,
) -> View:
    """Render the snapshot ``data`` as text. ``page`` selects a window when the page exceeds the budgets."""
    text_lines = data["text"].split("\n") if data.get("text") else []
    text_chunks = chunk_lines(text_lines, max_text)
    elements = data.get("elements", [])
    element_pages = math.ceil(len(elements) / max_elements) if elements else 0
    pages = max(1, len(text_chunks), element_pages)
    page = min(max(page, 0), pages - 1)

    out = _header(data, status, notes or [])

    label = f" (part {page + 1}/{pages})" if pages > 1 else ""
    out.append(f"--- Page text{label} ---")
    chunk = text_chunks[page] if page < len(text_chunks) else []
    out.extend(chunk if chunk else ["(no text)" if page == 0 else "(no more text)"])
    if data.get("textLen", 0) > len(data.get("text", "")):
        out.append("(page text cut off at the scan limit)")
    if pages > 1 and page < len(text_chunks) - 1:
        out.append(TRUNCATION_NOTE)

    start = page * max_elements
    shown = elements[start : start + max_elements]
    if elements and pages > 1:
        out.append(f"--- Interactive elements {start + 1}-{start + len(shown)} of {len(elements)} ---")
    else:
        out.append("--- Interactive elements ---")
    out.extend([element_line(e) for e in shown] or ["(none)"])
    if start + len(shown) < len(elements):
        out.append(TRUNCATION_NOTE)
    elif data.get("totalElements", 0) >= 600:
        out.append("(element list capped at 600)")
    return View(text="\n".join(out), page=page, pages=pages)


def find_in_page(
    data: dict,
    needle: str,
    *,
    context: int = 1,
    max_chars: int = 4000,
    status: int | None = None,
    notes: list[str] | None = None,
) -> str:
    """Return only the lines of page text (with ``context`` neighbours) and elements that mention ``needle``."""
    low = needle.lower()
    out = _header(data, status, notes or [])
    lines = data["text"].split("\n") if data.get("text") else []
    hits = [i for i, line in enumerate(lines) if low in line.lower()]

    matched: list[str] = []
    if hits:
        keep: list[int] = []
        for i in hits:
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                if not keep or j > keep[-1]:
                    keep.append(j)
        hit_set = set(hits)
        prev = None
        for j in keep:
            if prev is not None and j != prev + 1:
                matched.append("  ...")
            matched.append(("> " if j in hit_set else "  ") + lines[j])
            prev = j

    element_hits = [element_line(e) for e in data.get("elements", []) if low in element_line(e).lower()]

    if not matched and not element_hits:
        out.append(
            f"No match for {_q(needle)} in the page text ({len(lines)} lines) or {len(data.get('elements', []))} "
            "interactive elements. It may be on another page: try pagination/search controls or scroll for lazy content."
        )
        return "\n".join(out)

    if matched:
        out.append(f"--- Page text matching {_q(needle)} ({len(hits)} line(s); '>' marks matches) ---")
        budget = max_chars
        for line in matched:
            if budget - len(line) < 0:
                out.append("(more matches omitted, use a more specific `find`)")
                break
            out.append(line)
            budget -= len(line) + 1
    if element_hits:
        out.append(f"--- Interactive elements matching {_q(needle)} ({len(element_hits)}) ---")
        out.extend(element_hits[:25])
        if len(element_hits) > 25:
            out.append(f"(+{len(element_hits) - 25} more)")
    return "\n".join(out)
