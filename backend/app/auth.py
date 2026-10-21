"""Operator authentication (Prompt 4.1, fixes BUG-06): bcrypt-hashed
passwords in the `operators` table, signed JWTs (PyJWT) for the
`Authorization: Bearer <jwt>` header `GET /pending-approvals` and
`POST /approval-decision` require.

The operator identity (`operator_id`) a request acts as always comes from
a verified token, never from the request body -- a body `operator_id` is
accepted (for the `/approval-decision` contract's backward compatibility)
but ignored. This is the same principle CLAUDE.md's "Things to Never Do"
already states for `input_text`: never let anything an attacker could
shape stand in for a real, authenticated identity.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import Header, HTTPException

from app import config

JWT_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    """Hashes a plaintext password with bcrypt (a fresh random salt every
    call), for storing in `storage.create_operator`."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Checks a plaintext password against a bcrypt hash from
    `storage.get_operator_by_username`."""
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_token(operator_id: int, username: str) -> str:
    """Signs a JWT for a successfully-authenticated operator, expiring
    `config.JWT_EXPIRY_HOURS` hours from now. `username` is what
    `require_operator` later reports as the caller's `operator_id` -- a
    readable identity (e.g. `"reviewer_jane"`), matching how `operator_id`
    was already used as a free-form string before real auth existed."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(operator_id),
        "username": username,
        "iat": now,
        "exp": now + timedelta(hours=config.JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=JWT_ALGORITHM)


@dataclass(frozen=True)
class AuthenticatedOperator:
    """What a verified `Authorization: Bearer <jwt>` header resolves to.
    `operator_id` is the operator's `username` -- the identity any
    endpoint should record as who acted, never a value from the request
    body."""

    id: int
    operator_id: str


def _decode_token(token: str) -> AuthenticatedOperator:
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired; log in again.") from None
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}") from None
    return AuthenticatedOperator(id=int(payload["sub"]), operator_id=payload["username"])


def require_operator(authorization: str | None = Header(default=None)) -> AuthenticatedOperator:
    """FastAPI dependency: `operator: AuthenticatedOperator =
    Depends(require_operator)` on any route that needs a logged-in
    operator. Requires `Authorization: Bearer <jwt>` (from
    `POST /auth/login`); raises `HTTPException(401)` if the header is
    missing, malformed, or the token is invalid/expired.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header; expected 'Bearer <token>'.",
        )
    token = authorization.removeprefix("Bearer ").strip()
    return _decode_token(token)
