from sqlmodel import SQLModel, create_engine
from sqlmodel import Session

from app.core.config import settings

DATABASE_URL = settings.DATABASE_URL or "sqlite:///./moneyin.db"

connect_args = {
    "check_same_thread": False
} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, echo=False, connect_args=connect_args)


def get_session():
    with Session(engine) as session:
        yield session
