"""Small conversions between the worker's plain-Python values (ISO strings, hex run ids) and database types."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


def parse_ts(value: str | datetime | None) -> datetime | None:
    """An ISO-8601 string (as ``utcnow()`` makes) as an aware UTC datetime; naive values are taken to be UTC.
    Empty or unparseable input gives None."""
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def iso_ts(value: datetime | None) -> str | None:
    """The inverse of ``parse_ts``: UTC ISO-8601, identical to ``utcnow()`` for whole seconds."""
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def clean_text(value: Any) -> Any:
    """Strip NUL characters from a string: PostgreSQL text columns cannot hold them. Other values pass through."""
    return value.replace("\x00", "") if isinstance(value, str) else value


def to_uuid(value: str | uuid.UUID) -> uuid.UUID:
    """The UUID for a run or org id given as hex or dashed text. Raises ``ValueError`` when it is not a UUID."""
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def maybe_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    """``to_uuid``, but None instead of ``ValueError`` (for reads, where an unknown id simply finds nothing)."""
    try:
        return to_uuid(value)  # type: ignore[arg-type]
    except (ValueError, AttributeError, TypeError):
        return None
