import asyncio
import hashlib
import hmac
import os
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import case, delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_contracts import SessionOut, UserOut
from app.auth_models import BrowserSession, LoginThrottle, User
from app.database import get_session

SESSION_SECONDS = 12 * 60 * 60
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID | None = None
    role: str | None = None
    api_key: bool = False
    token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None
    username: str | None = None

    @property
    def admin(self) -> bool:
        return self.role == "admin"

    @property
    def authenticated(self) -> bool:
        return self.role is not None


ANONYMOUS = Principal()


def dev_mode() -> bool:
    return os.environ.get("COVERAGE_DEV_MODE", "").lower() == "true"


def cookie_name() -> str:
    return "coverage_dev_session" if dev_mode() else "__Host-coverage_session"


def public_origin() -> str:
    from urllib.parse import urlsplit

    value = os.environ.get("COVERAGE_PUBLIC_ORIGIN", "https://graphql.rebyte.fun")
    try:
        parts = urlsplit(value)
        # A port is optional; reject paths, credentials, and malformed ports.
        valid = (parts.scheme in ({"http", "https"} if dev_mode() else {"https"})
                 and parts.hostname and parts.netloc and parts.path == ""
                 and not parts.query and not parts.fragment and "@" not in parts.netloc)
        _ = parts.port
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(503, "Browser authentication origin is not configured correctly")
    return value


def check_origin(request: Request) -> None:
    origins = request.headers.getlist("origin")
    if origins != [public_origin()] or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Same-origin request required")
    if not dev_mode() and request.url.scheme != "https":
        raise HTTPException(403, "HTTPS is required for browser authentication")


def session_token(request: Request) -> str | None:
    # Starlette's cookie dict collapses duplicate cookie names. Reject ambiguity.
    values = [part.strip().split("=", 1)[1]
              for header in request.headers.getlist("cookie") for part in header.split(";")
              if "=" in part and part.strip().split("=", 1)[0] == cookie_name()]
    if len(values) != 1 or not TOKEN_PATTERN.fullmatch(values[0]):
        return None
    return values[0]


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def csrf_token(token: str) -> str:
    return hmac.new(token.encode("ascii"), b"coverage-csrf-v1", hashlib.sha256).hexdigest()


def check_csrf(request: Request, principal: Principal) -> None:
    check_origin(request)
    provided = request.headers.getlist("x-csrf-token")
    if (principal.token is None or len(provided) != 1 or not provided[0].isascii()
            or not hmac.compare_digest(provided[0], csrf_token(principal.token))):
        raise HTTPException(403, "Invalid or missing CSRF token")


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    # OWASP scrypt minimum: N=2^17, r=8, p=1 (128 MiB); use a distinct random salt.
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=131072, r=8, p=1,
                            maxmem=256 * 1024 * 1024, dklen=32)
    return f"scrypt$131072$8$1${salt.hex()}${digest.hex()}"


# Fixed dummy hash has the same work factor; unknown users still verify a password.
DUMMY_HASH = "scrypt$131072$8$1$" + "00" * 16 + "$" + "00" * 32


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if (algorithm, n, r, p) != ("scrypt", "131072", "8", "1"):
            return False
        actual = hash_password(password, salt=bytes.fromhex(salt)).rsplit("$", 1)[1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


async def current_principal(request: Request, session: AsyncSession = Depends(get_session)) -> Principal:
    from app.access import is_admin

    if is_admin(request):
        return Principal(role="admin", api_key=True)
    # An explicit API key takes precedence, including invalid credentials.
    if request.headers.getlist("x-coverage-api-key"):
        return ANONYMOUS
    token = session_token(request)
    if token is None:
        return ANONYMOUS
    row = (await session.execute(
        select(User, BrowserSession.expires_at)
        .join(BrowserSession, BrowserSession.user_id == User.id)
        .where(BrowserSession.token_hash == token_hash(token), User.is_active.is_(True),
               BrowserSession.expires_at > datetime.now(timezone.utc))
    )).first()
    if row is None:
        return ANONYMOUS
    user, expires = row
    return Principal(user.id, user.role, False, token, expires, user.username)


def session_out(principal: Principal) -> SessionOut:
    if principal.token is None:
        return SessionOut(user=None, csrf_token=None, expires_at=None)
    return SessionOut(user=UserOut(id=principal.user_id, username=principal.username,
                                  role=principal.role, is_active=True),
                      csrf_token=csrf_token(principal.token), expires_at=principal.expires_at)


async def throttle_login(session: AsyncSession, username: str, peer: str) -> None:
    now = datetime.now(timezone.utc)
    # Atomic upserts count successful and failed attempts across processes. Keys
    # contain no raw usernames/IPs. Never trust client-supplied forwarding headers.
    blocked = False
    for identity, maximum in (("peer:" + peer, 30), ("account:" + username, 10)):
        key = hashlib.sha256(identity.encode()).hexdigest()
        stmt = insert(LoginThrottle).values(bucket_key=key, attempts=1, expires_at=now + timedelta(minutes=15))
        stmt = stmt.on_conflict_do_update(
            index_elements=[LoginThrottle.bucket_key],
            set_={"attempts": case(
                      (LoginThrottle.expires_at <= now, 1), else_=LoginThrottle.attempts + 1),
                  "expires_at": case(
                      (LoginThrottle.expires_at <= now, now + timedelta(minutes=15)),
                      else_=LoginThrottle.expires_at)},
        ).returning(LoginThrottle.attempts)
        blocked = (await session.scalar(stmt)) > maximum
        if blocked:
            break
    await session.commit()
    if blocked:
        raise HTTPException(429, "Too many login attempts", headers={"Retry-After": "900"})


async def bootstrap_admin(session: AsyncSession, username: str, password: str) -> User:
    # Serialize all bootstrap processes, including the initially empty table.
    await session.execute(text("SELECT pg_advisory_xact_lock(719842031)"))
    if await session.scalar(select(User.id).where(User.role == "admin")) is not None:
        raise ValueError("An administrator already exists; use authenticated user management")
    from app.auth_contracts import UserCreate

    validated = UserCreate(username=username, password=password, role="admin")
    user = User(username=validated.username, role="admin",
                password_hash=await asyncio.to_thread(hash_password, validated.password.get_secret_value()))
    session.add(user)
    await session.commit()
    return user


async def cleanup_auth(session: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(delete(BrowserSession).where(BrowserSession.expires_at <= now))
    await session.execute(delete(LoginThrottle).where(LoginThrottle.expires_at <= now))
    await session.commit()


def visibility_kwargs(principal: Principal) -> dict:
    kwargs = {"admin": principal.admin}
    if principal.user_id is not None and not principal.admin:
        kwargs["user_id"] = principal.user_id
    return kwargs
