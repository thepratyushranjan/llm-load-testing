import json
from typing import Any, Dict, Optional, Tuple

from pydantic import TypeAdapter

from src.core import prompts
from src.models.job import Job
from src.schemas.responses import ExtractionResponse
from src.services.task_base import BaseTask


class ExtractionTask(BaseTask):
    """Image analysis / text extraction: objects, attributes, licence plates"""

    name = "extraction"
    output_schema = ExtractionResponse
    response_schema = TypeAdapter(ExtractionResponse)
    temperature = 0.2
    max_tokens = 1024
    text_first = True  # production sends [prompt, image]

    def prompts(self, job: Job) -> Tuple[Optional[str], str]:
        return None, prompts.EXTRACTION_PROMPT

    @staticmethod
    def extract_json(text: str) -> Any:
        """Same as production SearchAnalyzerService._extract_json"""
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        else:
            text = text.strip()
        return json.loads(text)

    def record(self, response: Dict) -> Dict[str, Any]:
        detections = [d for d in response["detections"] if isinstance(d, dict)]
        plates = [str((d.get("attributes") or {}).get("plate_number", "")).strip()
                  for d in detections if d.get("label") == "license_plate"]
        return {
            "person_count": response.get("person_count", 0),
            "vehicle_count": response.get("vehicle_count", 0),
            "dog_count": response.get("dog_count", 0),
            "labels": sorted({d["label"] for d in detections}),
            "plate_numbers": [p for p in plates if p and p.lower() != "string"],
            "detections": json.dumps(detections, ensure_ascii=False),
        }
