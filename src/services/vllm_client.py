import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional

import httpx

from src.core.config import Settings, get_settings
from src.models.job import Status, now_ms

DEFAULT_RETRY_AFTER_S = 1.0


@dataclass
class CallResult:
    """Timings (epoch ms), tokens and outcome of one chat completion"""
    status: Status
    sent_ts: int
    first_token_ts: Optional[int] = None
    done_ts: Optional[int] = None
    text: str = ""
    reasoning_text: str = ""
    finish_reason: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    http_status: Optional[int] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    retry_after_s: Optional[float] = None

    @property
    def ttft_ms(self) -> Optional[int]:
        return self.first_token_ts - self.sent_ts if self.first_token_ts else None

    @property
    def latency_ms(self) -> Optional[int]:
        return self.done_ts - self.sent_ts if self.done_ts else None


def parse_retry_after(value: Optional[str]) -> float:
    """Retry-After is either seconds or an HTTP date"""
    if not value:
        return DEFAULT_RETRY_AFTER_S
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_S


class VllmClient:
    """Async OpenAI-compatible client (vLLM on E2E, Ollama locally) that streams and times every request"""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        headers = {"Content-Type": "application/json"}
        if self.settings.vllm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.vllm_api_key}"
        timeout = self.settings.request_timeout_s
        self.http = httpx.AsyncClient(
            base_url=self.settings.vllm_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(
                max_connections=self.settings.worker_concurrency,
                max_keepalive_connections=self.settings.worker_concurrency,
            ),
        )
        self.extra_body: Dict = json.loads(self.settings.extra_body) if self.settings.extra_body else {}

    async def close(self) -> None:
        await self.http.aclose()

    async def models(self) -> List[str]:
        response = await self.http.get("/models")
        response.raise_for_status()
        return [m["id"] for m in response.json().get("data", [])]

    async def chat(self, body: Dict, deadline_ts: Optional[int] = None) -> CallResult:
        """Stream one chat completion. Never raises; the outcome is in CallResult.status"""
        body = {**body, **self.extra_body, "stream": True, "stream_options": {"include_usage": True}}
        timeout_s = self.settings.request_timeout_s
        if deadline_ts is not None:
            timeout_s = min(timeout_s, (deadline_ts - now_ms()) / 1000)
        result = CallResult(status=Status.ERROR, sent_ts=now_ms())
        if timeout_s <= 0:
            result.status, result.error_type = Status.EXPIRED, "deadline_passed"
            return result

        try:
            async with asyncio.timeout(timeout_s):
                async with self.http.stream("POST", "/chat/completions", json=body) as response:
                    result.http_status = response.status_code
                    if response.status_code != 200:
                        await self._http_error(response, result)
                        return result
                    await self._read_stream(response, result)
        except (TimeoutError, httpx.TimeoutException):
            result.status, result.error_type = Status.TIMEOUT, "timeout"
            result.error_message = f"no complete response within {timeout_s:.1f}s"
        except httpx.HTTPError as e:
            result.status, result.error_type = Status.ERROR, type(e).__name__
            result.error_message = str(e)[:500]
        result.done_ts = result.done_ts or now_ms()
        return result

    async def _http_error(self, response: httpx.Response, result: CallResult) -> None:
        text = (await response.aread()).decode(errors="replace")
        result.done_ts = now_ms()
        result.error_message = text[:500]
        if response.status_code == 429:
            result.status, result.error_type = Status.RATE_LIMITED, "http_429"
            result.retry_after_s = parse_retry_after(response.headers.get("retry-after"))
        else:
            result.status, result.error_type = Status.ERROR, f"http_{response.status_code}"

    async def _read_stream(self, response: httpx.Response, result: CallResult) -> None:
        text: List[str] = []
        reasoning: List[str] = []
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if "error" in chunk:
                result.status, result.error_type = Status.ERROR, "stream_error"
                result.error_message = json.dumps(chunk["error"])[:500]
                result.done_ts = now_ms()
                return
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                content = delta.get("content")
                # vLLM reasoning parsers: reasoning_content; Ollama: reasoning
                thought = delta.get("reasoning_content") or delta.get("reasoning")
                if (content or thought) and result.first_token_ts is None:
                    result.first_token_ts = now_ms()
                if content:
                    text.append(content)
                if thought:
                    reasoning.append(thought)
                if choice.get("finish_reason"):
                    result.finish_reason = choice["finish_reason"]
            usage = chunk.get("usage")
            if usage:
                result.prompt_tokens = usage.get("prompt_tokens")
                result.completion_tokens = usage.get("completion_tokens")
                result.cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        result.done_ts = now_ms()
        result.text, result.reasoning_text = "".join(text), "".join(reasoning)
        result.status = Status.OK
        if result.finish_reason == "length":
            result.error_type = "max_tokens_reached"


async def _try(args) -> int:
    """Send one real request for a dataset case and print what the load test would record"""
    from src.services.event_meta import EventMetaLoader
    from src.services.job_builder import JobBuilder
    from src.services.manifest import load_manifest
    from src.services.tasks import get_task

    settings = get_settings()
    client = VllmClient(settings)
    try:
        print(f"🔌 {settings.vllm_url} models: {await client.models()}")
        rows = [r for r in load_manifest() if args.function in r.functions(settings.function_list)]
        if args.case:
            rows = [r for r in rows if r.case_id == args.case]
        if not rows:
            print(f"❌ no {args.function} case found", file=sys.stderr)
            return 1
        builder = JobBuilder(settings, EventMetaLoader())
        task = get_task(args.function, settings)
        failed = 0
        for row in rows[:args.n]:
            job = builder.build(row, args.function, run_id="try", deadline_s=settings.request_timeout_s)
            body = task.build_request(job, settings.model)
            call = await client.chat(body)
            parsed = task.parse(call.text, job) if call.status == Status.OK else None
            failed += not (parsed and parsed.schema_valid)
            print(f"\n▶ {args.function} {row.case_id} media={[m.kind for m in job.media]} "
                  f"request={len(json.dumps(body)) // 1024}KB")
            print(f"  status={call.status} http={call.http_status} ttft={call.ttft_ms}ms latency={call.latency_ms}ms "
                  f"tokens in/out={call.prompt_tokens}/{call.completion_tokens} finish={call.finish_reason} "
                  f"reasoning_chars={len(call.reasoning_text)} {call.error_type or ''} {call.error_message or ''}")
            text_tokens = sum(len(p["text"]) for m in body["messages"]
                              for p in ([{"text": m["content"]}] if isinstance(m["content"], str) else m["content"])
                              if "text" in p) // 4
            if call.prompt_tokens and call.prompt_tokens < 0.8 * text_tokens:
                print(f"  ⚠️  server counted {call.prompt_tokens} prompt tokens but the text alone is ~{text_tokens}: "
                      f"the prompt was probably cut to the server's context length")
            if parsed:
                print(f"  json_ok={parsed.json_parse_ok} schema_ok={parsed.schema_valid} "
                      f"key_fields={parsed.key_fields} {parsed.error or ''}")
                if args.show:
                    print(json.dumps(parsed.parsed, indent=2)[:3000] if parsed.parsed else call.text[:3000])
        return 1 if failed else 0
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Send real requests for dataset cases to VLLM_URL and print timings")
    parser.add_argument("--function", default="event", choices=["event", "ai_info", "extraction"])
    parser.add_argument("--case", help="case_id from the manifest (default: first matching cases)")
    parser.add_argument("-n", type=int, default=1, help="number of cases to send, one after another")
    parser.add_argument("--show", action="store_true", help="print the formatted response")
    return asyncio.run(_try(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
