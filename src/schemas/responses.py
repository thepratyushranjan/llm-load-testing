"""Response schemas for the three load-test functions

Event and AI-info mirror the dict roboi-ai-service returns from `_format_response`
(roboi-main @ e176e6e5: event_validation_service.py, ai_office_validation_service.py,
base_validation_service.py, context.py). Extraction mirrors the parsed JSON that
search_analyzer.extract_attributes_from_image returns unchanged.
"""
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from typing import Annotated, Any, Dict, List, Literal, Optional, Union


class EventRoi(BaseModel):
    """Bounding box on a normalised 0-1000 grid: [ymin, xmin, ymax, xmax]"""
    model_config = ConfigDict(extra="allow")

    label: str
    box_2d: List[float] = Field(min_length=4, max_length=4)
    confidence: float = Field(ge=0, le=1)


# ── Event processing: EventValidationService._format_response ──────────────

class EventAlertDetails(BaseModel):
    processed_at: Union[str, int, None] = None
    cam_name: Optional[str] = None
    event_folder: str
    media_files: List[str]
    status: Optional[str] = None
    people_count: Optional[int] = None
    triggers: Optional[List[Any]] = None
    capture_triggered: Optional[bool] = None
    detected_labels: List[Any]
    display_label: Optional[str] = None
    identity: Optional[str] = None


class EventValidAlertResponse(BaseModel):
    alert_status: Literal["True"]
    alert_details: EventAlertDetails
    validation_summary: str
    ai_verified_people_count: Optional[int] = None
    media_analyzed: int
    event_roi: List[EventRoi]


class EventReference(BaseModel):
    processed_at: Union[str, int, None] = None
    cam_name: Optional[str] = None
    event_folder: str
    media_files: List[str]


class EventClaimedData(BaseModel):
    people_count: Optional[int] = None
    triggers: Optional[List[Any]] = None
    identity: Optional[str] = None


class EventAiAnalysis(BaseModel):
    actual_people_count: Optional[int] = None
    verdict: Optional[str] = None
    what_happened: Optional[str] = None
    why_it_happened: Optional[str] = None
    recommendation: Optional[str] = None


class EventFalsePositiveResponse(BaseModel):
    alert_status: Literal["False"]
    validation_summary: str
    event_reference: EventReference
    claimed_data: EventClaimedData
    ai_analysis: EventAiAnalysis
    media_analyzed: int
    event_roi: List[EventRoi]


# Either shape, picked by alert_status
EventValidationResponse = TypeAdapter(Annotated[
    Union[EventValidAlertResponse, EventFalsePositiveResponse], Field(discriminator="alert_status")
])


# ── AI-info processing: AIOfficeValidationService._format_response ─────────

class OfficeMetadata(BaseModel):
    company_id: Optional[str] = None
    device_id: Optional[str] = None
    cam_id: Optional[str] = None
    cam_name: Optional[str] = None
    site_name: Optional[str] = None
    site_id: Optional[str] = None
    event_timestamp: Union[str, int, None] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    country: Optional[str] = None
    state: Optional[str] = None
    district: Optional[str] = None
    clip_duration_seconds: float
    media_analyzed: int
    analysis_mode: str


class OfficeCriticalAlerts(BaseModel):
    unauthorized_zone_entry: int
    fire_smoke_detected: int
    emergency_exit_blocked: int
    mob_gathering: int
    physical_altercation: int
    credential_sharing: int
    attendance_mismatch: int
    server_room_entry: int
    lone_worker_hazard: int
    workplace_violence: int
    issue_timestamp_offset: int


class OfficeHighAlerts(BaseModel):
    tailgating_entry: int
    fire_equipment_obstructed: int
    safety_glasses_missing: int
    fall_incident: int
    suspicious_concealment: int
    abandoned_object: int
    camera_offline: int
    early_departure: int
    lone_worker_extended: int
    visitor_without_escort: int
    property_removal: int
    contractor_ppe_missing: int


