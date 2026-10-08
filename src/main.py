"""
FastAPI Application Entry Point
"""
from fastapi import FastAPI
from contextlib import asynccontextmanager

from src.database import db
from src.core.config import get_settings
from src.api.routes import health, test, database


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events"""
    # Startup: Connect to ClickHouse
    settings = get_settings()
    print(f"🚀 Starting {settings.app_name} v{settings.app_version}")
    db.connect()
    db.init_schema()
    yield
    # Shutdown: Disconnect from ClickHouse
    db.disconnect()
    print("👋 Shutting down")


def create_app() -> FastAPI:
    """Application factory"""
    settings = get_settings()
    
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc"
    )
    
    # Include routers
    app.include_router(health.router)
    app.include_router(test.router)
    app.include_router(database.router)
    
    return app


# Create application instance
app = create_app()

