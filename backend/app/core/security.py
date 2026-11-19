from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from enum import StrEnum

from fastapi import Cookie, Depends, Header, Query, Request, status

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


def _signing_secret(settings: Settings) -> bytes:
    secret = (settings.admin_api_token or "ncke-dev-media-signing-key").encode("utf-8")
    return hashlib.sha256(secret + b":media-token-v1").digest()


def create_signed_media_token(
    principal: Principal,
    path: str,
    settings: Settings,
    ttl_seconds: int | None = None,
) -> tuple[str, int]:
    """
    P1-08: Create a short-lived HMAC-signed token scoped to a specific media path & expiry.
    Prevents leaking long-lived API tokens into browser history, logs, or referrers.
    """
    ttl = ttl_seconds if ttl_seconds is not None else settings.media_token_ttl_seconds
    exp = int(time.time()) + max(10, ttl)
    payload = f"{principal.user_id}|{principal.role.value}|{path}|{exp}".encode("utf-8")
    sig = hmac.new(_signing_secret(settings), payload, hashlib.sha256).hexdigest()
    raw = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{raw}.{sig}", exp


def verify_signed_media_token(
    token: str | None,
    expected_path: str,
    settings: Settings,
) -> Principal | None:
    if not token or "." not in token:
        return None
    try:
        raw_b64, provided_sig = token.rsplit(".", 1)
        pad = "=" * ((4 - len(raw_b64) % 4) % 4)
        payload = base64.urlsafe_b64decode(raw_b64 + pad)
        expected_sig = hmac.new(_signing_secret(settings), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided_sig, expected_sig):
            return None
        user_id, role_str, path, exp_str = payload.decode("utf-8").split("|", 3)
        if int(exp_str) < int(time.time()):
            return None
        if path != expected_path:
            return None
        return Principal(user_id=user_id, role=UserRole(role_str))
    except Exception:
        return None


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
    request: Request,
    authorization: str | None = Header(default=None),
    x_user_role: str | None = Header(default=None),
    ncke_session_token: str | None = Cookie(default=None),
    access_token: str | None = Query(default=None),
    media_token: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """
    FIX-04 & P1-08:
    - In production, ALWAYS require a valid Bearer header, HttpOnly session cookie,
      or short-lived path-scoped HMAC `media_token`. Generic long-lived `access_token`
      query parameters are REJECTED in production to prevent credential leaks.
    - Never trust client-supplied X-User-Role in production or when tokens are configured.
    """
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
        # Check short-lived path-scoped media_token first if provided
        if media_token:
            signed_principal = verify_signed_media_token(media_token, request.url.path, settings)
            if signed_principal is not None:
                return signed_principal
            raise AppError(
                code="INVALID_MEDIA_TOKEN",
                message="Expired or invalid signed media token",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        # Disallow long-lived access_token in URL query in production
        if access_token and not authorization and not ncke_session_token:
            raise AppError(
                code="QUERY_TOKEN_DISABLED_IN_PRODUCTION",
                message="Long-lived access_token query parameter is disabled in production; use HttpOnly cookie or signed media_token",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        token = _extract_bearer(authorization) or ncke_session_token
        principal = _resolve_principal_from_token(token, settings)
        if principal is None:
            raise AppError(
                code="UNAUTHORIZED",
                message="Invalid or missing authorization token",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        return principal

    if media_token:
        signed_principal = verify_signed_media_token(media_token, request.url.path, settings)
        if signed_principal is not None:
            return signed_principal

    token = _extract_bearer(authorization) or ncke_session_token or access_token
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
