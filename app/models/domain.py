from datetime import datetime, date
from typing import List, Optional
from uuid import UUID, uuid4

from sqlmodel import Field, Relationship, SQLModel


class Household(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    invite_code: Optional[str] = None

    users: List["User"] = Relationship(back_populates="household")
    accounts: List["Account"] = Relationship(back_populates="household")
    categories: List["Category"] = Relationship(back_populates="household")
    transactions: List["Transaction"] = Relationship(back_populates="household")
    file_imports: List["FileImport"] = Relationship(back_populates="household")


class User(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    email: str = Field(index=True, unique=True)
    name: str
    hashed_password: str
    profile_picture_url: Optional[str] = None
    household_id: UUID = Field(foreign_key="household.id")
    access_code: Optional[str] = Field(default=None, unique=True, index=True)

    household: Optional[Household] = Relationship(back_populates="users")
    accounts: List["Account"] = Relationship(back_populates="user")


class Account(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str
    type: str
    bank_slug: Optional[str] = None
    last_4_digits: Optional[str] = None
    balance: float = Field(default=0.0)
    credit_limit: Optional[float] = None
    user_id: UUID = Field(foreign_key="user.id")
    household_id: UUID = Field(foreign_key="household.id")

    user: Optional[User] = Relationship(back_populates="accounts")
    household: Optional[Household] = Relationship(back_populates="accounts")
    transactions: List["Transaction"] = Relationship(back_populates="account")
    balance_history: List["AccountBalanceHistory"] = Relationship(back_populates="account")


class AccountBalanceHistory(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    account_id: UUID = Field(foreign_key="account.id")
    balance: float
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    account: Optional[Account] = Relationship(back_populates="balance_history")


class Category(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str
    type: str
    color: str = Field(default="#6b7280")
    household_id: Optional[UUID] = Field(default=None, foreign_key="household.id")

    household: Optional[Household] = Relationship(back_populates="categories")
    transactions: List["Transaction"] = Relationship(back_populates="category")


class FileImport(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    filename: str
    display_name: str
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)
    household_id: UUID = Field(foreign_key="household.id")
    status: str = Field(default="PENDING")
    progress_message: Optional[str] = None
    error_message: Optional[str] = None
    payload: Optional[str] = None

    household: Optional[Household] = Relationship(back_populates="file_imports")
    transactions: List["Transaction"] = Relationship(back_populates="file_import")


class Transaction(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    date: date
    description: str
    amount: float
    account_id: UUID = Field(foreign_key="account.id")
    category_id: Optional[UUID] = Field(default=None, foreign_key="category.id")
    user_id: Optional[UUID] = Field(default=None, foreign_key="user.id")
    household_id: UUID = Field(foreign_key="household.id")
    is_transfer: bool = Field(default=False)
    transaction_hash: str = Field(unique=True, index=False)
    file_import_id: Optional[UUID] = Field(default=None, foreign_key="fileimport.id")
    installment_number: Optional[int] = Field(default=None)
    total_installments: Optional[int] = Field(default=None)
    reference_month: Optional[int] = Field(default=None)
    reference_year: Optional[int] = Field(default=None)
    status: str = Field(default="CONFIRMED")

    account: Optional[Account] = Relationship(back_populates="transactions")
    category: Optional[Category] = Relationship(back_populates="transactions")
    household: Optional[Household] = Relationship(back_populates="transactions")
    file_import: Optional[FileImport] = Relationship(back_populates="transactions")
