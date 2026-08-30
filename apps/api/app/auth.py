import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User

bearer = HTTPBearer(auto_error=False)
BCRYPT_ROUNDS = 12


def _bcrypt_input(password: str) -> tuple[bytes, bool]:
    raw = password.encode("utf-8")
    if len(raw) <= 72:
        return raw, False
    # bcrypt has a 72-byte input limit. Pre-hash only oversized inputs and
    # record that scheme explicitly so verification remains deterministic.
    return base64.b64encode(hashlib.sha256(raw).digest()), True


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("password must contain at least 8 characters")
    password_bytes, prehashed = _bcrypt_input(password)
    encoded = bcrypt.hashpw(password_bytes, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")
    return f"bcrypt_sha256${encoded}" if prehashed else encoded


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(encoded, str):
        return False

    try:
        if encoded.startswith("bcrypt_sha256$"):
            # The scheme is used only for inputs beyond bcrypt's byte limit.
            candidate = base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())
            return bcrypt.checkpw(candidate, encoded.removeprefix("bcrypt_sha256$").encode("ascii"))
        if encoded.startswith(("$2a$", "$2b$", "$2y$")):
            return bcrypt.checkpw(password.encode("utf-8"), encoded.encode("ascii"))

        # Backward compatibility for accounts created before the bcrypt
        # migration. Successful login upgrades these hashes in-place.
        algorithm, rounds, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (UnicodeError, ValueError, TypeError):
        return False


def password_needs_rehash(encoded: str) -> bool:
    candidate = encoded.removeprefix("bcrypt_sha256$") if isinstance(encoded, str) else ""
    if not candidate.startswith(("$2a$", "$2b$", "$2y$")):
        return True
    try:
        return int(candidate.split("$", 3)[2]) != BCRYPT_ROUNDS
    except (IndexError, ValueError):
        return True


def create_access_token(user_id: str, expires_minutes: int | None = None) -> str:
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=expires_minutes or settings.jwt_access_token_minutes)
    return jwt.encode({"sub": user_id, "iat": now, "exp": exp, "type": "access"}, settings.app_secret_key, algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.app_secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "UNAUTHENTICATED", "message": "Invalid or expired token"}) from exc


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "UNAUTHENTICATED", "message": "Authorization header required"})
    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    user = db.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "UNAUTHENTICATED", "message": "User is unavailable"})
    return user
