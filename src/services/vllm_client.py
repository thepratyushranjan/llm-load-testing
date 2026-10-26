import argparse
import asyncio
import inspect
import json
import sys
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional

import httpx
from openai import (APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI, AsyncStream,
                    AuthenticationError, DefaultAsyncHttpxClient, NotFoundError, PermissionDeniedError,
                    RateLimitError)
from openai.resources.chat.completions import AsyncCompletions
from openai.types.chat import ChatCompletionChunk

from src.core.config import Settings, get_settings
from src.models.job import Status, now_ms

DEFAULT_RETRY_AFTER_S = 1.0
CHECK_TIMEOUT_S = 15.0
# Body fields the SDK takes as arguments; the rest (vLLM-only, e.g. cache_salt) go through extra_body
SDK_PARAMS = set(inspect.signature(AsyncCompletions.create).parameters) - {"self", "extra_body"}


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
    """Async client for the E2E TIR vLLM endpoint (OpenAI SDK) that streams and times every request"""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.settings.require_vllm()
        timeout = self.settings.request_timeout_s
        self.client = AsyncOpenAI(
            base_url=self.settings.vllm_url.rstrip("/"),
            api_key=self.settings.vllm_api_key,
            timeout=httpx.Timeout(timeout, connect=10.0),
            max_retries=0,  # the worker retries (429 / 5xx / timeout) and sends failures to the DLQ
            http_client=DefaultAsyncHttpxClient(limits=httpx.Limits(
                max_connections=self.settings.worker_concurrency,
                max_keepalive_connections=self.settings.worker_concurrency,
            )),
        )
        self.extra_body: Dict = json.loads(self.settings.extra_body) if self.settings.extra_body else {}

    async def close(self) -> None:
        await self.client.close()

    async def models(self) -> List[str]:
        return [m.id async for m in self.client.models.list()]

    async def chat(self, body: Dict, deadline_ts: Optional[int] = None) -> CallResult:
        """Stream one chat completion. Never raises; the outcome is in CallResult.status"""
        timeout_s = self.settings.request_timeout_s
        if deadline_ts is not None:
            timeout_s = min(timeout_s, (deadline_ts - now_ms()) / 1000)
        result = CallResult(status=Status.ERROR, sent_ts=now_ms())
        if timeout_s <= 0:
            result.status, result.error_type = Status.EXPIRED, "deadline_passed"
            return result

        params = {k: v for k, v in body.items() if k in SDK_PARAMS}
        extra = {**{k: v for k, v in body.items() if k not in SDK_PARAMS}, **self.extra_body}
        try:
            async with asyncio.timeout(timeout_s):
                stream = await self.client.chat.completions.create(
                    **params, stream=True, stream_options={"include_usage": True}, extra_body=extra or None,
                )
                async with stream:
                    result.http_status = stream.response.status_code
                    await self._read_stream(stream, result)
        except RateLimitError as e:
            result.status, result.error_type, result.http_status = Status.RATE_LIMITED, "http_429", e.status_code
            result.retry_after_s = parse_retry_after(e.response.headers.get("retry-after"))
            result.error_message = str(e)[:500]
        except APIStatusError as e:
            result.status, result.error_type, result.http_status = Status.ERROR, f"http_{e.status_code}", e.status_code
            result.error_message = str(e)[:500]
        except (TimeoutError, APITimeoutError, httpx.TimeoutException):
            result.status, result.error_type = Status.TIMEOUT, "timeout"
            result.error_message = f"no complete response within {timeout_s:.1f}s"
        except APIConnectionError as e:
            cause = e.__cause__ or e
            result.status, result.error_type = Status.ERROR, type(cause).__name__
            result.error_message = str(cause)[:500]
        except APIError as e:  # error event inside the stream
            result.status, result.error_type = Status.ERROR, "stream_error"
            result.error_message = json.dumps(e.body)[:500] if e.body else str(e)[:500]
        except httpx.HTTPError as e:  # connection dropped while reading the stream
            result.status, result.error_type = Status.ERROR, type(e).__name__
            result.error_message = str(e)[:500]
        result.done_ts = result.done_ts or now_ms()
        return result

    async def _read_stream(self, stream: AsyncStream[ChatCompletionChunk], result: CallResult) -> None:
        text: List[str] = []
        reasoning: List[str] = []
        async for chunk in stream:
            for choice in chunk.choices:
                delta = choice.delta
                content = delta.content if delta else None
                # vLLM reasoning parsers: reasoning_content (older releases) / reasoning (newer)
                thought = (getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)) if delta else None
                if (content or thought) and result.first_token_ts is None:
                    result.first_token_ts = now_ms()
                if content:
                    text.append(content)
                if thought:
                    reasoning.append(thought)
                if choice.finish_reason:
                    result.finish_reason = choice.finish_reason
            if chunk.usage:
                result.prompt_tokens = chunk.usage.prompt_tokens
                result.completion_tokens = chunk.usage.completion_tokens
                details = chunk.usage.prompt_tokens_details
                result.cached_tokens = details.cached_tokens if details else None
        result.done_ts = now_ms()
        result.text, result.reasoning_text = "".join(text), "".join(reasoning)
        result.status = Status.OK
        if result.finish_reason == "length":
            result.error_type = "max_tokens_reached"


