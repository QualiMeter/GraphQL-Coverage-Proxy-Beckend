"""Fail-closed API-key authentication for privileged coverage operations.

The administrative key is not a GraphQL upstream credential. In particular,
the proxy must never forward it to TARGET_GRAPHQL_URL.
"""

import hmac
import os

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader


API_KEY_HEADER_NAME = "X-Coverage-Api-Key"
_api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER_NAME, scheme_name="CoverageApiKey", auto_error=False
)


def configured_api_key() -> str | None:
    value = os.environ.get("COVERAGE_API_KEY", "")
    # Refuse short or accidentally unset production secrets.
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


def require_admin(
    request: Request, _api_key: str | None = Security(_api_key_scheme)
) -> None:
    if configured_api_key() is None:
        raise HTTPException(status_code=503, detail="Coverage API authentication is not configured")
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="Invalid or missing Coverage API key")