class OfficeMediumAlerts(BaseModel):
    trip_hazard_object: int
    smoking_non_designated: int
    id_badge_not_visible: int
    safety_signage_obstructed: int
    first_aid_inaccessible: int
    extended_break: int
    workstation_absence: int
    visitor_badge_missing: int


class OfficeLowAlerts(BaseModel):
    improper_waste_disposal: int
    uniform_non_compliance: int


class OfficeScores(BaseModel):
    uniform_compliance_score: float
    safety_score: float
    cleanliness_score: float
    access_compliance_score: float
    overall_compliance_score: float


class OfficeCounts(BaseModel):
    people_count: int
    employee_count: int
    visitor_count: int
    contractor_count: int
    unidentified_count: int


class OfficeZoneAnalysis(BaseModel):
    zone_name: Optional[str] = None
    zone_type: str
    occupancy_level: str
    authorized_access_only: bool


class OfficeClassification(BaseModel):
    status: str
    utilization: str


class OfficeAggregates(BaseModel):
    critical_issues_count: int
    high_issues_count: int
    medium_issues_count: int
    low_issues_count: int
    total_issues_count: int
    overall_compliance_pct: float


class OfficeSop(BaseModel):
    sop_access_control: int
    sop_safety_equipment: int
    sop_uniform_compliance: int
    sop_visitor_management: int
    sop_emergency_readiness: int


class OfficeAiInfoResponse(BaseModel):
    metadata: OfficeMetadata
    critical_alerts: OfficeCriticalAlerts
    high_alerts: OfficeHighAlerts
    medium_alerts: OfficeMediumAlerts
    low_alerts: OfficeLowAlerts
    scores: OfficeScores
    counts: OfficeCounts
    zone_analysis: OfficeZoneAnalysis
    face_matches: List[Any]
    classification: OfficeClassification
    triggers: List[Any]
    event_roi: List[EventRoi]
    ai_summary: str
    aggregates: OfficeAggregates
    sop: OfficeSop


# ── Image attribute extraction: returned as parsed, no formatting ──────────

class ExtractionDetection(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str
    attributes: Dict[str, Any] = Field(default_factory=dict)


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    person_count: int = 0
    vehicle_count: int = 0
    dog_count: int = 0
    detections: List[ExtractionDetection]


# ── Raw model output, exactly the "REQUIRED JSON SHAPE" each prompt asks for ──
# Used only to constrain generation when guided JSON is on.

class EventModelOutput(BaseModel):
    alert_valid: bool
    actual_people_count: int
    media_analyzed: int
    validation_summary: str
    verdict: str
    what_happened: str
    why_it_happened: str
    recommendation: str
    event_roi: List[EventRoi]


class OfficeModelMetadata(BaseModel):
    clip_duration_seconds: float
    media_analyzed: int
    analysis_mode: str


class OfficeModelHighAlerts(OfficeHighAlerts):
    obscene_activity_detected: int


class OfficeModelZoneAnalysis(BaseModel):
    zone_name: str
    zone_type: str
    occupancy_level: str
    authorized_access_only: bool


class OfficeModelFaceMatch(BaseModel):
    identity_id: str
    confidence: float
    location: str
    timestamp_offset: float


class OfficeModelOutput(BaseModel):
    metadata: OfficeModelMetadata
    critical_alerts: OfficeCriticalAlerts
    high_alerts: OfficeModelHighAlerts
    medium_alerts: OfficeMediumAlerts
    low_alerts: OfficeLowAlerts
    scores: OfficeScores
    counts: OfficeCounts
    zone_analysis: OfficeModelZoneAnalysis
    face_matches: List[OfficeModelFaceMatch]
    classification: OfficeClassification
    triggers: List[str]
    event_roi: List[EventRoi]
    ai_summary: str
    aggregates: OfficeAggregates
    sop: OfficeSop
