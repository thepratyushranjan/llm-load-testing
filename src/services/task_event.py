import json
from typing import Any, Dict, List, Optional, Tuple

from src.core import prompts
from src.models.job import Job
from src.schemas.responses import EventModelOutput, EventValidationResponse
from src.services.task_base import BaseTask, event_context, fill, media_event_context


def kpi_section(triggers: List[Any]) -> str:
    """Same as production _render_kpi_prompt, from the pinned per-trigger lines"""
    unique: List[str] = []
    for t in triggers or []:
        t = str(t or "").strip().lower()
        if t and t not in unique:
            unique.append(t)
    if not unique:
        return prompts.EVENT_KPI_NO_TRIGGERS
    lines = [prompts.EVENT_KPI_HEADER]
    for t in unique:
        lines.append(prompts.EVENT_KPI_LINES.get(t) or fill(prompts.EVENT_KPI_UNKNOWN, {"trigger": t}))
    return "\n".join(lines)


class EventTask(BaseTask):
    """Event processing: validate claimed triggers against the media"""

    name = "event"
    output_schema = EventModelOutput
    response_schema = EventValidationResponse
    temperature = 0.5
    max_tokens = 2048

    def prompts(self, job: Job) -> Tuple[Optional[str], str]:
        meta, data = event_context(job)
        system = "\n\n".join([prompts.EVENT_SYSTEM_HEADER, kpi_section(data.get("triggers", [])),
                              prompts.EVENT_OUTPUT_FORMAT])
        user = fill(prompts.EVENT_USER_TEMPLATE, {
            "company_id": meta.get("company_id"),
            "site_id": meta.get("site_id"),
            "cam_id": meta.get("cam_id"),
            "cam_name": meta.get("cam_name"),
            "site_name": meta.get("site_name"),
            "zone": meta.get("zone_names", []),
            "event_timestamp": meta.get("ts"),
            "people_count": data.get("people_count"),
            "vehicle_count": data.get("vehicle_count", 0),
            "animal_count": data.get("animal_count", 0),
            "triggers": data.get("triggers"),
            "media_count": len(job.media),
            "detections": json.dumps(data.get("detections", []), indent=2),
            "recognitions": json.dumps(data.get("recognitions", []), indent=2),
        })
        return system, user

    def format_response(self, job: Job, ai_validation: Dict) -> Dict:
        """Mirrors EventValidationService._format_response (roboi-main @ e176e6e5)"""
        context = media_event_context(job)
        is_valid = ai_validation.get('alert_valid', False)
        base_info = {
            "processed_at": context["processed_at"],
            "cam_name": context["cam_name"],
            "event_folder": context["event_folder_uri"],
            "media_files": context["media_files"]
        }

        if is_valid:
            return {
                "alert_status": "True",
                "alert_details": {
                    **base_info,
                    "status": context["status"],
                    "people_count": context["people_count"],
                    "triggers": context["triggers"],
                    "capture_triggered": context["capture_triggered"],
                    "detected_labels": context["detected_labels"],
                    "display_label": context["display_label"],
                    "identity": context["identity"],
                },
                "validation_summary": ai_validation.get('validation_summary'),
                "ai_verified_people_count": ai_validation.get('actual_people_count'),
                "media_analyzed": ai_validation.get('media_analyzed', len(context["media_files"])),
                "event_roi": ai_validation.get('event_roi', []),
            }

        return {
            "alert_status": "False",
            "validation_summary": ai_validation.get('validation_summary'),
            "event_reference": base_info,
            "claimed_data": {
                "people_count": context["people_count"],
                "triggers": context["triggers"],
                "identity": context["identity"]
            },
            "ai_analysis": {
                "actual_people_count": ai_validation.get('actual_people_count', 0),
                "verdict": ai_validation.get('verdict'),
                "what_happened": ai_validation.get('what_happened'),
                "why_it_happened": ai_validation.get('why_it_happened'),
                "recommendation": ai_validation.get('recommendation') or ai_validation.get('suggested_fix')
            },
            "media_analyzed": ai_validation.get('media_analyzed', len(context["media_files"])),
            "event_roi": [],
        }

    def key_fields(self, response: Dict) -> Dict[str, Any]:
        return {
            "alert_valid": response["alert_status"] == "True",
            "plate_number": None,
            "labels": sorted({roi["label"] for roi in response["event_roi"]}),
            "severity": None,
        }
