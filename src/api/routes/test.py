"""Test routes for load testing"""
from fastapi import APIRouter
from src.schemas.common import MessageResponse

router = APIRouter(prefix="/api/v1", tags=["Testing"])


@router.get("/test", response_model=MessageResponse)
async def test_endpoint():
    """Test endpoint for load testing"""
    return {
        "message": "This is a test endpoint for load testing"
    }
