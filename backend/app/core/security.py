from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum

from fastapi import Cookie, Depends, Header, Query, status

from app.core.config import Settings, get_settings
from app.core.errors import AppError

WEAK_TOKENS = frozenset({"", "change-me-in-production", "changeme", "secret", "admin", "password"})


class UserRole(StrEnum):
    ADMIN = "admin"
    REVIEWER = "reviewer"
    READER = "reader"


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: UserRole


def validate_production_auth_config(settings: Settings) -> None:
    """
    FIX-04: Fail closed in production if ADMIN_API_TOKEN is missing or weak.
    """
    if settings.app_env != "production":
        return
    token = (settings.admin_api_token or "").strip()
    if len(token) < 16 or token.lower() in WEAK_TOKENS:
        raise RuntimeError(
            "Production startup blocked: ADMIN_API_TOKEN must be set to a strong token (>= 16 chars)"
        )


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _match_token(provided: str, expected: str) -> bool:
    if not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _resolve_principal_from_token(token: str | None, settings: Settings) -> Principal | None:
    if not token:
        return None
    if settings.admin_api_token and _match_token(token, settings.admin_api_token):
        return Principal(user_id="admin", role=UserRole.ADMIN)
    if settings.reviewer_api_token and _match_token(token, settings.reviewer_api_token):
        return Principal(user_id="reviewer", role=UserRole.REVIEWER)
    if settings.reader_api_token and _match_token(token, settings.reader_api_token):
        return Principal(user_id="reader", role=UserRole.READER)
    return None


def get_current_principal(
    authorization: str | None = Header(default=None),
    x_user_role: str | None = Header(default=None),
    ncke_session_token: str | None = Cookie(default=None),
    access_token: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """
    FIX-04:
    - In production, ALWAYS require a valid configured token (Bearer header, HttpOnly cookie,
      or query token for SSE/media streams). Never trust client-supplied X-User-Role header.
    - When tokens are configured in non-production and a token is provided, derive role strictly
      from the verified token (never from X-User-Role).
    - In local development/test with no tokens configured, allow local-admin default and
      optional X-User-Role ONLY for local RBAC testing.
    """
    token = _extract_bearer(authorization) or ncke_session_token or access_token
    any_token_configured = bool(
        settings.admin_api_token or settings.reviewer_api_token or settings.reader_api_token
    )

    if settings.app_env == "production":
        if not settings.admin_api_token or len(settings.admin_api_token.strip()) < 16:
            raise AppError(
                code="AUTH_MISCONFIGURED",
                message="Server authentication is not properly configured for production",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        principal = _resolve_principal_from_token(token, settings)
        if principal is None:
            raise AppError(
                code="UNAUTHORIZED",
                message="Invalid or missing authorization token",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        return principal

    if any_token_configured:
        if token is not None:
            principal = _resolve_principal_from_token(token, settings)
            if principal is None:
                raise AppError(
                    code="UNAUTHORIZED",
                    message="Invalid authorization token",
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )
            return principal
        raise AppError(
            code="UNAUTHORIZED",
            message="Missing authorization token",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Local dev/test without configured tokens: default local-admin (X-User-Role only downgrades in dev/test)
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
