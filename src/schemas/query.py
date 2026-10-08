"""Query-related schemas"""
from pydantic import BaseModel, Field
from typing import Any, List


class QueryRequest(BaseModel):
    """Request model for executing queries"""
    sql: str = Field(..., description="SQL query to execute", min_length=1)
    
    class Config:
        json_schema_extra = {
            "example": {
                "sql": "SELECT 1 as number, 'test' as text"
            }
        }


class QueryResponse(BaseModel):
    """Response model for query results"""
    status: str
    rows: List[List[Any]]
    row_count: int
