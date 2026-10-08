import asyncio
import json
import random
import re
import time
import uuid
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from src.core import prompts
from src.core.config import get_settings

# Stand-in for vLLM so the whole stack can be exercised without a GPU.
# Run: uvicorn src.services.mock_vllm:app --host 0.0.0.0 --port 8000

settings = get_settings()
app = FastAPI(title="mock-vllm")

slots = asyncio.Semaphore(settings.mock_max_num_seqs)
stats = {"running": 0, "waiting": 0, "prompt_tokens": 0, "generation_tokens": 0,
         "success": 0, "errors": 0, "rate_limited": 0}

OFFICE_EXAMPLES = [json.loads(m, strict=False)
                   for m in re.findall(r'^\{\n  "metadata".*?^\}', prompts.OFFICE_SYSTEM, re.S | re.M)]
OBJECT_LABELS = ["person", "car", "motorcycle", "truck", "dog", "backpack", "license_plate"]


def detect_function(messages: List[Dict]) -> str:
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    if isinstance(system, str) and system.startswith(prompts.EVENT_SYSTEM_HEADER[:60]):
        return "event"
    if isinstance(system, str) and system.startswith(prompts.OFFICE_SYSTEM[:60]):
        return "ai_info"
    return "extraction"


def count_inputs(messages: List[Dict]) -> Tuple[int, int, int]:
    """(text_chars, images, videos)"""
    chars = images = videos = 0
    for m in messages:
        content = m.get("content")
        parts = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        for p in parts:
            if p.get("type") == "text":
                chars += len(p.get("text") or "")
            elif p.get("type") == "image_url":
                images += 1
            elif p.get("type") == "video_url":
                videos += 1
    return chars, images, videos


def fake_answer(function: str, rng: random.Random) -> Dict[str, Any]:
    if function == "event":
        valid = rng.random() < 0.4
        people = rng.randint(0, 6)
        return {
            "alert_valid": valid,
            "actual_people_count": people,
            "media_analyzed": 2,
            "validation_summary": (
                "- **Alert Status:** Valid Alert — loitering\n- **Environment Overview:** Mock scene.\n"
                "- **Evidence:** Mock evidence.\n- **Reasoning:** Mock reasoning.\n- **Action:** Dispatch security."
                if valid else
                "- **Environment Overview:** Mock scene with routine movement.\n"
                "- **Why AI Triaged This:** A detection matched the trigger pattern.\n"
                "- **Conclusion:** Activity is within normal parameters."
            ),
            "verdict": "VALID" if valid else "FALSE POSITIVE",
            "what_happened": "• Mock observation one\n• Mock observation two",
            "why_it_happened": "• Mock evidence one\n• Mock evidence two",
            "recommendation": "• Mock action one\n• Mock action two" if valid else "No action required — false positive confirmed.",
            "event_roi": [{"label": "loitering", "box_2d": [200, 350, 750, 580], "confidence": 0.88}] if valid else [],
        }
    if function == "ai_info":
        return rng.choice(OFFICE_EXAMPLES)
    detections = []
    for label in rng.sample(OBJECT_LABELS, k=rng.randint(1, 4)):
        attributes = {}
        if label == "person":
            attributes = {"shirt_color": "blue", "pant_color": "black", "has_bagpack": rng.randint(0, 1)}
        elif label in ("car", "motorcycle", "truck"):
            attributes = {"vehicle_color": "white", "vehicle_type": label}
        elif label == "license_plate":
            attributes = {"plate_number": f"MH{rng.randint(10, 49)}AB{rng.randint(1000, 9999)}"}
        detections.append({"label": label, "attributes": attributes})
    labels = [d["label"] for d in detections]
    return {"person_count": labels.count("person"), "vehicle_count": sum(l in ("car", "motorcycle", "truck") for l in labels),
            "dog_count": labels.count("dog"), "detections": detections}


