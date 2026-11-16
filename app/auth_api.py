import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from anyio.lowlevel import RunVar

from app.access import require_user_admin
from app.auth import (
    DUMMY_HASH, SESSION_SECONDS, Principal, check_csrf, check_origin, cookie_name,
    current_principal, dev_mode, hash_password, session_out, session_token,
    throttle_login, token_hash, verify_password,
)
from app.auth_contracts import LoginInput, SessionOut, UserCreate, UserOut, UserUpdate
from app.auth_models import BrowserSession, ServiceGrant, User
from app.database import get_session
from app.models import GraphQLService

router = APIRouter(prefix="/api/auth", tags=["Authentication"],
                   responses={401: {"description": "Authentication required"},
                              403: {"description": "Insufficient role or invalid Origin/CSRF"}})
# Bound password work per worker without binding a semaphore to a previous test loop.
# AnyIO's limiter is created in the active async context by password_work().
_password_limiter = RunVar("coverage_password_limiter")


async def password_work(fn, *args):
    import anyio
    try:
        limiter = _password_limiter.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(2)
        _password_limiter.set(limiter)
    return await anyio.to_thread.run_sync(fn, *args, limiter=limiter)


def user_out(user: User) -> UserOut:
    return UserOut(id=user.id, username=user.username, role=user.role, is_active=user.is_active)


@router.post("/login", response_model=SessionOut, responses={401: {"description": "Invalid credentials"},
              403: {"description": "Origin or HTTPS rejected"}, 429: {"description": "Login throttled"}})
async def login(payload: LoginInput, request: Request, response: Response,
                session: AsyncSession = Depends(get_session)):
    check_origin(request)
    await throttle_login(session, payload.username, request.client.host if request.client else "unknown")
    user = await session.scalar(select(User).where(User.username == payload.username))
    valid = await password_work(verify_password, payload.password.get_secret_value(),
                                user.password_hash if user else DUMMY_HASH)
    if user is None or not user.is_active or not valid:
        raise HTTPException(401, "Invalid username or password")
    # Re-read and lock: role, password, or active status could change during KDF.
    checked_hash = user.password_hash
    user = await session.scalar(select(User).where(User.id == user.id).with_for_update()
                                .execution_options(populate_existing=True))
    if user is None or not user.is_active or user.password_hash != checked_hash:
        raise HTTPException(401, "Invalid username or password")
    previous = session_token(request)
    if previous:
        await session.execute(delete(BrowserSession).where(BrowserSession.token_hash == token_hash(previous)))
    # Rotate on every login; old supplied tokens never become authenticated.
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS)
    session.add(BrowserSession(token_hash=token_hash(token), user_id=user.id, expires_at=expires))
    await session.commit()
    response.set_cookie(cookie_name(), token, max_age=SESSION_SECONDS, path="/",
                        secure=not dev_mode(), httponly=True, samesite="lax")
    return session_out(Principal(user.id, user.role, False, token, expires, user.username))


@router.get("/session", response_model=SessionOut,
            description="Current browser user and CSRF token; anonymous callers receive null fields. Never cached.")
async def get_browser_session(principal: Principal = Depends(current_principal)):
    return session_out(principal)


@router.post("/logout", status_code=204, responses={403: {"description": "CSRF/Origin rejected"}})
async def logout(request: Request, response: Response, session: AsyncSession = Depends(get_session),
                 principal: Principal = Depends(current_principal)):
    check_origin(request)
    if principal.token:
        check_csrf(request, principal)
        await session.execute(delete(BrowserSession).where(BrowserSession.token_hash == token_hash(principal.token)))
        await session.commit()
    response.delete_cookie(cookie_name(), path="/", secure=not dev_mode(), httponly=True, samesite="lax")


@router.post("/users", status_code=201, response_model=UserOut,
             responses={409: {"description": "Username exists"}})
async def create_user(payload: UserCreate, session: AsyncSession = Depends(get_session),
                      principal: Principal = Depends(require_user_admin)):
    user = User(username=payload.username, role=payload.role,
                password_hash=await password_work(hash_password, payload.password.get_secret_value()))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Username already exists") from None
    return user_out(user)


@router.get("/users", response_model=list[UserOut])
async def list_users(session: AsyncSession = Depends(get_session),
                     principal: Principal = Depends(require_user_admin)):
    return [user_out(user) for user in (await session.scalars(select(User).order_by(User.username, User.id))).all()]


@router.patch("/users/{user_id}", response_model=UserOut,
              responses={404: {"description": "User not found"}, 409: {"description": "Last administrator"}})
async def update_user(user_id: uuid.UUID, payload: UserUpdate,
                      session: AsyncSession = Depends(get_session),
                      principal: Principal = Depends(require_user_admin)):
    changes = payload.model_dump(exclude_unset=True)
    if not changes or any(value is None for value in changes.values()):
        raise HTTPException(422, "Provide non-null account changes")
    encoded = await password_work(hash_password, payload.password.get_secret_value()) if payload.password else None
    # Serialize administrator changes with bootstrap; never remove the last active admin.
    await session.execute(text("SELECT pg_advisory_xact_lock(719842031)"))
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update()
                                .execution_options(populate_existing=True))
    if user is None:
        raise HTTPException(404, "User not found")
    if user.role == "admin" and user.is_active and (changes.get("role", "admin") != "admin"
                                                    or changes.get("is_active") is False):
        other = await session.scalar(select(User.id).where(User.role == "admin", User.is_active.is_(True), User.id != user_id))
        if other is None:
            raise HTTPException(409, "Cannot disable or demote the last active administrator")
    if "role" in changes:
        user.role = payload.role
    if "is_active" in changes:
        user.is_active = payload.is_active
    if encoded:
        user.password_hash = encoded
    await session.execute(delete(BrowserSession).where(BrowserSession.user_id == user.id))
    await session.commit()
    return user_out(user)


@router.put("/users/{user_id}/services/{service_id}", status_code=204,
            responses={404: {"description": "User or service not found"}})
async def grant_service(user_id: uuid.UUID, service_id: uuid.UUID,
                        session: AsyncSession = Depends(get_session),
                        principal: Principal = Depends(require_user_admin)):
    if await session.get(User, user_id) is None or await session.get(GraphQLService, service_id) is None:
        raise HTTPException(404, "User or service not found")
    await session.execute(insert(ServiceGrant).values(user_id=user_id, service_id=service_id).on_conflict_do_nothing())
    await session.commit()


@router.delete("/users/{user_id}/services/{service_id}", status_code=204)
async def revoke_service(user_id: uuid.UUID, service_id: uuid.UUID,
                         session: AsyncSession = Depends(get_session),
                         principal: Principal = Depends(require_user_admin)):
    await session.execute(delete(ServiceGrant).where(ServiceGrant.user_id == user_id, ServiceGrant.service_id == service_id))
    await session.commit()
