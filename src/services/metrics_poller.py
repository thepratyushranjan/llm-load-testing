import re
import time
from collections import defaultdict
from typing import Dict, Optional

import httpx

from src.core import keys
from src.core.profiles import RunSpec
from src.services.base import BaseService

LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eE]+|NaN|[+-]Inf)')
WARN_EVERY_S = 60


def parse_prometheus(text: str) -> Dict[str, float]:
    """Sum each metric over its label sets (several engines / models)"""
    out: Dict[str, float] = defaultdict(float)
    for line in text.splitlines():
        m = LINE.match(line)
        if m and m.group(3) not in ("NaN", "+Inf", "-Inf"):
            out[m.group(1)] += float(m.group(3))
    return out


def first(metrics: Dict[str, float], *names: str) -> Optional[float]:
    return next((metrics[n] for n in names if n in metrics), None)


class MetricsPoller(BaseService):
    """Scrapes vLLM /metrics into Redis (latest sample) for auto-stop and ./lt status"""

    name = "metrics-poller"

    async def setup(self) -> None:
        await super().setup()
        headers = {"Authorization": f"Bearer {self.settings.vllm_api_key}"} if self.settings.vllm_api_key else {}
        self.http = httpx.AsyncClient(headers=headers, timeout=3.0)
        self.prev: Optional[Dict[str, float]] = None
        self.prev_t = 0.0
        self.last_warning = 0.0

    async def teardown(self) -> None:
        await self.http.aclose()
        await super().teardown()

    async def run(self) -> None:
        self.log.info(f"Scraping {self.settings.metrics_url} every {self.settings.metrics_poll_interval_s}s")
        while not self.stopping:
            started = time.monotonic()
            try:
                await self.poll()
            except Exception as e:
                if time.monotonic() - self.last_warning > WARN_EVERY_S:
                    self.last_warning = time.monotonic()
                    self.log.warning(f"Metrics unavailable at {self.settings.metrics_url}: {e}")
            await self.wait_or_stop(max(0.0, self.settings.metrics_poll_interval_s - (time.monotonic() - started)))

    async def poll(self) -> None:
        response = await self.http.get(self.settings.metrics_url)
        response.raise_for_status()
        now = time.monotonic()
        m = parse_prometheus(response.text)
        if "vllm:num_requests_running" not in m:
            raise ValueError("no vllm:* metrics in the response (not a vLLM server?)")

        rates = {"prompt": None, "generation": None, "hit_rate": first(m, "vllm:gpu_prefix_cache_hit_rate")}
        if self.prev is not None and now > self.prev_t:
            dt = now - self.prev_t

            def delta(name: str) -> Optional[float]:
                if name in m and name in self.prev and m[name] >= self.prev[name]:  # counters reset on restart
                    return m[name] - self.prev[name]
                return None

            prompt, gen = delta("vllm:prompt_tokens_total"), delta("vllm:generation_tokens_total")
            rates["prompt"] = prompt / dt if prompt is not None else None
            rates["generation"] = gen / dt if gen is not None else None
            hits, queries = delta("vllm:prefix_cache_hits_total"), delta("vllm:prefix_cache_queries_total")
            if hits is not None and queries:
                rates["hit_rate"] = hits / queries
        self.prev, self.prev_t = m, now

        raw = await self.redis.get(keys.CURRENT_RUN_KEY)
        sample = {
            "ts_ms": int(time.time() * 1000),
            "run_id": RunSpec.model_validate_json(raw).run_id if raw else "",
            "requests_running": m.get("vllm:num_requests_running", 0.0),
            "requests_waiting": m.get("vllm:num_requests_waiting", 0.0),
            "kv_cache_usage": first(m, "vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc") or 0.0,
            "prefix_cache_hit_rate": rates["hit_rate"],
            "prompt_tokens_per_s": rates["prompt"],
            "generation_tokens_per_s": rates["generation"],
        }
        await self.redis.hset(keys.SERVER_METRICS_KEY, mapping={
            k: "" if v is None else str(round(v, 4) if isinstance(v, float) else v) for k, v in sample.items()})


if __name__ == "__main__":
    MetricsPoller().start()
