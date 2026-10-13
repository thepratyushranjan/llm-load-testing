# LLM Load Testing

Load testing and performance benchmarking for roboi's vision LLM (**event**, **AI-info** and **image search** functions), served by **vLLM on E2E Cloud**. It measures throughput, latency, scalability and resource use with production-identical requests.

📖 **Full guide to every feature:** [LOAD_TESTING.md](LOAD_TESTING.md)

## Architecture

- **FastAPI**: web framework for the project API
- **Redis**: job queue (Redis Streams), run control, live load-test numbers
- **ClickHouse** (24.9): the 3 response tables (event, AI-info, image analysis)
- **Grafana**: dashboards
- **Docker Compose**: all services: `scheduler`, `worker`, `ch-writer`, `metrics-poller`, `api`, `redis`, `clickhouse`, `grafana`
- **Model under test**: any OpenAI-compatible server: E2E vLLM, local Ollama, or the built-in mock

```
./run.sh → scheduler → Redis queue → worker → MODEL → ch-writer → ClickHouse → ./lt status / export / Grafana
```

## Project Structure

```
llm-load-testing/
├── run.sh                 # One command: start, check, prepare, run tests until Ctrl+C
├── lt                     # Control tool: ./lt run | status | pause | resume | stop | export | profiles
├── LOAD_TESTING.md        # Full load testing guide
├── src/
│   ├── main.py            # FastAPI application
│   ├── database.py        # ClickHouse connection handler
│   ├── redis_client.py    # Redis connection
│   ├── api/               # API routes
│   ├── core/              # Settings, load profiles, production prompts, logging
│   ├── models/            # ClickHouse tables, job and dataset models
│   ├── schemas/           # API and production response schemas
│   └── services/          # Scheduler, worker, writer, metrics poller, dataset tools, CLI
├── data/                  # (not in git) manifest, image cache, exports, logs
├── Dockerfile             # Container image (Python + ffmpeg)
├── docker-compose.yml     # Multi-container orchestration
└── requirements.txt       # Python dependencies
```

## Prerequisites

