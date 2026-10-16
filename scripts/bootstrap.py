"""Create the PostgreSQL database, run the migrations and seed the demo orgs and users (safe to run repeatedly).

    python scripts/bootstrap.py                       # uses DATABASE_URL from .env; prints a generated demo password once
    python scripts/bootstrap.py --demo-password ...   # choose the demo users' password
    python scripts/bootstrap.py --reset [--yes]       # drop and recreate the `oneforall` database first (asks to confirm)
    python scripts/bootstrap.py --provision-sim       # also create each demo org's own simulated company (sim must be running)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker.config import get_settings  # noqa: E402
from worker.db.bootstrap import DROPPABLE, bootstrap, describe  # noqa: E402
from worker.db.session import mask_url, normalize_url  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo-password", help="password for the demo users (default: generated and printed once)")
    parser.add_argument("--reset", action="store_true", help="drop and recreate the database first (DESTROYS ALL ITS DATA)")
    parser.add_argument("--provision-sim", action="store_true",
                        help="create the demo orgs' simulated companies and store their logins in the vault "
                             "(needs the simulator at SIM_BASE_URL; skipped with a message if it is not running)")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation with --reset")
    args = parser.parse_args(argv)

    raw = get_settings().DATABASE_URL.strip()
    if not raw:
        print("error: DATABASE_URL is not set (see .env.example)", file=sys.stderr)
        return 2
    url = normalize_url(raw)
    if args.reset:
        if url.database not in DROPPABLE:
            print(f"error: --reset only works on {' / '.join(sorted(DROPPABLE))}, not '{url.database}'", file=sys.stderr)
            return 2
        if not args.yes and input(f"This DELETES the database '{url.database}' and everything in it. Type its name to confirm: ").strip() != url.database:
            print("aborted.")
            return 1
    try:
        result = asyncio.run(bootstrap(url, demo_password=args.demo_password, reset=args.reset, provision_sim=args.provision_sim))
    except Exception as exc:  # noqa: BLE001 - a readable message that never contains the password
        print(f"error: could not bootstrap {mask_url(url)}: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}", file=sys.stderr)
        return 2
    print(describe(result, url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
