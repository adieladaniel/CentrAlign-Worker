"""``python -m worker.db migrate|check``: apply migrations / show the connection, server version and revision."""
from __future__ import annotations

import argparse
import asyncio
import sys

from worker.config import get_settings
from worker.db import migrate
from worker.db.session import make_engine, mask_url


async def _run(command: str, revision: str) -> int:
    url = get_settings().DATABASE_URL.strip()
    if not url:
        print("error: DATABASE_URL is not set (see .env.example)", file=sys.stderr)
        return 2
    engine = make_engine(url)
    try:
        print(f"database: {mask_url(url)}")
        if command == "migrate":
            before = await migrate.current_revision(engine)
            await migrate.upgrade(engine, revision)
            print(f"migrated: {before or '(empty)'} -> {await migrate.current_revision(engine)}")
        else:
            print(f"server:   PostgreSQL {await migrate.server_version(engine)}")
            current, head = await migrate.current_revision(engine), migrate.head_revision()
            print(f"revision: {current or '(not migrated)'} (latest: {head})" + ("" if current == head else "  <- run: python -m worker.db migrate"))
            return 0 if current == head else 1
    except Exception as exc:  # noqa: BLE001 - report a readable message; never echo the URL
        print(f"error: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker.db", description="Database migrations and checks.")
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("migrate", help="apply migrations (upgrade to head)")
    up.add_argument("--revision", default="head")
    sub.add_parser("check", help="print the masked URL, the server version and the current migration revision")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.command, getattr(args, "revision", "head")))


if __name__ == "__main__":
    sys.exit(main())