- Docker and Docker Compose
- A model server: E2E vLLM URL, **or** local [Ollama](https://ollama.com) with a vision model, **or** the built-in mock
- A folder of test images (and real event folders, see below)

## Quick Start: Load Testing

### 1. Configure

```bash
cp .env.example .env
```

Set at least:

```bash
# Model under test, E2E vLLM:
VLLM_URL=https://<e2e-host>/v1
VLLM_API_KEY=<key>
MODEL=<served model name>

# …or local Ollama (running on the host, not in Docker):
# VLLM_URL=http://host.docker.internal:11434/v1
# MODEL=gemma4-16k:latest
# VIDEO_INPUT=frames
# EXTRA_BODY='{"reasoning_effort": "none"}'

# Your test data folder (read-only, never copied or uploaded)
DATASET_DIR=/path/to/test_data
```

### 2. Prepare test data

```
test_data/
  trained/                     # or train/  → split "trained"
    extraction/*.jpg           # image search images
    events/event_001/          # one real event per folder:
      payload.json             #   production event JSON as-is ("type": "event" or "ai-info")
      1.jpg 2.jpg 3.jpg        #   its images (+ optional video)
  untrained/                   # or val/ test/  → split "untrained"
    ...
```

A plain folder of images also works. See [LOAD_TESTING.md §3](LOAD_TESTING.md#3-test-data).

### 3. Run

```bash
./run.sh                       # the test set by PROFILE in .env, repeated until Ctrl+C
```

That one command:
1. checks `.env` and Docker
2. starts all services and waits until they are healthy
3. checks the model server is up and `MODEL` is loaded
4. scans the dataset and prepares the image cache
5. shows how many event / AI-info cases use your `payload.json` (warns about cases without one)
6. runs the test round after round, prints results, exports each run to `data/exports/`

Press **Ctrl+C** to stop: sending stops at once and the queue is cleared. Output is saved to `data/logs/`.

More examples:

```bash
./run.sh baseline ramp spike           # several profiles in order
./run.sh --once ramp                   # run once and exit
./run.sh ramp -- --rate-scale 0.1      # lighter load (local GPU)
```

### 4. Monitor (other terminals)

```bash
./lt status --watch                            # live progress, latency, errors, queue
docker compose logs -f scheduler worker        # service logs
watch -n 1 nvidia-smi                          # GPU
```

### 5. Results

```bash
./lt status                    # per-function table: p50 / p95 latency, TTFT, errors, schema validity
./lt export                    # Parquet files in data/exports/<run_id>/
```

## Load Profiles

| Profile | Load | Duration | Purpose |
|---|---|---|---|
| `smoke` | 1 job / 5 s | 5 min | Everything works, JSON valid |
| `baseline` | 1 job / 5 s | 20 min | Latency with no contention |
| `ramp` | 1 → 5 → 10 → 20 → 35 jobs / 5 s | ~54 min | Maximum throughput (auto-stops when overloaded) |
| `spike` | 2 → 20 → 2 jobs / 5 s | 17 min | Recovery after a burst |
| `mixed` | 5 jobs / 5 s, production mix | 30 min | Capacity planning |
| `soak` | steady | 4 h | Stability and leaks |

## Control Commands (`./lt`)

```bash
./lt run --profile ramp --follow        # start a run (options: --sample N, --functions, --scale, --rate-scale, --rate, --notes)
./lt status [--watch]                   # progress and results
./lt pause | ./lt resume                # hold / continue sending
./lt stop                               # kill switch: stop now and clear the queue
./lt export [--run-id ID]               # export to Parquet
./lt profiles [--file my.yaml]          # list or validate profiles
```

Single request without load (to check the model and prompts):

```bash
docker compose run --rm dataset python -m src.services.vllm_client --function event -n 1 --show
```

## Running Services Manually

### 1. Start all services

```bash
docker compose up -d --build
```

This will start:
- ClickHouse on ports 8123 (HTTP) and 9000 (Native)
- FastAPI application on port 8000
- Redis on 127.0.0.1:6379, Grafana on http://localhost:3000
- scheduler, worker, ch-writer, metrics-poller

Optional mock model server (no GPU needed): `docker compose --profile mock up -d mock-vllm` with `VLLM_URL=http://mock-vllm:8000/v1`.

### 2. Prepare the dataset

```bash
docker compose run --rm dataset python -m src.services.manifest     # scan DATASET_DIR
docker compose run --rm dataset python -m src.services.preencode    # resize images into the cache
```

### 3. Check services status

```bash
docker compose ps
```

### 4. Stop all services

```bash
docker compose down
```

### 5. Stop and remove volumes

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
docker compose logs -f worker
docker compose logs -f clickhouse
```

### Apply code or .env changes
```bash
docker compose build                  # one image (llm-load-testing:latest) used by every service and ./lt
docker compose up -d --force-recreate
```

## Environment Variables

Main settings (all of them: `.env.example` and [LOAD_TESTING.md §14](LOAD_TESTING.md#14-settings-reference)):

| Variable | Default | Description |
|----------|---------|-------------|
| CLICKHOUSE_HOST | clickhouse | ClickHouse hostname |
| CLICKHOUSE_PORT | 8123 | ClickHouse HTTP port |
| CLICKHOUSE_USER | default | ClickHouse username |
| CLICKHOUSE_PASSWORD | (empty) | ClickHouse password |
| REDIS_URL | redis://redis:6379/0 | Redis connection |
| VLLM_URL | | Model server (OpenAI-compatible `/v1`) |
| VLLM_API_KEY | (empty) | Model server API key |
| VLLM_METRICS_URL | (derived) | vLLM `/metrics` endpoint |
| MODEL | | Model name to test |
| DATASET_DIR | | Test data folder |
| MACHINE_ID | A | This machine's id |
| WORKER_CONCURRENCY | 64 | Parallel requests per worker |
| JOB_DEADLINE_S / REQUEST_TIMEOUT_S | 30 / 30 | Time limits (raise for a slow local GPU) |
| AUTO_STOP / MAX_ERROR_RATE / SLA_P95_MS | true / 0.02 / 0 | Auto-stop rules |

## ClickHouse Access

### HTTP Interface
```bash
curl 'http://localhost:8123/?query=SELECT%201'
```

### CLI Access
```bash
docker exec -it clickhouse-db clickhouse-client
```

ClickHouse has only these three tables. Each row also holds that request's speed (`ttft_ms`, `model_latency_ms`, `e2e_ms`, tokens). Failed requests and live numbers are kept in Redis (`./lt status`).


| Table | Holds |
|---|---|
| `event_responses` | Valid event responses: verdict (valid alert / false positive), summary, people count … |
| `ai_info_responses` | Valid AI-info responses: flagged KPIs, counts, scores, summary … |
| `image_analysis_responses` | Valid image search responses: counts, objects found, plates, attributes |

## License

MIT
