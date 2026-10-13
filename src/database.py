"""
ClickHouse database connection manager
"""
import clickhouse_connect
from typing import Optional
from clickhouse_connect.driver import Client

from src.core.config import get_settings


class ClickHouseDatabase:
    """ClickHouse database connection manager"""
    
    def __init__(self):
        self.settings = get_settings()
        self.client: Optional[Client] = None
    
    def connect(self) -> bool:
        """Establish connection to ClickHouse"""
        try:
            self.client = clickhouse_connect.get_client(
                host=self.settings.clickhouse_host,
                port=self.settings.clickhouse_port,
                username=self.settings.clickhouse_user,
                password=self.settings.clickhouse_password,
                database=self.settings.clickhouse_database
            )
            print(f"✅ Connected to ClickHouse at {self.settings.clickhouse_host}:{self.settings.clickhouse_port}")
            return True
        except Exception as e:
            print(f"❌ Failed to connect to ClickHouse: {e}")
            return False
    
    def disconnect(self) -> None:
        """Close connection to ClickHouse"""
        if self.client:
            self.client.close()
            self.client = None
            print("✅ Disconnected from ClickHouse")
    
    def insert(self, table: str, rows: list, column_names: list, settings: Optional[dict] = None):
        """Insert rows (list of lists) into a table"""
        if not self.client:
            raise ConnectionError("Not connected to ClickHouse")
        return self.client.insert(table, rows, column_names=column_names, settings=settings or {})

    def query_rows(self, query: str, parameters: Optional[dict] = None) -> list:
        """Run a parameterised SELECT and return its rows as dicts"""
        if not self.client:
            raise ConnectionError("Not connected to ClickHouse")
        result = self.client.query(query, parameters=parameters or {})
        return [dict(zip(result.column_names, row)) for row in result.result_rows]

    def export_parquet(self, query: str, parameters: Optional[dict] = None) -> bytes:
        """Run a SELECT and return the result as a Parquet file"""
        if not self.client:
            raise ConnectionError("Not connected to ClickHouse")
        return self.client.raw_query(query, parameters=parameters or {}, fmt="Parquet")

    def execute_query(self, query: str):
        """Execute a query and return results"""
        if not self.client:
            raise ConnectionError("Not connected to ClickHouse")
        return self.client.query(query)
    
    def execute_command(self, command: str):
        """Execute a command (INSERT, CREATE, etc.)"""
        if not self.client:
            raise ConnectionError("Not connected to ClickHouse")
        return self.client.command(command)
    
    def ping(self) -> bool:
        """Check if connection is alive"""
        try:
            if self.client:
                self.client.ping()
                return True
        except Exception:
            return False
        return False
    
    def init_schema(self) -> None:
        """Create the three response tables if they don't exist (and add any new columns)"""
        from src.models.tables import SCHEMA_STATEMENTS

        for statement in SCHEMA_STATEMENTS:
            self.execute_command(statement)
        print("✅ Response tables ready: event_responses, ai_info_responses, image_analysis_responses")

    def get_version(self) -> Optional[str]:
        """Get ClickHouse server version"""
        try:
            result = self.execute_query("SELECT version()")
            return result.result_rows[0][0] if result.result_rows else None
        except Exception:
            return None


# Global database instance
db = ClickHouseDatabase()


def get_db() -> ClickHouseDatabase:
    """Dependency to get database instance"""
    return db