async def _check() -> int:
    """Pre-flight (run.sh): the E2E endpoint answers, the auth token is accepted and MODEL is served"""
    settings = get_settings()
    url = settings.vllm_url
    try:
        client = VllmClient(settings)
    except ValueError as e:
        print(f"❌ {e}")
        return 1
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            ids = await client.models()
    except AuthenticationError:
        print(f"❌ {url} rejected VLLM_API_KEY (401): copy the auth token from the TIR endpoint page")
        return 1
    except PermissionDeniedError:
        print(f"❌ {url} refused VLLM_API_KEY (403): the token has no access to this endpoint")
        return 1
    except NotFoundError:
        print(f"❌ {url.rstrip('/')}/models not found (404): VLLM_URL should be the TIR endpoint URL ending in /v1")
        return 1
    except APIStatusError as e:
        print(f"❌ {url} answered HTTP {e.status_code} (endpoint stopped or still starting?): {e.message[:300]}")
        return 1
    except (TimeoutError, APITimeoutError):
        print(f"❌ {url} did not answer within {CHECK_TIMEOUT_S:.0f}s")
        return 1
    except APIConnectionError as e:
        print(f"❌ model server not reachable at {url}: {e.__cause__ or e}")
        return 1
    finally:
        await client.close()
    if settings.model not in ids:
        print(f"❌ MODEL={settings.model} not served at {url}; available: {ids}")
        return 1
    print(f"✅ {settings.model} is up at {url}")
    return 0


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
                stored = {k: v for k, v in parsed.record.items() if k not in ("response_json", "detections")}
                print(f"  json_ok={parsed.json_parse_ok} schema_ok={parsed.schema_valid} "
                      f"{stored or ''} {parsed.error or ''}")
                if args.show:
                    print(json.dumps(parsed.parsed, indent=2)[:3000] if parsed.parsed else call.text[:3000])
        return 1 if failed else 0
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Send real requests for dataset cases to the E2E endpoint (VLLM_URL) and print timings")
    parser.add_argument("--function", default="event", choices=["event", "ai_info", "extraction"])
    parser.add_argument("--case", help="case_id from the manifest (default: first matching cases)")
    parser.add_argument("-n", type=int, default=1, help="number of cases to send, one after another")
    parser.add_argument("--show", action="store_true", help="print the formatted response")
    parser.add_argument("--check", action="store_true",
                        help="only check that the endpoint answers, the token is accepted and MODEL is served")
    args = parser.parse_args()
    return asyncio.run(_check() if args.check else _try(args))


if __name__ == "__main__":
    sys.exit(main())
