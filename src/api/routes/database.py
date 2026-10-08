"""Database-related routes"""
from fastapi import APIRouter, HTTPException, Depends

from src.database import get_db, ClickHouseDatabase
from src.schemas.common import DatabaseVersionResponse
from src.schemas.query import QueryRequest, QueryResponse

router = APIRouter(prefix="/api/v1/db", tags=["Database"])


@router.get("/test", response_model=DatabaseVersionResponse)
async def test_database(db: ClickHouseDatabase = Depends(get_db)):
    """Test ClickHouse database connection"""
    try:
        result = db.execute_query("SELECT version() as version, currentDatabase() as database")
        
        return {
            "status": "success",
            "message": "ClickHouse connection is working",
            "version": result.result_rows[0][0] if result.result_rows else None,
            "database": result.result_rows[0][1] if result.result_rows else None
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@router.post("/query", response_model=QueryResponse)
async def execute_query(query: QueryRequest, db: ClickHouseDatabase = Depends(get_db)):
    """Execute a custom ClickHouse query"""
    try:
        result = db.execute_query(query.sql)
        
        return {
            "status": "success",
            "rows": result.result_rows,
            "row_count": len(result.result_rows)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Query error: {str(e)}")
