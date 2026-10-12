import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

FUNCTIONS = ("event", "ai_info", "extraction")
SPLITS = ("trained", "untrained")


class Step(BaseModel):
    """One load level: `batch_per_tick` jobs every TICK_SECONDS (or `rps` jobs per second) for `duration_s`"""
    name: str = ""
    duration_s: float = Field(gt=0)
    batch_per_tick: Optional[float] = Field(default=None, ge=0)
    rps: Optional[float] = Field(default=None, ge=0)
    function_mix: Optional[Dict[str, float]] = None  # overrides the profile mix for this step
    drain_after: bool = True  # wait for the queue to empty + cooldown before the next step

    @model_validator(mode="after")
    def _one_rate(self):
        if (self.batch_per_tick is None) == (self.rps is None):
            raise ValueError("give exactly one of batch_per_tick or rps")
        return self

    def per_tick(self, tick_s: float) -> float:
        return self.batch_per_tick if self.batch_per_tick is not None else self.rps * tick_s


def _check_mix(mix: Optional[Dict[str, float]], allowed, what: str) -> None:
    if mix is None:
        return
    unknown = set(mix) - set(allowed)
    if unknown:
        raise ValueError(f"{what}: unknown keys {sorted(unknown)} (allowed: {list(allowed)})")
    if any(v < 0 for v in mix.values()) or sum(mix.values()) <= 0:
        raise ValueError(f"{what}: weights must be >= 0 and not all zero")


class Profile(BaseModel):
    name: str
    description: str = ""
    steps: List[Step] = Field(min_length=1)
    function_mix: Dict[str, float] = Field(default_factory=lambda: {f: 1.0 for f in FUNCTIONS})
    split_mix: Optional[Dict[str, float]] = None  # None = any split
    cooldown_s: float = Field(default=60, ge=0)
    job_deadline_s: Optional[float] = Field(default=None, gt=0)
    # auto-stop overrides (None = use settings)
    auto_stop: Optional[bool] = None
    sla_p95_ms: Optional[float] = None
    max_error_rate: Optional[float] = None
    queue_growth_window_s: Optional[float] = None

    @model_validator(mode="after")
    def _check(self):
        _check_mix(self.function_mix, FUNCTIONS, "function_mix")
        _check_mix(self.split_mix, SPLITS, "split_mix")
        for i, step in enumerate(self.steps):
            _check_mix(step.function_mix, FUNCTIONS, f"steps[{i}].function_mix")
        return self

    @property
    def duration_s(self) -> float:
        return sum(s.duration_s for s in self.steps) + self.cooldown_s * sum(
            s.drain_after for s in self.steps[:-1])

    def scaled(self, duration_scale: float = 1.0, rate_scale: float = 1.0) -> "Profile":
        """Shorter / lighter copy, e.g. duration_scale=0.1 for a quick rehearsal"""
        steps = [s.model_copy(update={
            "duration_s": s.duration_s * duration_scale,
            "batch_per_tick": None if s.batch_per_tick is None else s.batch_per_tick * rate_scale,
            "rps": None if s.rps is None else s.rps * rate_scale,
        }) for s in self.steps]
        return self.model_copy(update={"steps": steps, "cooldown_s": self.cooldown_s * duration_scale})


def _steps(*levels, minutes: float, drain: bool = True) -> List[Step]:
    return [Step(name=f"{b}/tick", duration_s=minutes * 60, batch_per_tick=b, drain_after=drain) for b in levels]


PROFILES: Dict[str, Profile] = {p.name: p for p in [
    Profile(name="smoke", description="1 job per tick for 5 min: does everything work end to end, is the JSON valid?",
            steps=_steps(1, minutes=5)),
    Profile(name="baseline", description="1 job per tick for 20 min: latency with no contention",
            steps=_steps(1, minutes=20)),
    Profile(name="ramp", description="1 → 5 → 10 → 20 → 35 jobs per tick, 10 min each: max sustainable RPS",
            steps=_steps(1, 5, 10, 20, 35, minutes=10)),
    Profile(name="spike", description="baseline → 10x for 2 min → baseline: how fast the queue recovers",
            steps=[Step(name="before", duration_s=300, batch_per_tick=2, drain_after=False),
                   Step(name="spike 10x", duration_s=120, batch_per_tick=20, drain_after=False),
                   Step(name="after", duration_s=600, batch_per_tick=2)]),
    Profile(name="soak", description="steady load for 4 h: leaks and stability (set --rate to ~70% of the ramp max)",
            steps=[Step(name="soak", duration_s=4 * 3600, batch_per_tick=10)]),
    Profile(name="mixed", description="all functions at the production mix for 30 min: capacity planning number",
            function_mix={"event": 0.4, "ai_info": 0.3, "extraction": 0.3},
            steps=[Step(name="mixed", duration_s=1800, batch_per_tick=5)]),
]}


def load_profile(name: Optional[str] = None, path: Optional[str] = None) -> Profile:
    if path:
        return Profile.model_validate(yaml.safe_load(Path(path).read_text()))
    if name not in PROFILES:
        raise KeyError(f"unknown profile {name!r}; built-in: {', '.join(PROFILES)}")
    return PROFILES[name]


class RunSpec(BaseModel):
    """What the CLI asks the scheduler to run (stored in Redis)"""
    run_id: str
    machine_id: str
    profile: Profile
    functions: List[str]
    sample: Optional[int] = None  # use only this many cases per function
    notes: str = ""
    submitted_at: float = Field(default_factory=time.time)

    @staticmethod
    def new_id(profile: str) -> str:
        return f"{time.strftime('%Y%m%d-%H%M%S')}-{profile}-{uuid.uuid4().hex[:4]}"