def chunk(rid: str, model: str, delta: Dict, finish: str = None, usage: Dict = None) -> str:
    body = {"id": rid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
            "choices": [] if usage else [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        body["usage"] = usage
    return f"data: {json.dumps(body)}\n\n"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": settings.model, "object": "model", "owned_by": "mock-vllm"}]}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    # Same metric names as vLLM v1
    labels = f'{{model_name="{settings.model}"}}'
    return "\n".join([
        f"vllm:num_requests_running{labels} {stats['running']}",
        f"vllm:num_requests_waiting{labels} {stats['waiting']}",
        f"vllm:kv_cache_usage_perc{labels} {stats['running'] / settings.mock_max_num_seqs:.4f}",
        f"vllm:prompt_tokens_total{labels} {stats['prompt_tokens']}",
        f"vllm:generation_tokens_total{labels} {stats['generation_tokens']}",
        f"vllm:prefix_cache_queries_total{labels} {stats['prompt_tokens']}",
        f"vllm:prefix_cache_hits_total{labels} 0",
        f'vllm:request_success_total{{model_name="{settings.model}",finished_reason="stop"}} {stats["success"]}',
        "",
    ])


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str = Header(default="")):
    if settings.vllm_api_key and authorization != f"Bearer {settings.vllm_api_key}":
        raise HTTPException(401, "invalid api key")
    body = await request.json()
    rng = random.Random()

    roll = rng.random()
    if roll < settings.mock_rate_limit_rate:
        stats["rate_limited"] += 1
        return JSONResponse({"error": {"message": "rate limited", "type": "rate_limit"}}, status_code=429,
                            headers={"Retry-After": "1"})
    roll -= settings.mock_rate_limit_rate
    if roll < settings.mock_error_rate:
        stats["errors"] += 1
        return JSONResponse({"error": {"message": "mock internal error", "type": "server_error"}}, status_code=500)
    roll -= settings.mock_error_rate
    if roll < settings.mock_hang_rate:
        await asyncio.sleep(3600)

    messages = body.get("messages", [])
    function = detect_function(messages)
    chars, images, videos = count_inputs(messages)
    prompt_tokens = chars // 4 + images * settings.mock_image_tokens + videos * settings.mock_video_tokens

    text = json.dumps(fake_answer(function, rng), indent=2)
    if rng.random() < 0.3:
        text = f"```json\n{text}\n```"
    if rng.random() < settings.mock_invalid_json_rate:
        text = text[: len(text) // 2]
    max_tokens = body.get("max_tokens") or 4096
    completion_tokens = max(1, -(-len(text) // 4))  # ~4 chars per token
    finish = "stop"
    if completion_tokens > max_tokens:
        completion_tokens, text, finish = max_tokens, text[: max_tokens * 4], "length"

    rid, model = f"chatcmpl-{uuid.uuid4().hex[:12]}", body.get("model", settings.model)
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
             "total_tokens": prompt_tokens + completion_tokens}

    async def generate():
        stats["waiting"] += 1
        try:
            await slots.acquire()
        finally:
            stats["waiting"] -= 1
        stats["running"] += 1
        try:
            await asyncio.sleep(prompt_tokens / settings.mock_prefill_tokens_per_s)
            stats["prompt_tokens"] += prompt_tokens
            yield chunk(rid, model, {"role": "assistant", "content": ""})
            per_token = 1 / settings.mock_decode_tokens_per_s
            step = 4  # tokens per streamed chunk
            for i in range(0, completion_tokens, step):
                await asyncio.sleep(per_token * step)
                stats["generation_tokens"] += min(step, completion_tokens - i)
                yield chunk(rid, model, {"content": text[i * 4:(i + step) * 4]})
            yield chunk(rid, model, {}, finish=finish)
            if (body.get("stream_options") or {}).get("include_usage"):
                yield chunk(rid, model, {}, usage=usage)
            yield "data: [DONE]\n\n"
            stats["success"] += 1
        finally:
            stats["running"] -= 1
            slots.release()

    if body.get("stream"):
        return StreamingResponse(generate(), media_type="text/event-stream")
    async for _ in generate():
        pass
    return {"id": rid, "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}],
            "usage": usage}
