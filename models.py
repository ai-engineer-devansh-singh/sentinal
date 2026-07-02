"""Data models for SENTINEL.

Plain dataclasses for speed (created every tick) plus a few Pydantic models
for the HTTP API surface.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Vitals:
    t: float  # simulated minutes since patient start
    hr: float
    rr: float
    sbp: float
    dbp: float
    temp: float
    spo2: float

    @property
    def map(self) -> float:
        return self.dbp + (self.sbp - self.dbp) / 3.0


@dataclass
class LabResult:
    t: float
    lactate: float | None = None
    wbc: float | None = None
    creatinine: float | None = None
    bilirubin: float | None = None
    platelets: float | None = None  # x10^3/uL
    gcs: float | None = None  # 3..15


@dataclass
class NursingNote:
    t: float
    text: str


@dataclass
class AgentResult:
    name: str
    score: float  # 0..100
    confidence: float  # 0..1
    reasoning: str
    model_used: str
    llm_used: str  # model name, "none", or "local-stub"
    engine: str = "local"  # "grok" | "local"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AlertPayload:
    alert_id: str
    patient_id: str
    timestamp: str  # ISO
    sim_t: float
    tier: str  # GREEN | YELLOW | RED
    consensus: float
    agents: dict[str, dict[str, Any]]
    recommended_action: str
    differential_note: str
    uncertainty_flag: str
    carbon_cost_g: float
    models_used: list[str]
    cross_examination: list[dict[str, Any]] = field(default_factory=list)
    consensus_explanation: str = ""
    engine: str = "local"  # "grok" | "local"
    data_trust: dict[str, Any] = field(default_factory=dict)  # governance: input-trust summary


@dataclass
class PatientState:
    """Live, broadcast-ready view of one patient."""
    patient_id: str
    name: str
    age: int
    sex: str
    ward: str
    comorbidities: list[str]
    advance_directive: str

    connected: bool = False  # device patch streaming?
    sim_t: float = 0.0
    started_real: float = field(default_factory=time.time)

    vitals_now: dict[str, float] = field(default_factory=dict)
    vitals_history: list[dict[str, Any]] = field(default_factory=list)  # last N ticks for charts
    labs: dict[str, Any] = field(default_factory=dict)
    notes: list[dict[str, Any]] = field(default_factory=list)

    sofa_score: float = 0.0
    delta_sofa: float = 0.0
    qsofa_met: bool = False
    shock_index: float = 0.0

    features: dict[str, Any] = field(default_factory=dict)
    agent_results: dict[str, AgentResult] = field(default_factory=dict)
    consensus_ema: float | None = None  # smoothed consensus to suppress noise-driven tier flapping
    current_alert: AlertPayload | None = None
    tier: str = "GREEN"

    clinician_action: str | None = None  # ACCEPT | MODIFY | REJECT
    outcome: str = "MONITORING"  # MONITORING | INTERVENED | STABLE | DECEASED | DISCHARGED
    pending_alert: bool = False
    data_trust: dict[str, Any] = field(default_factory=dict)  # governance: rolling-window input trust

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["agent_results"] = {
            k: asdict(v) if not isinstance(v, dict) else v
            for k, v in self.agent_results.items()
        }
        if self.current_alert is not None:
            d["current_alert"] = asdict(self.current_alert)
        return d