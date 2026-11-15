import hmac
import os

from fastapi import Depends, Header, HTTPException, Request, Security
from fastapi.security import APIKeyCookie, APIKeyHeader

from app.auth import Principal, check_csrf, cookie_name, current_principal


API_KEY_HEADER_NAME = "X-Coverage-Api-Key"
_api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER_NAME, scheme_name="CoverageApiKey", auto_error=False
)


def configured_api_key() -> str | None:
    value = os.environ.get("COVERAGE_API_KEY", "")
    return value if len(value) >= 32 and value.isascii() else None


def is_admin(request: Request) -> bool:
    expected = configured_api_key()
    provided = request.headers.getlist(API_KEY_HEADER_NAME)
    return bool(
        expected
        and len(provided) == 1
        and provided[0].isascii()
        and hmac.compare_digest(provided[0], expected)
    )



_cookie_scheme = APIKeyCookie(name=cookie_name(), scheme_name="CoverageSession", auto_error=False)
ROLE_RANK = {"viewer": 0, "analyst": 1, "operator": 2, "admin": 3}


def require_role(role: str, *, principal_dependency=current_principal):
    async def dependency(
        request: Request,
        principal: Principal = Depends(principal_dependency),
        _key: str | None = Security(_api_key_scheme),
        _cookie: str | None = Security(_cookie_scheme),
        _csrf: str | None = Header(default=None, alias="X-CSRF-Token",
                                  description="Required with a browser session for unsafe methods; obtain from /api/auth/session."),
    ) -> Principal:
        if not principal.authenticated:
            if configured_api_key() is None and not request.headers.get("cookie"):
                raise HTTPException(503, "Coverage API authentication is not configured")
            raise HTTPException(401, "Authentication required")
        if ROLE_RANK[principal.role] < ROLE_RANK[role]:
            raise HTTPException(403, "Insufficient role")
        if not principal.api_key and request.method not in {"GET", "HEAD", "OPTIONS"}:
            check_csrf(request, principal)
        return principal
    return dependency


require_operator = require_role("operator")
require_analyst = require_role("analyst")
require_user_admin = require_role("admin")
