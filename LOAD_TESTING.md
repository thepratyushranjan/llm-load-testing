# Load Testing Guide — roboi LLM

How this project load-tests the vision LLM that serves roboi's **event**, **AI-info** and **image search** functions: what each part does, how a test runs, what is measured, and how to read the results.

**Target:** a ~70B vision model served by **vLLM on E2E Cloud**. During development the same tests run against a local model (Ollama `gemma4`). Only `.env` changes between the two.

**Goal:** measure **performance, scalability, throughput, latency and resource use** of the deployed model under realistic production requests.

---

## Contents
1. [The three functions under test](#1-the-three-functions-under-test)
2. [Architecture](#2-architecture)
3. [Test data](#3-test-data)
4. [Requests: exactly like production](#4-requests-exactly-like-production)
5. [Load profiles](#5-load-profiles)
6. [Running tests](#6-running-tests)
7. [Reliability: queue, retries, stop](#7-reliability-queue-retries-stop)
8. [Auto-stop](#8-auto-stop)
9. [What is measured](#9-what-is-measured)
10. [Monitoring and logs](#10-monitoring-and-logs)
11. [Results and export](#11-results-and-export)
12. [Correctness checks](#12-correctness-checks)
13. [Local model vs E2E](#13-local-model-vs-e2e)
14. [Settings reference](#14-settings-reference)
15. [Code map](#15-code-map)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. The three functions under test

| Function | Input | What the model does | Output |
|---|---|---|---|
| **event** | 3 images (+ video) + event JSON | Checks whether the media really shows what the event JSON claims (triggers, detections) | **Valid alert** or **false positive**, with a summary |
| **ai_info** | 3 images (+ video) + AI-info JSON | Audits the scene against the JSON and the office KPIs (crowding, PPE, hazards …) | KPI alerts by severity, with a classification |
| **extraction** (image search) | 1 image | Lists what is actually in the image (people, vehicles, colours, plates …) for storage in the DB | Detections with attributes |

- Prompts and response formats are **copied from roboi-main** and pinned to commit `e176e6e5` (`src/core/prompts.py`, `PROMPT_VERSION=e176e6e5-d9aefa7a`).
- Responses are formatted with the same code as production's `_format_response`, so the output shape is identical.
- Nothing from roboi-main is imported at runtime, and no production credentials, databases or queues are used.

---

## 2. Architecture

Everything runs in Docker Compose. Each machine runs its **own clone independently**; machines are never merged or synced.

```
 ./run.sh  or  ./lt run --profile ramp
        │
        ▼
 ┌────────────┐  every tick (5 s): pick N cases following the profile
 │ scheduler  │──────────────────────────────────────────┐
 └────────────┘                                          ▼
                                                  ┌─────────────┐
                                                  │ Redis queue │ lt:jobs
                                                  └─────────────┘
                                                         │
 ┌────────────┐  build request (images base64 + prompt   ▼
 │  worker    │  + JSON), stream it to the model, time it, validate the JSON
 └────────────┘──────────────► MODEL (E2E vLLM / local Ollama / mock)
        │ result
        ▼
 ┌────────────┐                ┌────────────┐
 │ ch-writer  │──────────────► │ ClickHouse │ ◄── metrics-poller (vLLM /metrics every few s)
 └────────────┘  batch insert  └────────────┘
                                      ▲
                       ./lt status · ./lt export · Grafana
```

| Service | Role |
|---|---|
| **scheduler** | Always on and idle until a run is submitted. Sends jobs at the profile's rate (open loop: it keeps the schedule even when the model is slow), drains at the end, runs auto-stop. |
| **worker** | Pulls jobs (up to `WORKER_CONCURRENCY` at once), calls the model with streaming, measures timings, parses and validates the output, handles retries and timeouts. |
| **ch-writer** | Writes results to ClickHouse in batches; never loses or duplicates a result, even across a ClickHouse restart. |
| **metrics-poller** | Reads vLLM's Prometheus `/metrics` (running/waiting requests, KV cache, prefix-cache hits, tokens/s, GPU) and tags each sample with the run id. |
| **redis** | Job queue, results stream, run state, control (run / pause / stop). |
| **clickhouse** | All results and metrics. |
| **grafana** | http://localhost:3000, with the ClickHouse plugin installed (no ready-made dashboard yet). |
| **api** | The project's FastAPI app (port 8000). |
| **dataset** *(tool)* | One-off commands: manifest, preencode, CLI (`./lt`). |
| **mock-vllm** *(optional)* | Fake GPU server for testing the system without a model. |

---

## 3. Test data

### 3.1 Point to your folder
Set one path in `.env`. It is scanned recursively and mounted **read-only**; nothing is copied or uploaded anywhere.
```
DATASET_DIR=/home/pratyush/Downloads/loadtest_functional
```

### 3.2 Folder layout
```
<DATASET_DIR>/
  trained/                       ← split (also accepted: train)
    extraction/                  ← optional: use these files only for image search
      cam1_0001.jpg
    events/
      event_001/                 ← a REAL EVENT: one folder per event
        payload.json             ←   the production event JSON, as-is ("type": "event")
        1.jpg  2.jpg  3.jpg      ←   the images that came with it
        clip.mp4                 ←   optional video
    ai_info/
      aiinfo_001/
        payload.json             ←   "type": "ai-info"
        1.jpg  2.jpg  3.jpg
  untrained/                     ← split (also accepted: val, test)
    ...
```

| Rule | Meaning |
|---|---|
| A folder containing `payload.json` | One **event case**: the JSON plus every image (sorted by name) and the first video in that folder, sent exactly as production does. `"type"` selects the function (`event` or `ai-info`). |
| Any other image or video | One case. Usable by every function unless it sits under a folder named `event`, `ai_info` or `extraction`. An image and a video with the same name become one case. |
| `trained` / `train` folder | Split = trained (images the model was trained on). |
| `untrained` / `val` / `test` folder | Split = untrained. Lets you compare trained vs unseen data. |
| Identical files | Skipped (content hash), so duplicates never inflate results. |

### 3.3 Where the event / AI-info JSON comes from
For each case, in this order:
1. **`payload.json`** in the event folder: real production data, used as-is. **Use this for realistic tests.**
2. `data/events/<case_id or image name>.json`: a real payload for one plain image.
3. `data/events/templates.jsonl`: real payloads, one per line, reused across cases.
4. **Synthetic**: generated in the production shape (same keys, random values). Fine for raw speed tests, but the JSON does not describe the image, so the model will mostly answer "false positive".

Check which source each case uses:
```
docker compose run --rm dataset python -m src.services.event_meta              # counts per source
docker compose run --rm dataset python -m src.services.event_meta --show <case_id> --function event
```

### 3.4 How many cases you need
You do **not** need new data for every request: the scheduler cycles through the cases. About **50–200 real events per function** is enough. With repeats, watch `prefix_cache_hit_rate` (see [§13](#13-local-model-vs-e2e)).

### 3.5 Prepare: manifest and cache
Run this once, and again whenever the folder changes (`./run.sh` does both automatically):
```
docker compose run --rm dataset python -m src.services.manifest    # scan → data/manifest.csv
docker compose run --rm dataset python -m src.services.preencode   # resize → data/cache/
```
- **manifest** hashes and probes every file, builds one row per case, skips duplicates and unreadable files, and prints a coverage check (every file accounted for).
- **preencode** resizes images the way Qwen-VL expects (multiples of 28 within `MAX_PIXELS`) and transcodes videos (ffmpeg). It runs once per file; the cache is reused and only new files are processed. Requests are built from the cache, so the test machine doesn't spend CPU on images during a run.

### 3.6 Functional test set (ready to use)
`/home/pratyush/Downloads/loadtest_functional` (41 MB, hard links to the original dataset):

| Folder | Cases | Expected answer |
|---|---|---|
| `*/events/valid_stranger_*` | 10 | Valid alert: strangers present in all 3 frames |
| `*/events/fp_no_person_*` | 5 | False positive: stranger claimed, nobody in the frames |
| `*/events/fp_fire_*` | 5 | False positive: fire claimed, none in the frames |
| `*/ai_info/ai_info_*` | 10 | Routine audit (no expected answer) |
| `*/extraction/*.jpg` | 40 | Image search; objects present are listed in `data/ground_truth.csv` |

The JSON detections come from the dataset's automatic labels, so the expected answers are a first check, not human-verified truth. Replace or extend them with real production events.

---

## 4. Requests: exactly like production

Each request is an OpenAI-compatible **streaming** chat completion:
```
system:  production system prompt for the function
user:    production user prompt with the event JSON filled in
         + images as inline base64 data URLs (+ video as video_url, or as frames)
```
- **No uploads:** media, prompt and JSON go inline in the request.
- **Streaming** with `stream_options.include_usage`, to measure time to first token and token counts.
- **Per-function parameters** match production: event temp 0.5, max 2048 tokens; ai_info temp 0.4, max 4096; extraction temp 0.2.
- **Optional guided JSON** (`GUIDED_JSON=true`): the model is forced to output the response schema (vLLM `response_format: json_schema`).
- **`EXTRA_BODY`**: extra JSON merged into every request (e.g. `{"reasoning_effort": "none"}` for Ollama gemma4).
- **Video:** `VIDEO_INPUT=video` sends the clip (vLLM). `VIDEO_INPUT=frames` sends `VIDEO_FRAMES` frames as images (for servers without video support, such as Ollama).

Measured prompt sizes (3 images): **ai_info ≈ 13,100 tokens**, event ≈ 5,200, extraction ≈ 750. vLLM must be started with **`--max-model-len` of at least 16384**.

Try a single request without any load:
```
docker compose run --rm dataset python -m src.services.vllm_client --function event -n 1 --show
docker compose run --rm dataset python -m src.services.vllm_client --function extraction --case <case_id> --show
```

---

## 5. Load profiles

One **tick** = `TICK_SECONDS` (5 s). Jobs per tick ÷ 5 = requests per second.

| Profile | Shape | Duration | Answers |
|---|---|---|---|
| **smoke** | 1/tick | 5 min | Does everything work end to end? Is the JSON valid? |
| **baseline** | 1/tick | 20 min | Latency with no contention |
| **ramp** | 1 → 5 → 10 → 20 → 35 /tick, 10 min each, cooldown between steps | ~54 min | Maximum sustainable throughput (where latency breaks) |
| **spike** | 2/tick 5 min → 20/tick 2 min → 2/tick 10 min | 17 min | Does the queue recover after a 10× burst? |
| **soak** | 10/tick (set `--rate` to ~70% of the ramp max) | 4 h | Memory leaks, slowdowns, stability |
| **mixed** | 5/tick, event 40% / ai_info 30% / extraction 30% | 30 min | Capacity planning at the production mix |

```
./lt profiles                     # list them
```

**Run length** = steps + cooldowns + a final **drain** (waits for in-flight requests to finish; at most `JOB_DEADLINE_S + REQUEST_TIMEOUT_S + 15 s`). Auto-stop or `./lt stop` ends a run early.

**Custom profile (YAML):**
```yaml
name: my-test
description: ramp to 15/tick, event only
function_mix: {event: 1.0}
split_mix: {trained: 0.5, untrained: 0.5}   # optional
cooldown_s: 30
steps:
  - {name: warm, duration_s: 300, batch_per_tick: 2}
  - {name: high, duration_s: 600, batch_per_tick: 15, drain_after: true}
  - {name: rps,  duration_s: 300, rps: 2.5}            # or a requests-per-second target
```
```
./lt profiles --file my-test.yaml       # validate
./lt run --file my-test.yaml --follow
```

---

## 6. Running tests

### 6.1 One command: `run.sh`
Starts everything, checks the model, prepares the data, then runs tests **round after round until you press Ctrl+C**.
```
./run.sh                                   # smoke, repeated until Ctrl+C
./run.sh baseline ramp spike               # these profiles in order, repeated
./run.sh --once ramp                       # run once, then exit
./run.sh ramp -- --rate-scale 0.1          # extra options passed to ./lt run
```
What it does:
1. **Checks:** `.env` exists, Docker is running, prints `MODEL` / `VLLM_URL` / `DATASET_DIR`.
2. **Services:** `docker compose up -d --wait` (until all are healthy).
3. **Model:** the server is reachable and `MODEL` is loaded; otherwise it exits with a clear error.
4. **Data:** manifest + preencode (only new files are processed).
5. **Rounds:** each profile with live progress, the results table, and an automatic export to `data/exports/<run_id>/`.

**Ctrl+C:** sending stops immediately, the queue is cleared, and the final status is printed. Services stay up for the next run (`docker compose down` stops them). All output is saved to `data/logs/run-<time>.log`.

### 6.2 Step by step: `./lt`
`./lt` is the control tool (it runs inside the `dataset` container).

| Command | What it does |
|---|---|
| `./lt run --profile smoke --follow` | Start a run and show progress until it ends |
| `./lt run ... --sample 50` | Use only 50 cases per function |
| `./lt run ... --functions event,ai_info` | Only these functions |
| `./lt run ... --scale 0.5` | Half the duration (`2` = double) |
| `./lt run ... --rate-scale 0.1` | 10% of the load (for a local GPU) |
| `./lt run ... --rate 4` | Fixed **jobs per tick** for every step (4/tick = 0.8 req/s) |
| `./lt run ... --sla-p95-ms 8000` | Auto-stop when p95 latency exceeds 8 s |
| `./lt run ... --max-error-rate 0.05 --queue-growth-s 180 --no-auto-stop` | Tune or disable auto-stop |
| `./lt run ... --notes "vLLM 0.6, TP=4"` | Note stored with the run |
| `./lt run ... --force` | Replace a run that is still active |
| `./lt status` / `./lt status --watch` | Progress, queue, worker health, last 60 s, per-function table |
| `./lt pause` / `./lt resume` | Hold or continue sending (in-flight requests finish) |
| `./lt stop` | **Kill switch:** stop sending, cancel in-flight requests, clear the queue |
| `./lt stop --keep-queue` | Stop without clearing the queue |
| `./lt export [--run-id ID]` | Export a run to Parquet |
| `./lt profiles [--file F]` | List or validate profiles |

### 6.3 Recommended order
```
1. smoke      must be 100% ok and schema_ok before anything else
2. baseline   latency at idle: the reference numbers
3. ramp       finds the maximum throughput (auto-stops when overloaded)
4. spike      recovery after a burst
5. mixed      production mix
6. soak       --rate ≈ 70% of the ramp maximum, 4 h
```

---

## 7. Reliability: queue, retries, stop

| Feature | Behaviour |
|---|---|
| **Open-loop schedule** | Ticks are aligned to absolute time, so a slow model never slows the sending rate (like real traffic). Tick lateness is measured; it stayed ≤ 3 ms in tests. |
| **Job deadline** | A job not finished within `JOB_DEADLINE_S` of creation is marked `expired`. |
| **Retries** | Errors, timeouts and 429s are retried up to `MAX_RETRIES` with exponential backoff (or the server's `Retry-After`), using a delayed queue. |
| **Dead-letter queue** | Jobs that keep failing, or are handed out `MAX_DELIVERIES` times (worker crashes), go to `lt:dlq` and are recorded as `dlq`. |
| **Crash recovery** | Jobs held by a dead worker are reclaimed after `CLAIM_IDLE_S`. |
| **Exactly-once results** | Each result is published once per attempt (idempotency key), and ClickHouse inserts use dedup tokens, so restarts never double-count. |
| **Rate cap** | `MAX_RPS` / `RATE_BURST`: an optional safety limit (token bucket). |
| **Pause / stop** | Control state in Redis; workers react within ~0.2 s. Stop cancels in-flight requests (tested: 16 → 0 in under 1 s). |
| **Healthchecks** | Every service writes a heartbeat; Docker marks it unhealthy if the heartbeat stops. |

---

## 8. Auto-stop

The scheduler checks every 10 s and **stops the run itself** when:

| Condition | Default | Meaning |
|---|---|---|
| **Backlog keeps growing** | 120 s (`QUEUE_GROWTH_WINDOW_S`) | Redis waiting + vLLM `num_requests_waiting` rose at **every** check for the whole window: the model can't keep up |
| **Error rate** | > 2% in the last 60 s (`MAX_ERROR_RATE`) | Too many failures |
| **Latency SLA** | off (`SLA_P95_MS=0`) | p95 end-to-end latency above the SLA (set it once the SLA is agreed) |

The reason is logged (`⛔ Auto-stop: ...`) and stored with the run. In a **ramp**, the step where auto-stop fires marks the capacity limit.

---

## 9. What is measured

### Per request (`lt_requests`)
| Metric | Meaning |
|---|---|
| `queue_wait_ms` | Created → picked up by a worker (test-side queueing) |
| `ttft_ms` | Sent → **first token** (includes prefill of images + prompt; reasoning tokens count) |
| `model_latency_ms` | Sent → last token (pure model time) |
| `e2e_ms` | Created → done (what a user would wait) |
| `writer_lag_ms` | Done → stored |
| `prompt_tokens`, `completion_tokens`, `cached_tokens` | From the server's usage report |
| `n_images`, `n_videos`, `media_bytes`, `video_duration_s`, `num_frames` | Request size |
| `status` | `ok`, `error`, `timeout`, `rate_limited`, `expired`, `dlq` |
| `http_status`, `error_type` | Failure detail |
| `json_parse_ok`, `schema_valid` | Was the output valid JSON in the production shape? |

### Per response (`lt_responses`)
The raw text, parsed JSON and key fields: `alert_valid` (event verdict / AI-info flagged), `labels` (detected objects or flagged KPIs), `plate_number`, `severity`. Image search output is stored here, in the load tester's own ClickHouse, never in production.

### Server side (`lt_server_metrics`, vLLM only)
`requests_running`, `requests_waiting`, `kv_cache_usage`, `prefix_cache_hit_rate`, `prompt_tokens_per_s`, `generation_tokens_per_s`, `gpu_util`, `gpu_mem_used_bytes`.

### Per minute (`lt_requests_1m`)
A materialized rollup with counts and latency quantiles per minute, function and status, for fast dashboards.

### Test machine health
Worker CPU %, in-flight count and **event-loop lag**, shown in `./lt status`. If the lag is high, the test machine itself is the bottleneck and its numbers can't be trusted.

---

## 10. Monitoring and logs

Use three terminals while a test runs:

**1 — Live status**
```
./lt status --watch
```
| Field | Healthy | Problem |
|---|---|---|
| `jobs_waiting` | small and steady | keeps growing → model can't keep up |
| `failed`, `dlq` | 0 | rising → errors or timeouts |
| `p95_e2e_ms` | steady | climbing step after step → overloaded |
| `json_ok_pct`, `schema_ok_pct` | 100 | below 100 → model output broken |
| `loop lag` | < 50 ms | high → the test machine is the bottleneck |

**2 — Service logs**
```
docker compose logs -f scheduler worker ch-writer
docker compose logs -f worker | grep -E "WARNING|ERROR"
```
| Log line | Meaning |
|---|---|
| `[scheduler] ▶ Run ...` | Run started |
| `[scheduler] Step 2 '10/tick' ...` | Load increased |
| `[worker] cpu=6% inflight=12 done=430 loop_lag=1ms` | Worker health |
| `[ch-writer] Wrote 25 results (oldest 2.1s old)` | Results saved |
| `[scheduler] ⛔ Auto-stop: ...` | Stopped itself, with the reason |
| `[metrics-poller] Metrics unavailable ... 404` | Normal with Ollama (it has no `/metrics`) |

**3 — GPU**
```
watch -n 1 nvidia-smi          # on the GPU machine (local PC or E2E server)
journalctl -u ollama -f        # local Ollama only
```

**Grafana:** http://localhost:3000 (admin / `GRAFANA_ADMIN_PASSWORD`). The ClickHouse plugin is installed; add a data source (server `clickhouse`, port `8123`, user `default`) and chart the queries from [§11](#11-results-and-export). A ready-made dashboard is not built yet.

---

## 11. Results and export

```
./lt status                    # final table per step and function: rows, ok, p50, p95, TTFT, schema_ok
./lt export                    # last run → data/exports/<run_id>/{lt_runs,lt_requests,lt_responses,lt_server_metrics}.parquet
```

Example ClickHouse queries:
```
docker compose exec clickhouse clickhouse-client
```
```sql
-- latency and errors per function for the latest run
SELECT function, count() AS n, countIf(status != 'ok') AS failed,
       round(quantile(0.5)(e2e_ms)) AS p50_ms, round(quantile(0.95)(e2e_ms)) AS p95_ms,
       round(quantile(0.95)(ttft_ms)) AS p95_ttft_ms, round(avg(schema_valid) * 100, 1) AS schema_ok_pct
FROM lt_requests
WHERE run_id = (SELECT max(run_id) FROM lt_requests)
GROUP BY function;

-- throughput and latency per ramp step (find where it breaks)
SELECT step, count() / (dateDiff('second', min(created_ts), max(created_ts)) + 1) AS rps,
       round(quantile(0.95)(e2e_ms)) AS p95_ms, countIf(status != 'ok') AS failed
FROM lt_requests WHERE run_id = '<run_id>' GROUP BY step ORDER BY step;

-- GPU server state during the run (vLLM)
SELECT toStartOfMinute(ts) AS m, avg(requests_running), max(requests_waiting),
       max(kv_cache_usage), avg(generation_tokens_per_s), avg(prefix_cache_hit_rate)
FROM lt_server_metrics WHERE run_id = '<run_id>' GROUP BY m ORDER BY m;
```

**Reading a ramp:** the capacity is the **highest step where p95 stays flat, errors stay at 0 and `jobs_waiting` doesn't grow**. Use ~70% of it for the soak test and for production planning.

---

## 12. Correctness checks

Load testing is about speed, but every response is also checked:
- **Valid JSON** (`json_parse_ok`) and **production schema** (`schema_valid`): always on.
- **Verdict vs expected answer:** write `data/ground_truth.csv`:
  ```
  image_id,function,alert_valid,labels
  event_001,event,true,
  event_002,event,false,
  cam1_0001,extraction,,person|chair|bottle
  ```
  `image_id` = event folder name, image file name or case id. The model's verdicts are in `lt_responses.alert_valid` / `labels`, compared per case.
  ```
  docker compose run --rm dataset python -m src.services.ground_truth     # how many cases are labelled
  ```
- **Trained vs untrained:** every result carries its split, so quality and speed can be compared on seen vs unseen images.

---

## 13. Local model vs E2E

| | Local (development) | E2E (real test) |
|---|---|---|
| Purpose | Check the system and data work | Real performance numbers |
| Model | Ollama `gemma4-16k` (16K context), RTX 4070 | ~70B **vision** model (e.g. Qwen2.5-VL-72B) on vLLM |
| `.env` | `VLLM_URL=http://host.docker.internal:11434/v1`<br>`MODEL=gemma4-16k:latest`<br>`VIDEO_INPUT=frames`<br>`EXTRA_BODY='{"reasoning_effort": "none"}'`<br>`VLLM_METRICS_URL=` | `VLLM_URL=https://<e2e-host>/v1`<br>`VLLM_API_KEY=...`<br>`MODEL=<served model name>`<br>`VIDEO_INPUT=video`<br>`EXTRA_BODY=`<br>`VLLM_METRICS_URL=https://<e2e-host>/metrics` |
| Load | small: `--rate-scale 0.1` | full profiles |
| Server metrics | none (Ollama has no `/metrics`) | yes |

**Local notes**
- Ollama runs on the **host**, not in Docker; containers reach it via `host.docker.internal`.
- Stock `gemma4` has a 4K context and silently cuts off long prompts. `gemma4-16k` is a variant with a 16K context (`ollama rm gemma4-16k` removes it). To recreate it:
  `curl localhost:11434/api/create -d '{"model":"gemma4-16k","from":"gemma4:latest","parameters":{"num_ctx":16384}}'`

**E2E vLLM checklist**
- A **vision** model (text-only 70B models such as Llama 3 70B can't see images).
- `--max-model-len 16384` or more (ai_info prompts are ~13K tokens).
- `--max-num-seqs` sized for the GPUs; keep `WORKER_CONCURRENCY` ≥ 1.5 × `--max-num-seqs`.
- `/metrics` reachable from the test machine.
- For honest numbers with repeated test data, run the main tests with **`--no-enable-prefix-caching`** (or check `prefix_cache_hit_rate`).

**Without a GPU (mock server)** — to test the load tester itself:
```
docker compose --profile mock up -d mock-vllm
# .env: VLLM_URL=http://mock-vllm:8000/v1, VLLM_METRICS_URL=http://mock-vllm:8000/metrics
```
The mock simulates batching limits, prefill/decode speed, errors, 429s, hangs and invalid JSON (`MOCK_*` settings).

---

## 14. Settings reference

All in `.env` (see `.env.example`). Main ones:

| Setting | Default | Meaning |
|---|---|---|
| `VLLM_URL`, `VLLM_API_KEY`, `MODEL` | | Model server and model name |
| `VLLM_METRICS_URL` | | vLLM `/metrics` (empty = derived from `VLLM_URL`) |
| `DATASET_DIR` | | Your test data folder (read-only) |
| `MACHINE_ID`, `MACHINES` | `A`, `A` | This machine (each clone runs independently) |
| `FUNCTIONS` | `event,ai_info,extraction` | Functions to test |
| `TICK_SECONDS` | `5` | Scheduler tick |
| `JOB_DEADLINE_S` | `30` | Max time from creation to done |
| `REQUEST_TIMEOUT_S` | `30` | Max time for one model call |
| `WORKER_CONCURRENCY` | `64` | Parallel requests per worker |
| `MAX_RETRIES`, `MAX_DELIVERIES`, `RETRY_BACKOFF_S` | `2`, `3`, `1.0` | Retry policy |
| `MAX_RPS`, `RATE_BURST` | `0` | Optional rate cap |
| `MAX_PIXELS` | `1003520` | Image resolution sent to the model |
| `IMAGE_VARIANTS_PX`, `VIDEO_VARIANTS_SIDE` | | Cached sizes |
| `VIDEO_INPUT`, `VIDEO_FRAMES`, `VIDEO_SIDE` | `video`, `4`, `1280` | How video is sent |
| `VIDEO_MAX_SECONDS`, `VIDEO_FPS`, `JPEG_QUALITY` | `10`, `0`, `90` | Media preparation |
| `GUIDED_JSON` | `false` | Force JSON schema output (vLLM) |
| `EXTRA_BODY` | | Extra JSON in every request |
| `AUTO_STOP`, `SLA_P95_MS`, `MAX_ERROR_RATE`, `QUEUE_GROWTH_WINDOW_S` | `true`, `0`, `0.02`, `120` | Auto-stop |
| `GRAFANA_ADMIN_PASSWORD` | `admin` | Grafana login |

For a slow local GPU, raise `JOB_DEADLINE_S` and `REQUEST_TIMEOUT_S` (e.g. 120).

---

## 15. Code map

```
run.sh                       one command: start → check → prepare → run until Ctrl+C
lt                           control tool (./lt run | status | pause | resume | stop | export | profiles)
docker-compose.yml           all services
src/
  main.py, api/, database.py FastAPI app and ClickHouse client
  redis_client.py            Redis connection
  core/
    config.py                settings (.env)
    profiles.py              load profiles, run spec
    prompts.py               production prompts (pinned to roboi-main e176e6e5)
    keys.py                  Redis key names
    logging.py, healthcheck.py
  models/
    tables.py                ClickHouse tables and rollup
    job.py                   Job / Result models
    dataset.py               manifest row, cache entry, event metadata, ground truth
  schemas/responses.py       production response schemas (event, ai-info, extraction)
  services/
    manifest.py              scan DATASET_DIR → data/manifest.csv (images, videos, event folders)
    preencode.py             resize / transcode → data/cache
    event_meta.py            event / AI-info JSON per case (payload.json → templates → synthetic)
    ground_truth.py          expected answers (data/ground_truth.csv)
    job_builder.py           case → job (media + JSON)
    task_event.py, task_ai_info.py, task_extraction.py, task_base.py, tasks.py
                             prompt building, parsing, production formatting, validation
    vllm_client.py           streaming OpenAI-compatible client (+ single-request try-out CLI)
    job_queue.py             Redis Streams queue, retries, DLQ, idempotency, control
    scheduler.py             open-loop scheduler, drain, auto-stop
    worker.py                concurrent request execution
    ch_writer.py             batched, deduplicated ClickHouse writes
    metrics_poller.py        vLLM Prometheus metrics
    mock_vllm.py             fake GPU server
    prompt_snapshot.py       dev tool: re-render prompts from a roboi-main checkout
    cli.py                   ./lt commands
data/                        (not in git) manifest, cache, exports, logs, ground_truth.csv, events/
```

---

## 16. Troubleshooting

| Problem | Fix |
|---|---|
| `run.sh`: model not reachable | Start Ollama / check the E2E URL and key; `curl <VLLM_URL>/models` |
| `MODEL=... not served` | Use one of the names `run.sh` prints |
| `schema_ok_pct` low, outputs cut off | Context too small: use `gemma4-16k` locally or raise `--max-model-len` on vLLM |
| Many `expired` / `timeout` locally | The GPU is overloaded: use `--rate-scale 0.1`, raise `JOB_DEADLINE_S` / `REQUEST_TIMEOUT_S` |
| `run ... is still active` | `./lt stop`, or `./lt run ... --force` |
| `no manifest` / `not in cache` | Run manifest and preencode (or `./run.sh`) |
| Event cases use `synthetic` JSON | Add `payload.json` event folders (§3) |
| `metrics-poller` 404 | Normal with Ollama |
| Service unhealthy | `docker compose ps`, `docker compose logs <service>` |
| Changed code or `.env` not applied | `docker compose up -d --build --force-recreate` |
