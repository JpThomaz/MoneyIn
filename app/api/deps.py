from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.security import decode_access_token
from app.models.domain import User


reusable_oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


async def get_token_from_request(request: Request, token: str = Depends(reusable_oauth2)) -> str:
    """Extract token from Authorization header or cookies."""
    # Try Authorization header first
    if token:
        return token
    
    # Try from cookies
    cookie_token = request.cookies.get("access_token")
    if cookie_token:
        return cookie_token
    
    return None


async def get_current_user(db: Session = Depends(get_session), request: Request = None, token: str = Depends(get_token_from_request)) -> User:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")

    email = payload.get("email")
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")

    stmt = select(User).where(User.email == email)
    user = db.exec(stmt).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")

    return user
