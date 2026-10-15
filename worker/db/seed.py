"""Demo data: two organisations with users in each role. Idempotent; existing rows (and passwords) are left alone."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.db.models import DEFAULT_ORG_SETTINGS, Membership, Org, User

_hasher = PasswordHasher()

# (slug, name, [(local part of the e-mail, role, display name)])
DEMO_ORGS = [
    ("acme", "Acme Corp", [("owner", "owner", "Olivia Owner"), ("admin", "admin", "Adam Admin"),
                           ("operator", "operator", "Omar Operator"), ("approver", "approver", "Alice Approver"),
                           ("viewer", "viewer", "Victor Viewer")]),
    ("globex", "Globex", [("operator", "operator", "Gina Operator"), ("approver", "approver", "Gus Approver")]),
]


def hash_password(password: str) -> str:
    """An argon2id hash (self-describing: it carries its own salt and parameters)."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Does ``password`` match ``password_hash``?"""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def generate_password() -> str:
    """A random demo password (16 url-safe characters)."""
    return secrets.token_urlsafe(12)


@dataclass
class SeedResult:
    """What ``seed_demo`` did. ``users`` maps e-mail to role for the users created in this call."""

    orgs_created: list[str] = field(default_factory=list)
    users_created: dict[str, str] = field(default_factory=dict)
    memberships_created: int = 0
    org_ids: dict[str, str] = field(default_factory=dict)  # slug -> org id (all demo orgs, new or existing)


async def seed_demo(sessionmaker: async_sessionmaker[AsyncSession], password: str) -> SeedResult:
    """Create the demo orgs, users and memberships that do not exist yet. Users get ``password`` (argon2-hashed)."""
    result = SeedResult()
    async with sessionmaker() as session, session.begin():
        for slug, name, members in DEMO_ORGS:
            org = await session.scalar(select(Org).where(Org.slug == slug))
            if org is None:
                org = Org(slug=slug, name=name, settings=dict(DEFAULT_ORG_SETTINGS))
                session.add(org)
                await session.flush()
                result.orgs_created.append(slug)
            result.org_ids[slug] = str(org.id)
            for local, role, display_name in members:
                email = f"{local}@{slug}.test"
                user = await session.scalar(select(User).where(func.lower(User.email) == email))
                if user is None:
                    user = User(email=email, password_hash=hash_password(password), display_name=display_name)
                    session.add(user)
                    await session.flush()
                    result.users_created[email] = role
                created = await session.execute(
                    pg_insert(Membership).values(org_id=org.id, user_id=user.id, role=role).on_conflict_do_nothing()
                    .returning(Membership.user_id))
                result.memberships_created += 1 if created.first() else 0
    return result
