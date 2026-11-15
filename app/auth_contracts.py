"""Frontend authentication contracts; secrets are input-only."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, SecretStr
from pydantic.alias_generators import to_camel

from app.api_models import ApiModel

Role = Literal["viewer", "analyst", "operator", "admin"]


class AuthInput(ApiModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class LoginInput(AuthInput):
    username: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_.-]*$")
    password: SecretStr = Field(min_length=1, max_length=128)


class UserCreate(LoginInput):
    password: SecretStr = Field(min_length=12, max_length=128)
    role: Role = "viewer"


class UserUpdate(AuthInput):
    role: Role | None = None
    is_active: bool | None = None
    password: SecretStr | None = Field(default=None, min_length=12, max_length=128)


class UserOut(ApiModel):
    id: uuid.UUID
    username: str
    role: Role
    is_active: bool


class SessionOut(ApiModel):
    user: UserOut | None
    csrf_token: str | None
    expires_at: datetime | None
