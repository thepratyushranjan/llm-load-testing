import json
from typing import Any, Dict, List, Optional, Tuple

from pydantic import TypeAdapter

from src.core import prompts
from src.models.job import Job
from src.schemas.responses import OfficeAiInfoResponse, OfficeModelOutput
from src.services.task_base import BaseTask, event_context, fill, media_event_context


class AiInfoTask(BaseTask):
    """AI-info processing: office vertical audit of the scene"""

    name = "ai_info"
    output_schema = OfficeModelOutput
    response_schema = TypeAdapter(OfficeAiInfoResponse)
    zone_default = "office"
    temperature = 0.4
    max_tokens = 4096

    def prompts(self, job: Job) -> Tuple[Optional[str], str]:
        meta, data = event_context(job)
        detections = data.get("detections", [])
        user = fill(prompts.OFFICE_USER_TEMPLATE, {
            "company_id": meta.get("company_id"),
            "device_id": meta.get("device_id", "N/A"),
            "site_id": meta.get("site_id"),
            "cam_id": meta.get("cam_id", "N/A"),
            "cam_name": meta.get("cam_name", "N/A"),
            "zone_type": meta.get("zone_type", "office"),
            "timestamp": meta.get("ts", "N/A"),
            "detection_count": len(detections),
            "people_count": data.get("people_count", 0),
            "triggers": data.get("triggers", []),
            "detections": json.dumps(detections, indent=2),
            "recognitions": json.dumps(data.get("recognitions", []), indent=2),
            "media_count": len(job.media),
        })
        return prompts.OFFICE_SYSTEM, user

    # ── Mirrors BaseValidationService helpers + AIOfficeValidationService._format_response
    #    (roboi-main @ e176e6e5)

    @staticmethod
    def _score_0_10_or_minus_one(val: Any, default: int = -1) -> int:
        try:
            score = int(val)
        except (TypeError, ValueError):
            return default
        if score == -1:
            return -1
        if score < 0:
            return 0
        if score > 10:
            return 10
        return score

    @staticmethod
    def _pick(src: dict, keys: List[str], default: Any) -> dict:
        return {k: src.get(k, default) for k in keys}

    @staticmethod
    def _metadata_block(context: Dict, metadata: dict, *, include_analysis_mode: bool = True) -> dict:
        block = {
            "company_id": context["company_id"],
            "device_id": context["device_id"],
            "cam_id": context["cam_id"],
            "cam_name": context["cam_name"],
            "site_name": context["site_name"],
            "site_id": context["site_id"],
            "event_timestamp": context["processed_at"],
            "latitude": context["latitude"],
            "longitude": context["longitude"],
            "country": context["country"],
            "state": context["state"],
            "district": context["district"],
            "clip_duration_seconds": metadata.get("clip_duration_seconds", 20),
            "media_analyzed": metadata.get("media_analyzed", len(context["media_files"])),
        }
        if include_analysis_mode:
            block["analysis_mode"] = metadata.get("analysis_mode", "standard")
        return block

    @staticmethod
    def _aggregates_block(aggregates: dict) -> dict:
        return {
            "critical_issues_count": aggregates.get("critical_issues_count", 0),
            "high_issues_count": aggregates.get("high_issues_count", 0),
            "medium_issues_count": aggregates.get("medium_issues_count", 0),
            "low_issues_count": aggregates.get("low_issues_count", 0),
            "total_issues_count": aggregates.get("total_issues_count", 0),
            "overall_compliance_pct": aggregates.get("overall_compliance_pct", 100),
        }

    @staticmethod
    def _classification_block(classification: dict) -> dict:
        return {
            "status": classification.get("status", "safe"),
            "utilization": classification.get("utilization", "low"),
        }

    def format_response(self, job: Job, ai_validation: Dict) -> Dict:
        context = media_event_context(job, default_zone=self.zone_default)
        metadata = ai_validation.get("metadata", {})
        critical_alerts = ai_validation.get("critical_alerts", {})
        high_alerts = ai_validation.get("high_alerts", {})
        medium_alerts = ai_validation.get("medium_alerts", {})
        low_alerts = ai_validation.get("low_alerts", {})
        scores = ai_validation.get("scores", {})
        counts = ai_validation.get("counts", {})
        zone_analysis = ai_validation.get("zone_analysis", {})
        aggregates = ai_validation.get("aggregates", {})
        sop = ai_validation.get("sop", {})

        return {
            "metadata": self._metadata_block(context, metadata),
            "critical_alerts": self._pick(critical_alerts, [
                "unauthorized_zone_entry", "fire_smoke_detected", "emergency_exit_blocked",
                "mob_gathering", "physical_altercation", "credential_sharing",
                "attendance_mismatch", "server_room_entry", "lone_worker_hazard",
                "workplace_violence", "issue_timestamp_offset",
            ], 0),
            "high_alerts": self._pick(high_alerts, [
                "tailgating_entry", "fire_equipment_obstructed", "safety_glasses_missing",
                "fall_incident", "suspicious_concealment", "abandoned_object",
                "camera_offline", "early_departure", "lone_worker_extended",
                "visitor_without_escort", "property_removal", "contractor_ppe_missing",
            ], -1),
            "medium_alerts": self._pick(medium_alerts, [
                "trip_hazard_object", "smoking_non_designated", "id_badge_not_visible",
                "safety_signage_obstructed", "first_aid_inaccessible", "extended_break",
                "workstation_absence", "visitor_badge_missing",
            ], -1),
            "low_alerts": self._pick(low_alerts, [
                "improper_waste_disposal", "uniform_non_compliance",
            ], -1),
            "scores": {
                "uniform_compliance_score": scores.get("uniform_compliance_score", 10),
                "safety_score": scores.get("safety_score", 10),
                "cleanliness_score": scores.get("cleanliness_score", 10),
                "access_compliance_score": scores.get("access_compliance_score", 10),
                "overall_compliance_score": scores.get("overall_compliance_score", 10),
            },
            "counts": self._pick(counts, [
                "people_count", "employee_count", "visitor_count",
                "contractor_count", "unidentified_count",
            ], 0),
            "zone_analysis": {
                "zone_name": zone_analysis.get("zone_name", context["zone_type"]),
                "zone_type": zone_analysis.get("zone_type", "office"),
                "occupancy_level": zone_analysis.get("occupancy_level", "low"),
                "authorized_access_only": zone_analysis.get("authorized_access_only", False),
            },
            "face_matches": ai_validation.get("face_matches", []),
            "classification": self._classification_block(ai_validation.get("classification", {})),
            "triggers": ai_validation.get("triggers", []),
            "event_roi": ai_validation.get("event_roi", []),
            "ai_summary": ai_validation.get("ai_summary", ""),
            "aggregates": self._aggregates_block(aggregates),
            "sop": {
                "sop_access_control": self._score_0_10_or_minus_one(
                    sop.get("sop_access_control", -1)
                ),
                "sop_safety_equipment": self._score_0_10_or_minus_one(
                    sop.get("sop_safety_equipment", -1)
                ),
                "sop_uniform_compliance": self._score_0_10_or_minus_one(
                    sop.get("sop_uniform_compliance", scores.get("uniform_compliance_score", -1))
                ),
                "sop_visitor_management": self._score_0_10_or_minus_one(
                    sop.get("sop_visitor_management", -1)
                ),
                "sop_emergency_readiness": self._score_0_10_or_minus_one(
                    sop.get("sop_emergency_readiness", scores.get("safety_score", -1))
                ),
            },
        }

    def key_fields(self, response: Dict) -> Dict[str, Any]:
        flagged = set()
        for tier in ("critical_alerts", "high_alerts", "medium_alerts", "low_alerts"):
            flagged |= {kpi for kpi, value in response[tier].items() if value == 1}
        return {
            "alert_valid": bool(flagged),
            "plate_number": None,
            "labels": sorted(flagged),
            "severity": response["classification"]["status"],
        }
