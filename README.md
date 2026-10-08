# LLM Load Testing

FastAPI application with ClickHouse database integration for load testing LLM applications.

## Architecture

- **FastAPI**: Web framework for building APIs
- **Uvicorn**: ASGI server
- **ClickHouse**: Column-oriented database (version 24.9)
- **Docker**: Containerization

## Project Structure

```
llm-load-testing/
├── src/
│   ├── __init__.py
│   ├── main.py          # FastAPI application
│   └── database.py      # ClickHouse connection handler
├── Dockerfile           # API container configuration
├── docker-compose.yml   # Multi-container orchestration
└── requirements.txt     # Python dependencies
```

## Prerequisites

- Docker
- Docker Compose

## Quick Start

### 1. Start all services

```bash
docker compose up -d --build
```

This will start:
- ClickHouse database on ports 8123 (HTTP) and 9000 (Native)
- FastAPI application on port 8000

### 2. Check services status

```bash
docker compose ps
```

### 3. Stop all services

```bash
docker compose down
```

### 4. Stop and remove volumes

```bash
docker compose down -v
```

## API Endpoints

### Base Endpoints

- `GET /` - Root endpoint
- `GET /health` - Health check with database status
- `GET /docs` - Swagger UI documentation
- `GET /redoc` - ReDoc documentation

### Testing Endpoints

- `GET /api/v1/test` - Test endpoint for load testing
- `GET /api/v1/db/test` - Test ClickHouse connection

### Database Endpoints

- `POST /api/v1/db/query` - Execute custom ClickHouse queries

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/db/query \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT version() as version"}'
```

## Testing the Setup

### Test API health
```bash
curl http://localhost:8000/health
```

### Test ClickHouse connection
```bash
curl http://localhost:8000/api/v1/db/test
```

### Execute custom query
```bash
curl -X POST http://localhost:8000/api/v1/db/query \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT 1 as number, '\''Hello'\'' as message"}'
```

## Development

### Build only the API
```bash
docker build -t llm-load-testing .
```

### Run API in development mode
```bash
docker run --rm -p 8000:8000 \
  -e CLICKHOUSE_HOST=clickhouse \
  llm-load-testing
```

### View logs
```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f api
docker compose logs -f clickhouse
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| CLICKHOUSE_HOST | clickhouse | ClickHouse hostname |
| CLICKHOUSE_PORT | 8123 | ClickHouse HTTP port |
| CLICKHOUSE_USER | default | ClickHouse username |
| CLICKHOUSE_PASSWORD | (empty) | ClickHouse password |

## ClickHouse Access

### HTTP Interface
```bash
curl 'http://localhost:8123/?query=SELECT%201'
```

### CLI Access
```bash
docker exec -it clickhouse-db clickhouse-client
```

## License

MIT
Load testing and performance benchmarking framework for LLM APIs and AI inference services.
