import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel, TypeAdapter, ValidationError

from src.core.config import Settings, get_settings
from src.models.job import Job, MediaRef

MIME = {"image": "image/jpeg", "video": "video/mp4"}  # cache files are always .jpg / .mp4


def fill(template: str, values: Dict[str, Any]) -> str:
    """Replace ⟪name⟫ markers left by the prompt snapshot"""
    for name, value in values.items():
        template = template.replace(f"⟪{name}⟫", str(value))
    return template


@dataclass
class ParsedResponse:
    raw_text: str
    parsed: Optional[Dict] = None
    json_parse_ok: bool = False
    schema_valid: bool = False
    error: Optional[str] = None
    key_fields: Dict[str, Any] = field(default_factory=dict)


class BaseTask:
    """One load-test function: builds the vLLM request and parses the reply"""

    name: str = ""
    output_schema: Type[BaseModel]  # JSON the prompt asks the model for (guided JSON)
    response_schema: TypeAdapter    # dict production returns after _format_response
    temperature: float = 0.4
    max_tokens: int = 2048
    text_first: bool = False  # production order of text vs media parts

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()

    # ── request ──────────────────────────────────────────────────────────

    def prompts(self, job: Job) -> Tuple[Optional[str], str]:
        """(system_prompt, user_prompt)"""
        raise NotImplementedError

    def media_url(self, ref: MediaRef) -> str:
        """Media goes inside the request body as a base64 data URL; nothing is uploaded"""
        data = (Path(self.settings.data_dir) / ref.path).read_bytes()
        return f"data:{MIME[ref.kind]};base64,{base64.b64encode(data).decode()}"

    def media_parts(self, job: Job) -> List[Dict]:
        parts = []
        for ref in job.media:
            key = "image_url" if ref.kind == "image" else "video_url"
            parts.append({"type": key, key: {"url": self.media_url(ref)}})
        return parts

    def build_request(self, job: Job, model: str, guided_json: Optional[bool] = None) -> Dict:
        """OpenAI-compatible chat completion body for vLLM"""
        system, user = self.prompts(job)
        text = [{"type": "text", "text": user}]
        media = self.media_parts(job)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": text + media if self.text_first else media + text})

        body = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.settings.guided_json if guided_json is None else guided_json:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": f"{self.name}_response", "schema": self.output_schema.model_json_schema()},
            }
        return body

    # ── response ─────────────────────────────────────────────────────────

    @staticmethod
    def extract_json(text: str) -> Any:
        """Same as production BaseValidationService.clean_json_response"""
        if not text or not text.strip():
            raise ValueError("Empty response from model")
        text = text.strip()
        match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
        if match:
            text = match.group(1).strip()
        return json.loads(text, strict=False)

    def format_response(self, job: Job, ai_validation: Dict) -> Dict:
        """Production _format_response; identity where production returns the JSON as-is"""
        return ai_validation

    def parse(self, raw_text: str, job: Job) -> ParsedResponse:
        """raw model text → production response dict, validated against the production shape"""
        result = ParsedResponse(raw_text=raw_text)
        try:
            data = self.extract_json(raw_text)
        except Exception as e:
            result.error = f"json: {e}"
            return result
        if not isinstance(data, dict):
            result.error = f"json: expected object, got {type(data).__name__}"
            return result
        result.json_parse_ok = True
        try:
            # production returns {"error": ...} when formatting raises
            formatted = self.format_response(job, data)
        except Exception as e:
            result.error = f"format: {type(e).__name__}: {e}"
            return result
        result.parsed = formatted
        try:
            self.response_schema.validate_python(formatted)
        except ValidationError as e:
            result.error = f"schema: {e.error_count()} errors: " + "; ".join(
                f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:5]
            )
            return result
        result.schema_valid = True
        result.key_fields = self.key_fields(formatted)
        return result

    def key_fields(self, response: Dict) -> Dict[str, Any]:
        """Fields compared against ground truth: alert_valid, plate_number, labels, severity"""
        raise NotImplementedError


def event_context(job: Job) -> Tuple[Dict, Dict]:
    meta = (job.event_meta or {}).get("meta", {})
    data = (job.event_meta or {}).get("data", {})
    return meta, data


def media_event_context(job: Job, default_zone: Optional[str] = None) -> Dict[str, Any]:
    """Same fields as production MediaEventContext.from_event_data.

    event_folder / media_files point at the local case instead of GCS URLs.
    """
    event_data = job.event_meta or {}
    meta, data = event_context(job)
    recognitions = data.get("recognitions", [{}])
    first_recognition = recognitions[0] if recognitions else {}
    return {
        "processed_at": event_data.get("processed_at"),
        "cam_name": meta.get("cam_name"),
        "cam_id": meta.get("cam_id"),
        "site_name": meta.get("site_name"),
        "site_id": meta.get("site_id"),
        "company_id": meta.get("company_id"),
        "device_id": meta.get("device_id"),
        "latitude": meta.get("latitude"),
        "longitude": meta.get("longitude"),
        "country": meta.get("country"),
        "state": meta.get("state"),
        "district": meta.get("district"),
        "status": meta.get("status"),
        "people_count": data.get("people_count"),
        "triggers": data.get("triggers"),
        "capture_triggered": data.get("capture_triggered"),
        "detected_labels": [d.get("label") for d in data.get("detections", [])],
        "display_label": first_recognition.get("display_label"),
        "identity": first_recognition.get("identity"),
        "event_folder_uri": f"loadtest://{job.case_id}",
        "media_files": [ref.path for ref in job.media],
        "zone_type": meta.get("zone_type", default_zone),
    }
