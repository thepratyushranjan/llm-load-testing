# Load Testing Guide — roboi LLM

How this project load-tests the vision LLM that serves roboi's **event**, **AI-info** and **image search** functions: what each part does, how a test runs, what is measured, and how to read the results.

**Target:** a vision model (currently `Qwen/Qwen3.6-35B-A3B-FP8`) served by **vLLM on an E2E TIR inference endpoint**, called through its OpenAI-compatible API with the `openai` SDK.

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
13. [E2E endpoint setup](#13-e2e-endpoint-setup)
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
 └────────────┘──────────────► MODEL (E2E TIR vLLM endpoint)
        │ result
        ▼
 ┌────────────┐                ┌────────────┐
 │ ch-writer  │──────────────► │ ClickHouse │  3 tables: event / ai_info / image_analysis responses
 └────────────┘  batch insert  └────────────┘
                                      ▲
                       ./lt status · ./lt export · Grafana
```

| Service | Role |
|---|---|
| **scheduler** | Always on and idle until a run is submitted. Sends jobs at the profile's rate (open loop: it keeps the schedule even when the model is slow), drains at the end, runs auto-stop. |
| **worker** | Pulls jobs (up to `WORKER_CONCURRENCY` at once), calls the model with streaming, measures timings, parses and validates the output, handles retries and timeouts. |
| **ch-writer** | Writes valid responses (with their speed) to the 3 ClickHouse tables in batches; never loses or duplicates one, even across a ClickHouse restart. |
| **metrics-poller** | Reads vLLM's Prometheus `/metrics` (running/waiting requests, KV cache, prefix-cache hits, tokens/s) into Redis for auto-stop and `./lt status`. |
| **redis** | Job queue, results stream, run state and live stats, vLLM metrics, control (run / pause / stop). |
| **clickhouse** | The 3 response tables (event, AI-info, image analysis). |
| **grafana** | http://localhost:3000, with the ClickHouse plugin installed (no ready-made dashboard yet). |
| **api** | The project's FastAPI app (port 8000). |
| **dataset** *(tool)* | One-off commands: manifest, preencode, CLI (`./lt`). |

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
You do **not** need new data for every request: the scheduler cycles through the cases. About **50–200 real events per function** is enough. Repeats don't hit vLLM's prefix cache: each job gets its own `cache_salt` (`ISOLATE_PREFIX_CACHE`, see [§13](#13-e2e-endpoint-setup)).

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
         + images as inline base64 data URLs (+ video as video_url)
```
- **No uploads:** media, prompt and JSON go inline in the request.
- **Streaming** with `stream_options.include_usage`, to measure time to first token and token counts.
- **Per-function parameters** match production: event temp 0.5, max 2048 tokens; ai_info temp 0.4, max 4096; extraction temp 0.2, max 2048, `presence_penalty` 1.0 (without it Qwen3.6 repeats the same detection, e.g. `chair` 170×, on crowded images until the token limit).
- **Optional guided JSON** (`GUIDED_JSON=true`): the model is forced to output the response schema (vLLM `response_format: json_schema`).
- **`EXTRA_BODY`**: extra JSON merged into every request, e.g. `{"chat_template_kwargs": {"enable_thinking": false}}` to turn Qwen3 / Qwen3.6 thinking off.
- **Images are the main focus.** Video is optional: a case with a video sends it as a `video_url` part (Qwen2.5-VL / Qwen3.6 accept video).
- **Prefix cache isolation** (`ISOLATE_PREFIX_CACHE=true`, default): each job carries a unique vLLM `cache_salt`, so the repeated test images don't hit the server's prefix cache (production images are unique). Retries of a job reuse its salt.

Measured prompt sizes on Qwen3.6-35B-A3B: **ai_info ≈ 14,400 tokens** (3 images), event ≈ 5,700 (3 images), extraction ≈ 1,400 (1 image). Prompt + max output must fit, so vLLM needs **`--max-model-len` of at least 20480** (ai_info: 14.4K + 4,096).

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
./run.sh                                   # PROFILE from .env (e.g. PROFILE=ramp), repeated until Ctrl+C
./run.sh baseline ramp spike               # these profiles in order, repeated
./run.sh --once ramp                       # run once, then exit
./run.sh ramp -- --rate-scale 0.1          # extra options passed to ./lt run
```
What it does:
1. **Checks:** `.env` exists, Docker is running, prints `MODEL` / `VLLM_URL` / `DATASET_DIR`.
2. **Services:** `docker compose up -d --wait` (until all are healthy).
3. **Model:** the server is reachable and `MODEL` is loaded; otherwise it exits with a clear error.
4. **Data:** manifest + preencode (only new files are processed). Stops with a clear error if either fails.
5. **JSON check:** how many event / AI-info cases use your `payload.json`, with a warning for cases that would use generated JSON.
6. **Rounds:** each profile with live progress, the results table, and an automatic export to `data/exports/<run_id>/`.

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

ClickHouse holds **only three tables**: `event_responses`, `ai_info_responses` and `image_analysis_responses`. Live numbers for every request (including failures) are kept in **Redis** while a test runs.

### Per request (Redis, live)
Every request attempt adds one entry to the run's live stats (`lt:run:stats:<run_id>`, kept 7 days). `./lt status`, auto-stop and the end-of-run table are computed from it.

| Metric | Meaning |
|---|---|
| `ttft` | Sent → **first token** (includes prefill of images + prompt; reasoning tokens count) |
| `e2e` | Scheduled → done (what a user would wait) |
| `status` | `ok`, `error`, `timeout`, `rate_limited`, `expired`, `dlq` |
| `json_ok`, `schema_ok` | Was the output valid JSON in the production shape? |

### Model responses: one table per function
Only **valid** responses are stored, meaning correct JSON in the production format after the same `_format_response` as production. Invalid replies and failed requests are not stored in ClickHouse; they are counted in the live stats (`json_ok` / `schema_ok`, `failed`).

Every response table has:
- **Identity:** `run_id`, `machine_id`, `job_id`, `attempt`, `model`, `case_id`, `split`, `prompt_version`, `inserted_ts`
- **Speed of that request:** `created_ts`, `step` (load level), `ttft_ms` (sent → first token), `model_latency_ms` (sent → last token), `e2e_ms` (scheduled → done), `prompt_tokens`, `completion_tokens`, `n_images`
- **The response:** the columns below, plus `response_json` (the full production-format response)

| Table | Function | Main columns |
|---|---|---|
| **`event_responses`** | event | `alert_valid` (1 = valid alert, 0 = false positive), `verdict`, `validation_summary`, `cam_name`, `claimed_triggers`, `claimed_people_count`, `ai_people_count`, `what_happened`, `why_it_happened`, `recommendation`, `roi_labels`, `media_analyzed` |
| **`ai_info_responses`** | ai_info | `status`, `utilization`, `critical_alerts` / `high_alerts` / `medium_alerts` / `low_alerts` (the KPIs flagged), `total_issues`, `people_count`, `employee_count`, `visitor_count`, `contractor_count`, `unidentified_count`, `safety_score`, `cleanliness_score`, `uniform_compliance_score`, `access_compliance_score`, `overall_compliance_score`, `overall_compliance_pct`, `zone_type`, `occupancy_level`, `triggers`, `ai_summary`, `cam_name`, `site_name`, `media_analyzed` |
| **`image_analysis_responses`** | extraction (image search) | `person_count`, `vehicle_count`, `dog_count`, `labels` (objects found), `plate_numbers`, `detections` (JSON: label + attributes per object) |

These live in the load tester's own ClickHouse, never in production.

```sql
SELECT verdict, count() FROM event_responses WHERE run_id = '<run_id>' GROUP BY verdict;
SELECT status, avg(people_count), avg(overall_compliance_pct) FROM ai_info_responses GROUP BY status;
SELECT arrayJoin(labels) AS object, count() FROM image_analysis_responses GROUP BY object ORDER BY count() DESC;
```

### Server side (Redis, vLLM only)
The latest vLLM `/metrics` sample (`lt:server`), shown by `./lt status` and used by auto-stop: `requests_running`, `requests_waiting`, `kv_cache_usage`, `prefix_cache_hit_rate`, `prompt_tokens_per_s`, `generation_tokens_per_s`.

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
| `[metrics-poller] Metrics unavailable ... 503` | Endpoint stopped or still starting; retried, warned once a minute |
| `[metrics-poller] Server metrics off: ... HTTP 404` | The endpoint doesn't expose `/metrics`; the poller stops, client-side timings still work (set `VLLM_METRICS_URL` if it lives elsewhere) |

**3 — GPU**
```
./lt status                    # vLLM running / waiting requests and KV cache, from /metrics
```
GPU utilisation and memory: the endpoint's monitoring page in TIR (there is no shell on the GPU node).

**Grafana:** http://localhost:3000 (admin / `GRAFANA_ADMIN_PASSWORD`). The ClickHouse plugin is installed; add a data source (server `clickhouse`, port `8123`, user `default`) and chart the queries from [§11](#11-results-and-export). A ready-made dashboard is not built yet.

---

## 11. Results and export

```
./lt status                    # final table per step and function: rows, ok, p50, p95, TTFT, schema_ok
./lt export                    # last run → data/exports/<run_id>/
                               #   event_responses / ai_info_responses / image_analysis_responses .parquet
                               #   summary.json: run settings + speed and errors per step and function
```

Example ClickHouse queries:
```
docker compose exec clickhouse clickhouse-client
```
```sql
-- speed of valid responses per function for a run
SELECT 'event' AS function, count() AS n, round(quantile(0.5)(e2e_ms)) AS p50_ms, round(quantile(0.95)(e2e_ms)) AS p95_ms,
       round(quantile(0.95)(ttft_ms)) AS p95_ttft_ms FROM event_responses WHERE run_id = '<run_id>'
UNION ALL
SELECT 'ai_info', count(), round(quantile(0.5)(e2e_ms)), round(quantile(0.95)(e2e_ms)), round(quantile(0.95)(ttft_ms))
FROM ai_info_responses WHERE run_id = '<run_id>'
UNION ALL
SELECT 'image_analysis', count(), round(quantile(0.5)(e2e_ms)), round(quantile(0.95)(e2e_ms)), round(quantile(0.95)(ttft_ms))
FROM image_analysis_responses WHERE run_id = '<run_id>';

-- latency per ramp step (find where it breaks)
SELECT step, count() AS n, round(quantile(0.95)(e2e_ms)) AS p95_ms
FROM event_responses WHERE run_id = '<run_id>' GROUP BY step ORDER BY step;
```
Failures and error rates are in `./lt status` and `summary.json`, since failed requests have no valid response to store.

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
  `image_id` = event folder name, image file name or case id. The model's answers are in `event_responses.alert_valid` and `image_analysis_responses.labels`, compared per case.
  ```
  docker compose run --rm dataset python -m src.services.ground_truth     # how many cases are labelled
  ```
- **Trained vs untrained:** every result carries its split, so quality and speed can be compared on seen vs unseen images.

---

## 13. E2E endpoint setup

The model is called like the [E2E TIR vLLM guide](https://docs.e2enetworks.com/docs/tir/Inference/Tutorials/vllm_openai/): the `openai` SDK with `base_url` = endpoint URL and `api_key` = TIR auth token (sent as `Authorization: Bearer`). The client uses `AsyncOpenAI` with SDK retries off, because the worker does its own retries and DLQ.

| `.env` | Value |
|---|---|
| `VLLM_URL` | Endpoint URL from the TIR endpoint page, ending in `/v1`, e.g. `https://infer.e2enetworks.net/project/<p-id>/endpoint/<is-id>/v1` |
| `VLLM_API_KEY` | TIR auth token |
| `MODEL` | Served model name, exactly as `GET <VLLM_URL>/models` lists it (e.g. `Qwen/Qwen3.6-35B-A3B-FP8`) |
| `EXTRA_BODY` | Optional, e.g. `{"chat_template_kwargs": {"enable_thinking": false}}` |
| `VLLM_METRICS_URL` | Empty = `<VLLM_URL without /v1>/metrics` |

All three of `VLLM_URL`, `VLLM_API_KEY` and `MODEL` are required; the worker refuses to start without them. Before a run:
```
docker compose run --rm -T dataset python -m src.services.vllm_client --check
```
| Result | Meaning |
|---|---|
| `✅ <model> is up at ...` | Ready |
| `HTTP 503 ... no healthy upstream` | Endpoint stopped, scaled to 0 or still loading the model: check its status in TIR |
| `401` / `403` | Wrong token, or no access to this endpoint |
| `404` | `VLLM_URL` is not the endpoint URL ending in `/v1` |
| `MODEL=... not served` | Use one of the names it lists |

**E2E vLLM checklist**
- A **vision** model (text-only 70B models such as Llama 3 70B can't see images).
- `--max-model-len 20480` or more (ai_info ≈ 14.4K prompt + 4,096 output tokens).
- `--max-num-seqs` sized for the GPUs; keep `WORKER_CONCURRENCY` ≥ 1.5 × `--max-num-seqs`.
- `/metrics` reachable from the test machine.
- Prefix caching can stay on at the endpoint: `ISOLATE_PREFIX_CACHE=true` sends a unique `cache_salt` per job (vLLM ≥ 0.9), so repeated test images don't hit the cache. `prefix_cache_hit_rate` in `./lt status` should stay near 0 during a run.

---

## 14. Settings reference

All in `.env` (see `.env.example`). Main ones:

| Setting | Default | Meaning |
|---|---|---|
| `VLLM_URL`, `VLLM_API_KEY`, `MODEL` | | E2E TIR endpoint URL (ending in `/v1`), auth token, served model name; all required |
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
| `IMAGE_VARIANTS_PX`, `VIDEO_VARIANTS_SIDE` | `1003520`, `1280` | Sizes prepared in the cache (comma list to prepare more; `MAX_PIXELS` / `VIDEO_SIDE` must be one of them) |
| `VIDEO_SIDE` | `1280` | Video variant sent to the model |
| `VIDEO_MAX_SECONDS`, `VIDEO_FPS`, `JPEG_QUALITY` | `10`, `0`, `90` | Media preparation |
| `GUIDED_JSON` | `false` | Force JSON schema output (vLLM) |
| `EXTRA_BODY` | | Extra JSON in every request |
| `ISOLATE_PREFIX_CACHE` | `true` | Unique vLLM `cache_salt` per job, so repeated test images don't hit the prefix cache |
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
    tables.py                the 3 ClickHouse response tables
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
    vllm_client.py           streaming client for the E2E endpoint (openai SDK; --check pre-flight, single-request try-out)
    job_queue.py             Redis Streams queue, retries, DLQ, idempotency, control
    scheduler.py             open-loop scheduler, drain, auto-stop
    worker.py                concurrent request execution
    ch_writer.py             batched, deduplicated writes of valid responses
    run_stats.py             live per-request stats in Redis (status, auto-stop, summary)
    metrics_poller.py        vLLM Prometheus metrics → Redis
    prompt_snapshot.py       dev tool: re-render prompts from a roboi-main checkout
    cli.py                   ./lt commands
data/                        (not in git) manifest, cache, exports, logs, ground_truth.csv, events/
```

---

## 16. Troubleshooting

| Problem | Fix |
|---|---|
| `run.sh` stops at step 3 | Read the message (see §13): 503 = endpoint not running, 401 = token, 404 = URL. Re-check with `docker compose run --rm -T dataset python -m src.services.vllm_client --check` |
| `MODEL=... not served` | Use one of the names `run.sh` prints |
| `schema_ok_pct` low, outputs cut off | Context too small: raise `--max-model-len` on the endpoint (≥ 20480) |
| Many `expired` / `timeout` | The endpoint is overloaded: use `--rate-scale 0.1`, raise `JOB_DEADLINE_S` / `REQUEST_TIMEOUT_S` |
| `run ... is still active` | `./lt stop`, or `./lt run ... --force` |
| `no manifest` / `not in cache` | Run manifest and preencode (or `./run.sh`) |
| Event cases use `synthetic` JSON | Add `payload.json` event folders (§3) |
| `metrics-poller`: `Server metrics off` | The endpoint has no `/metrics` (see §10); set `VLLM_METRICS_URL` if it lives elsewhere |
| Service unhealthy | `docker compose ps`, `docker compose logs <service>` |
| Changed code or `.env` not applied | `docker compose build`, then `docker compose up -d --force-recreate` (one image serves every service; `run.sh` never builds) |
