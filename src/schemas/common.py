"""Common schemas used across the application"""
from pydantic import BaseModel
from typing import Optional


class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    service: str
    database: str


class MessageResponse(BaseModel):
    """Generic message response"""
    message: str


class DatabaseVersionResponse(BaseModel):
    """Database version response"""
    status: str
    message: str
    version: Optional[str] = None
    database: Optional[str] = None
