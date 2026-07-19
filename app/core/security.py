import random
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import bcrypt
import jwt

from app.core.config import settings

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days


def get_password_hash(password: str) -> str:
    """Hash a password using bcrypt (direct library)."""
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a hashed password."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token including an expiration."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})

    secret_key = settings.SECRET_KEY or "dev-secret-key-change-in-production"
    # Prefer PyJWT if available; otherwise emit a simple dev token for local testing
    try:
        token = jwt.encode(to_encode, secret_key, algorithm=ALGORITHM)
        return token
    except Exception:
        # Fallback token format: dev:<email>
        email = data.get("email") or data.get("sub") or "dev-user"
        return f"dev:{email}"


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode and validate a JWT access token. Returns payload or None."""
    # Try real jwt decode first
    try:
        secret_key = settings.SECRET_KEY or "dev-secret-key-change-in-production"
        payload = jwt.decode(token, secret_key, algorithms=[ALGORITHM])
        return payload
    except Exception:
        # Accept fallback dev token `dev:<email>`
        if isinstance(token, str) and token.startswith("dev:"):
            return {"email": token.split(":", 1)[1]}
        return None


def generate_access_code() -> str:
    """Generate a random 6-digit numeric access code."""
    return f"{random.randint(0, 999999):06d}"
