"""Data governance & input-trust layer.

A medical AI system must not make confident clinical calls on untrustworthy
input. This module sits *on top of* the rolling feature window and answers a
single question every tick: **how much should downstream agents trust the data
this patient's features are built on?**

It is deliberately separate from the feature store so the trust computation
can evolve (new artifact detectors, device attestation, lab-chain provenance)
without touching feature math. The output is a streaming **confidence score
(0..1)** plus a human-readable breakdown and a grade (HIGH / MEDIUM / LOW) —
carried all the way to the dashboard so a clinician sees input trustworthiness
next to the sepsis risk, and into the orchestrator so low trust raises
ambiguity (→ more LLM/human review) and caps agent confidence.

Two levels of check:

  * **per-reading** (`validate`) — physiologic plausibility + spike vs the
    previous reading. Tallied into a running pass-rate.
  * **per-window** (`assess`) — coverage, freshness, plausibility pass-rate,
    stability (flatline detection), lab staleness and source trust, blended
    into the confidence score.

Nothing here is a clinical device; the thresholds are conservative and
auditable. The point is governance plumbing: every reading is attested,
flagged, and its trust made visible.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from .models import Vitals

# ── thresholds (auditable, conservative) ───────────────────────────────────

# Physiologic plausibility bounds. A reading outside these is non-physiologic
# → almost certainly a sensor/cable artifact or a units error, never real.
PLAUSIBILITY: dict[str, tuple[float, float]] = {
    "hr": (25.0, 220.0),
    "rr": (4.0, 40.0),
    "sbp": (40.0, 250.0),
    "dbp": (20.0, 150.0),
    "temp": (30.0, 44.0),    # °C
    "spo2": (50.0, 100.0),
}

# Per-tick spike limits: a jump larger than this between consecutive readings
# is an artifact (the simulator changes vitals gradually; real patches do too).
SPIKE_LIMIT: dict[str, float] = {
    "hr": 35.0, "rr": 12.0, "sbp": 40.0, "dbp": 25.0, "temp": 1.5, "spo2": 8.0,
}

# Flatline detection: a real patch reading always has a little noise. If a key
# vital's std over the last N readings is below this, the sensor is stuck.
FLATLINE_N = 6
FLATLINE_STD: dict[str, float] = {"hr": 0.5, "sbp": 0.6, "spo2": 0.2}

# Window coverage / freshness
WINDOW_MIN = 15.0          # matches feature_store.WINDOW
MAX_GAP_MIN = 2.0          # gap (sim-min) beyond which freshness degrades
LAB_STALE_MIN = 30.0       # lab age beyond which lab trust degrades

# Source trust baselines — what we'd back a device attestation with in a pilot.
# Wearable patch vitals are consumer-grade; POCT (i-STAT) labs are trusted but
# sporadic. These are the floor; the rolling checks adjust upward/downward.
SOURCE_TRUST_PATCH = 0.92
SOURCE_TRUST_POCT = 0.95

# Confidence grade boundaries
CONF_HIGH = 0.85
CONF_MED = 0.60

# Weights for the blended confidence (sum to 1.0)
_W = {"plausibility": 0.30, "coverage": 0.25, "freshness": 0.20,
      "stability": 0.15, "source": 0.10}


@dataclass
class ReadingReport:
    plausible: bool
    violations: list[str] = field(default_factory=list)
    spike_flags: list[str] = field(default_factory=list)


class PatientDataGovernance:
    """Per-patient rolling-window data-trust assessor.

    Held by ``PatientFeatures``. ``validate`` is called on every incoming
    reading; ``assess`` is called when features are computed, over the rolling
    window, and returns the trust summary injected into the feature dict.
    """

    def __init__(self, source_trust: float = SOURCE_TRUST_PATCH) -> None:
        self.source_trust = source_trust
        self._prev: Vitals | None = None
        # running plausibility tally across the patient's whole stay
        self.samples_total = 0
        self.samples_plausible = 0

    # ── per-reading ──────────────────────────────────────────────────────
    def validate(self, v: Vitals) -> ReadingReport:
        violations: list[str] = []
        spike_flags: list[str] = []
        for key, (lo, hi) in PLAUSIBILITY.items():
            val = getattr(v, key)
            if not (lo <= val <= hi):
                violations.append(f"{key.upper()}={val} outside {lo}-{hi}")
        if self._prev is not None:
            for key, limit in SPIKE_LIMIT.items():
                dv = getattr(v, key) - getattr(self._prev, key)
                if abs(dv) > limit:
                    spike_flags.append(f"{key.upper()} Δ{dv:+.1f} exceeds ±{limit}/tick")
        self._prev = v
        self.samples_total += 1
        plausible = not violations
        if plausible:
            self.samples_plausible += 1
        return ReadingReport(plausible=plausible, violations=violations,
                             spike_flags=spike_flags)

    # ── per-window ───────────────────────────────────────────────────────
    def assess(self, window: list[Vitals], t_now: float,
               last_lab_t: float | None) -> dict[str, Any]:
        flags: list[str] = []

        if not window:
            return {
                "data_confidence": 0.0, "data_trust_grade": "NO DATA",
                "data_quality_flags": ["no readings in window"],
                "coverage": 0.0, "freshness_min": None,
                "plausibility_pass": 0.0, "stability": 0.0,
                "lab_age_min": None, "source_trust": self.source_trust,
            }

        # coverage: fraction of the window duration actually covered by data
        span = window[-1].t - window[0].t
        coverage = max(0.0, min(1.0, span / WINDOW_MIN))
        if coverage < 0.5:
            flags.append(f"low window coverage ({coverage*100:.0f}%)")

        # freshness: gap since the most recent reading
        gap = t_now - window[-1].t
        freshness = max(0.0, 1.0 - min(1.0, gap / MAX_GAP_MIN)) if gap >= 0 else 1.0
        if gap > MAX_GAP_MIN:
            flags.append(f"stale feed — last reading {gap:.1f} min ago")

        # plausibility pass-rate (running stay-wide tally; robust to a window
        # that's too short for a meaningful per-window rate)
        plausibility_pass = (self.samples_plausible / self.samples_total
                             if self.samples_total else 1.0)
        if plausibility_pass < 1.0:
            flags.append(f"{self.samples_total - self.samples_plausible} implausible reading(s) this stay")

        # stability / flatline detection on HR (the most continuously variable
        # vital). Genuine stability still varies >0.5 bpm with patch noise, so
        # a true flatline means the sensor is stuck, not that the patient is.
        stability = 1.0
        if len(window) >= FLATLINE_N:
            recent_hr = [v.hr for v in window[-FLATLINE_N:]]
            if statistics.pstdev(recent_hr) < FLATLINE_STD["hr"]:
                stability = 0.4
                flags.append("HR flatline suspected — sensor stuck?")

        # lab staleness: lactate/creatinine etc. drive the SOFA renal/liver
        # components, so deciding sepsis on a lab >30 min old is a governance
        # concern in its own right.
        lab_age_min: float | None = None
        lab_fresh = 1.0
        if last_lab_t is not None:
            lab_age_min = max(0.0, t_now - last_lab_t)
            if lab_age_min > LAB_STALE_MIN:
                lab_fresh = max(0.0, 1.0 - min(1.0, (lab_age_min - LAB_STALE_MIN) / 60.0))
                flags.append(f"labs stale — last POCT {lab_age_min:.0f} min ago")
        # labs are a secondary signal; fold lab_fresh into source trust softly
        effective_source = 0.5 * self.source_trust + 0.5 * lab_fresh * SOURCE_TRUST_POCT

        confidence = (
            _W["plausibility"] * plausibility_pass
            + _W["coverage"] * coverage
            + _W["freshness"] * freshness
            + _W["stability"] * stability
            + _W["source"] * effective_source
        )
        confidence = round(max(0.0, min(1.0, confidence)), 3)

        if confidence >= CONF_HIGH:
            grade = "HIGH"
        elif confidence >= CONF_MED:
            grade = "MEDIUM"
        else:
            grade = "LOW"
        if grade != "HIGH" and not flags:
            flags.append(f"composite confidence {confidence:.2f} below HIGH threshold")

        return {
            "data_confidence": confidence,
            "data_trust_grade": grade,
            "data_quality_flags": flags,
            "coverage": round(coverage, 3),
            "freshness_min": round(gap, 2),
            "plausibility_pass": round(plausibility_pass, 3),
            "stability": round(stability, 2),
            "lab_age_min": round(lab_age_min, 1) if lab_age_min is not None else None,
            "source_trust": round(effective_source, 3),
        }