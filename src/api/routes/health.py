"""Health check routes"""
from fastapi import APIRouter, Depends

from src.database import get_db, ClickHouseDatabase
from src.schemas.common import HealthResponse, MessageResponse

router = APIRouter(tags=["Health"])


@router.get("/", response_model=MessageResponse)
async def root():
    """Root endpoint"""
    return {"message": "Welcome to LLM Load Testing API"}


@router.get("/health", response_model=HealthResponse)
async def health_check(db: ClickHouseDatabase = Depends(get_db)):
    """Health check endpoint with database status"""
    database_status = "connected" if db.ping() else "disconnected"
    
    return {
        "status": "healthy",
        "service": "llm-load-testing",
        "database": database_status
    }
