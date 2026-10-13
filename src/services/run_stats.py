import json
import statistics
from typing import Dict, List, Optional

from src.core import keys
from src.models.job import Result

ERROR_STATUSES = ("error", "timeout", "rate_limited", "expired", "dlq")
STATS_TTL_S = 7 * 86400


def entry(r: Result) -> str:
    """One compact line per request attempt, kept in Redis for live numbers and auto-stop"""
    def ms(a: Optional[int], b: Optional[int]) -> Optional[int]:
        return b - a if a and b else None

    return json.dumps({
        "c": r.created_ts, "s": r.step, "f": r.function, "st": r.status.value,
        "j": r.json_parse_ok, "v": r.schema_valid,
        "e2e": ms(r.created_ts, r.done_ts), "ttft": ms(r.sent_ts, r.first_token_ts),
    }, separators=(",", ":"))


async def load(redis, run_id: str, last: int = 0) -> List[Dict]:
    """All entries of a run, or only the last N"""
    raw = await redis.lrange(keys.run_stats_key(run_id), -last if last else 0, -1)
    return [json.loads(x) for x in raw]


def quantile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    return round(statistics.quantiles(values, n=100, method="inclusive")[round(q * 100) - 1])


def summarize(entries: List[Dict]) -> Dict:
    ok = [e for e in entries if e["st"] == "ok"]
    pct = lambda flag: round(sum(bool(e[flag]) for e in ok) * 100 / len(ok), 1) if ok else None
    return {
        "rows": len(entries),
        "ok": len(ok),
        "failed": sum(e["st"] in ERROR_STATUSES for e in entries),
        "p50_e2e_ms": quantile([e["e2e"] for e in ok if e["e2e"] is not None], 0.5),
        "p95_e2e_ms": quantile([e["e2e"] for e in ok if e["e2e"] is not None], 0.95),
        "p95_ttft_ms": quantile([e["ttft"] for e in ok if e["ttft"] is not None], 0.95),
        "json_ok_pct": pct("j"),
        "schema_ok_pct": pct("v"),
    }


def last_minute(entries: List[Dict], now: int) -> Dict:
    recent = [e for e in entries if e["c"] >= now - 60_000]
    return {**summarize(recent), "rps": round(len(recent) / 60, 3)}


def by_step_function(entries: List[Dict]) -> List[Dict]:
    groups: Dict = {}
    for e in entries:
        groups.setdefault((e["s"], e["f"]), []).append(e)
    rows = []
    for (step, function), group in sorted(groups.items()):
        s = summarize(group)
        rows.append({"step": step, "function": function, "rows": s["rows"], "ok": s["ok"],
                     "not_ok": s["rows"] - s["ok"], "p50_ms": s["p50_e2e_ms"], "p95_ms": s["p95_e2e_ms"],
                     "p95_ttft_ms": s["p95_ttft_ms"], "schema_ok_pct": s["schema_ok_pct"]})
    return rows
