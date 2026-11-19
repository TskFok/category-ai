from dataclasses import dataclass
from enum import StrEnum

from fastapi import Depends, Header, status

from app.core.config import Settings, get_settings
from app.core.errors import AppError


class UserRole(StrEnum):
    ADMIN = "admin"
    REVIEWER = "reviewer"
    READER = "reader"


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: UserRole


def get_current_principal(
    authorization: str | None = Header(default=None),
    x_user_role: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """
    V1 single-admin boundary (ADR-07).
    - In development/test without ADMIN_API_TOKEN configured, defaults to local-admin.
    - When ADMIN_API_TOKEN is set in production, requires Bearer token match.
    """
    if settings.app_env == "production" and settings.admin_api_token:
        expected = f"Bearer {settings.admin_api_token}"
        if authorization != expected:
            raise AppError(
                code="UNAUTHORIZED",
                message="Invalid or missing authorization token",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

    role = UserRole.ADMIN
    if x_user_role in (UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER):
        role = UserRole(x_user_role)
    return Principal(user_id="local-admin", role=role)


def require_roles(*allowed: UserRole):
    def _dep(principal: Principal = Depends(get_current_principal)) -> Principal:
        if principal.role not in allowed:
            raise AppError(
                code="FORBIDDEN",
                message=f"Role '{principal.role}' is not permitted for this action",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        return principal

    return _dep
