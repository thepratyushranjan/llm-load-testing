#!/usr/bin/env bash
# One command: start the stack, check the model, prepare the dataset, then run load tests until Ctrl+C.
#   ./run.sh                                     PROFILE from .env, repeated until Ctrl+C
#   ./run.sh baseline ramp                       these profiles in order, repeated until Ctrl+C
#   ./run.sh --once ramp                         run once, then exit
#   ./run.sh ramp -- --rate-scale 0.1 --sample 100   extra options for ./lt run
# Test data: DATASET_DIR in .env. Event / AI-info JSON: one folder per event holding payload.json + its images.
# Output is also saved to data/logs/run-<time>.log
set -u -o pipefail
cd "$(dirname "$0")"

ONCE=0; PROFILES=(); EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --once) ONCE=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    --) shift; EXTRA=("$@"); break ;;
    *) PROFILES+=("$1") ;;
  esac
  shift
done
if [ ${#PROFILES[@]} -eq 0 ]; then  # no test named: PROFILE from .env
  PROFILE=$(sed -n 's/^PROFILE=\([^ #]*\).*/\1/p' .env 2>/dev/null | tr -d "\"'" | tail -1)
  [ -n "$PROFILE" ] || { echo "❌ name a test (./run.sh baseline) or set PROFILE= in .env"; exit 1; }
  PROFILES=("$PROFILE")
fi

mkdir -p data/logs
LOG="data/logs/run-$(date +%Y%m%d-%H%M%S).log"
exec > >(trap '' INT TERM; exec tee -a "$LOG") 2>&1

py() { docker compose run --rm -T dataset python "$@"; }
lt() { py -m src.services.cli "$@"; }

child=""
on_stop() {
  trap '' INT TERM
  echo; echo "⏹  Stopping the load test ..."
  [ -n "$child" ] && kill "$child" 2>/dev/null
  lt stop >/dev/null 2>&1
  sleep 3
  lt status 2>/dev/null | grep -v ClickHouse
  echo "✅ Stopped. Services keep running (docker compose down to shut them). Log: $LOG"
  exit 130
}
trap on_stop INT TERM

step() { echo; echo "━━━ $* ━━━"; }
fail() { echo "❌ $*"; exit 1; }

step "1/5 Checks"
[ -f .env ] || fail ".env missing: cp .env.example .env and fill it in"
docker info >/dev/null 2>&1 || fail "Docker is not running"
grep -E "^(VLLM_URL|MODEL|DATASET_DIR)=" .env
echo "TESTS=${PROFILES[*]}"

step "2/5 Starting services"
docker compose up -d --wait || fail "services did not become healthy (docker compose ps / docker compose logs)"

step "3/5 Checking the model"
py -c '
import sys, httpx
from src.core.config import get_settings
s = get_settings()
headers = {"Authorization": f"Bearer {s.vllm_api_key}"} if s.vllm_api_key else {}
try:
    r = httpx.get(s.vllm_url.rstrip("/") + "/models", headers=headers, timeout=15)
    r.raise_for_status()
except Exception as e:
    sys.exit(f"❌ model server not reachable at {s.vllm_url}: {e}")
ids = [m["id"] for m in r.json().get("data", [])]
if s.model not in ids:
    sys.exit(f"❌ MODEL={s.model} not served at {s.vllm_url}; available: {ids}")
print(f"✅ {s.model} is up at {s.vllm_url}")
' || exit 1

step "4/5 Preparing the dataset"
py -m src.services.manifest | tail -6 || fail "manifest failed (is DATASET_DIR in .env a folder of images / event folders?)"
py -m src.services.preencode | tail -2 || fail "preencode failed"

step "5/5 Checking event / AI-info JSON"
py -c '
from collections import Counter
from src.core.config import get_settings
from src.services.event_meta import EVENT_FUNCTIONS, EventMetaLoader
from src.services.manifest import load_manifest
enabled = get_settings().function_list
rows = load_manifest()
loader = EventMetaLoader()
source = Counter((fn, loader.get(r, fn).source) for r in rows for fn in r.functions(enabled) if fn in EVENT_FUNCTIONS)
images = sum("extraction" in r.functions(enabled) for r in rows)
print(f"   image analysis cases: {images}")
for fn in (f for f in EVENT_FUNCTIONS if f in enabled):
    yours = source[(fn, "json")] + source[(fn, "template")]
    generated = source[(fn, "synthetic")]
    print(f"   {fn:8} cases: {yours} with your JSON (payload.json)" + (f", {generated} with generated JSON" if generated else ""))
    if generated:
        print(f"   ⚠️  {generated} {fn} cases have no payload.json: fine for speed, but the answers are not meaningful")
' || fail "could not read the event / AI-info JSON"

round=1
while true; do
  for profile in "${PROFILES[@]}"; do
    step "Round $round · profile $profile · $(date '+%H:%M:%S') · Ctrl+C to stop"
    lt run --profile "$profile" --force --follow --notes "run.sh round $round" "${EXTRA[@]}" &
    child=$!
    wait "$child"
    child=""
    lt export 2>/dev/null | grep "exported" | sed 's#/app/data#data#'
  done
  [ "$ONCE" -eq 1 ] && break
  round=$((round + 1))
  echo "⏳ next round in 10 s (Ctrl+C to stop)"; sleep 10
done
echo "✅ Done. Log: $LOG"
