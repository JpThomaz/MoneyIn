import secrets
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.security import get_password_hash, verify_password, create_access_token, generate_access_code
from app.models.domain import User, Household
from app.api.deps import get_current_user, reusable_oauth2

router = APIRouter()


class RegisterRequest(BaseModel):
    email: str
    name: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class InviteRequest(BaseModel):
    pass  # No fields required for invite generation


class RegisterSpouseRequest(BaseModel):
    invite_code: str
    email: str
    name: str
    password: str


class LoginCodeRequest(BaseModel):
    code: str


@router.post("/auth/register")
async def register(request: RegisterRequest, db: Session = Depends(get_session)):
    """Register a new user and create a new Household for them (as admin)."""
    
    # Check if user already exists
    stmt = select(User).where(User.email == request.email)
    existing = db.exec(stmt).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already exists",
        )
    
    # Create household
    household = Household(name=f"{request.name}'s Family")
    db.add(household)
    db.flush()
    
    # Create user with hashed password and auto-generated access code
    user = User(
        id=uuid4(),
        email=request.email,
        name=request.name,
        household_id=household.id,
        hashed_password=get_password_hash(request.password),
        access_code=generate_access_code(),
    )
    db.add(user)
    db.commit()
    
    # Generate access token (include email for later resolution)
    token = create_access_token({"email": user.email})
    
    return {
        "status": "success",
        "user_id": str(user.id),
        "household_id": str(household.id),
        "email": user.email,
        "access_token": token,
        "token_type": "bearer",
    }


@router.post("/auth/invite")
async def generate_invite(db: Session = Depends(get_session), current_user: User = Depends(get_current_user)):
    """Generate and persist an invite code for the current user's household."""
    invite_code = secrets.token_urlsafe(24)
    stmt = select(Household).where(Household.id == current_user.household_id)
    household = db.exec(stmt).first()
    if not household:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Household not found")
    household.invite_code = invite_code
    db.add(household)
    db.commit()
    return {"status": "success", "invite_code": invite_code, "household_id": str(household.id)}


@router.post("/auth/register/spouse")
async def register_spouse(request: RegisterSpouseRequest, db: Session = Depends(get_session)):
    """Register a spouse using an invite code persisted on a Household."""
    if not request.invite_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid invite code")

    # Find household by invite_code
    stmt = select(Household).where(Household.invite_code == request.invite_code)
    household = db.exec(stmt).first()
    if not household:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid invite code or household not found")

    # Check if user already exists
    stmt = select(User).where(User.email == request.email)
    existing = db.exec(stmt).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User already exists")

    spouse = User(id=uuid4(), email=request.email, name=request.name, household_id=household.id, hashed_password=get_password_hash(request.password), access_code=generate_access_code())
    db.add(spouse)
    db.commit()
    token = create_access_token({"email": spouse.email})
    return {"status": "success", "user_id": str(spouse.id), "household_id": str(household.id), "email": spouse.email, "access_token": token, "token_type": "bearer"}


@router.post("/auth/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_session)):
    """Login with OAuth2 form (username=email) and return JWT access token via secure cookie."""
    stmt = select(User).where(User.email == form_data.username)
    user = db.exec(stmt).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    token = create_access_token({"email": user.email})
    
    # Return JSON response with cookie set
    response = JSONResponse({"access_token": token, "token_type": "bearer"})
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=False,  # Set to True in production (HTTPS)
        samesite="lax",
        max_age=7*24*60*60,  # 7 days
    )
    return response


@router.post("/auth/login-code")
async def login_with_code(request: LoginCodeRequest, db: Session = Depends(get_session)):
    """Login using a 6-digit numeric access code."""
    code = request.code.strip()
    if not code or len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Código inválido")

    stmt = select(User).where(User.access_code == code)
    user = db.exec(stmt).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Código inválido ou inexistente")

    token = create_access_token({"email": user.email})
    response = JSONResponse({"access_token": token, "token_type": "bearer", "user_name": user.name})
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=7*24*60*60,
    )
    return response


@router.post("/auth/generate-code")
async def generate_member_code(
    user_id: str,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Generate a new 6-digit access code for a household member."""
    uid = UUID(user_id)
    member = db.exec(select(User).where(User.id == uid)).first()
    if not member:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membro não encontrado")
    if member.household_id != current_user.household_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso negado")

    # Generate unique code
    for _ in range(20):
        code = generate_access_code()
        existing = db.exec(select(User).where(User.access_code == code, User.id != uid)).first()
        if not existing:
            break
    else:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Não foi possível gerar um código único")

    member.access_code = code
    db.add(member)
    db.commit()
    return {"status": "success", "access_code": code}
