from uuid import uuid4

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.core.database import get_session
from app.models.domain import Household, User, Account

router = APIRouter()


@router.post("/setup/create-test-data")
def create_test_data(db: Session = Depends(get_session)):
    """Create test data for quick testing: Household, User, and Account."""
    
    # Create household
    household = Household(name="Test Household")
    db.add(household)
    db.flush()
    
    # Create user
    user = User(
        email="test@example.com",
        name="Test User",
        household_id=household.id,
    )
    db.add(user)
    db.flush()
    
    # Create account
    account = Account(
        name="Test Account",
        type="Corrente",
        user_id=user.id,
        household_id=household.id,
    )
    db.add(account)
    db.commit()
    
    return {
        "household_id": str(household.id),
        "user_id": str(user.id),
        "account_id": str(account.id),
        "email": user.email,
    }
