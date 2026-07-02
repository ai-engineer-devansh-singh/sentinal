"""The four specialist agents + Carbon Budget Controller.

Stage-1 assessment is deterministic (rule + light ML heuristics) — the
"zero-LLM" path that handles 80% of inferences. Each agent returns a score
(0-100), confidence, and a concise structured reason. Rich Grok reasoning +
cross-examination is produced by the orchestrator's single synthesis call
(see orchestrator.py) when the Carbon Budget Controller permits.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .models import AgentResult, PatientState
from .profiles import PatientProfile
from .ml import ml as risk_model, note_classifier

# ── Carbon Budget Controller ──────────────────────────────────────────────

class CarbonBudgetController:
    """Pick the model tier based on case ambiguity (Sepsis-3 + carbon-aware)."""

    def select(self, ambiguity: float) -> dict[str, Any]:
        if ambiguity < 0.30:
            return {"llm": None, "model": None, "estimated_co2_g": 0.01, "tier": "ml_only"}
        if ambiguity < 0.70:
            return {"llm": "grok-4-fast", "model": "grok-4-fast", "estimated_co2_g": 0.15, "tier": "ml+sml"}
        return {"llm": "grok-4", "model": "grok-4", "estimated_co2_g": 1.20, "tier": "ml+large"}


# ── shared helpers ─────────────────────────────────────────────────────────

def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# ── SOFA Tracker ──────────────────────────────────────────────────────────

class SOFATracker:
    name = "sofa_tracker"

    def assess(self, state: PatientState, f: dict[str, Any], profile: PatientProfile) -> AgentResult:
        sofa = f.get("sofa_score", 0.0)
        d_base = f.get("delta_from_baseline", 0.0)  # monotonic trend vs patient baseline
        dsofa_tick = f.get("delta_sofa", 0.0)  # per-tick (display only)
        qsofa = f.get("qsofa_met", False)
        comps = f.get("sofa_components", {})
        shock = f.get("shock_index", 0.0)

        # Risk from absolute organ dysfunction + trend vs the patient's own
        # baseline (monotonic — no flap) + qSOFA + shock. We deliberately do NOT
        # score off the per-tick delta, which is noisy and oscillates.
        score = _clamp(sofa * 13 + max(0, d_base) * 16 + (18 if qsofa else 0) + max(0, shock - 0.7) * 28)
        borderline = 0 < abs(dsofa_tick) <= 1
        confidence = 0.95 if not borderline else 0.78

        tipped = ", ".join(k for k, v in comps.items() if v > 0) or "none"
        reason = (
            f"SOFA {sofa:.0f} (vs baseline {f.get('baseline_sofa', 0):.0f}, Δ {d_base:+.1f}); "
            f"organ systems tipped: {tipped}. qSOFA {'MET' if qsofa else 'not met'}. Shock index {shock:.2f}."
        )
        return AgentResult(
            name=self.name, score=round(score, 1), confidence=round(confidence, 2),
            reasoning=reason, model_used="sofa3_rule_engine",
            llm_used="none", engine="local",
            extra={"sofa": sofa, "delta_sofa": dsofa_tick, "delta_from_baseline": d_base, "qsofa": qsofa, "components": comps},
        )


# ── Differential Agent ────────────────────────────────────────────────────

class Differential:
    name = "differential"

    MIMIC_NOTES = {"pancreatitis": "Gallstone pancreatitis — epigastric pain, lipase elevated, lactate stable.",
                   "postop_stress": "Post-operative Day 2 inflammation — expected HR/CRP rise, lactate stable."}

    def assess(self, state: PatientState, f: dict[str, Any], profile: PatientProfile) -> AgentResult:
        lactate = f.get("lactate_latest")
        wbc = f.get("wbc_latest")
        temp = f.get("temp_now", 0.0)
        qsofa = f.get("qsofa_met", False)
        shock = f.get("shock_index", 0.0)
        dsofa = f.get("delta_sofa", 0.0)
        mimic = profile.mimic_tag

        signal = 0.0
        if lactate is not None and lactate > 2.0:
            signal += min(30, (lactate - 2.0) * 12)
        if qsofa:
            signal += 22
        if shock > 1.0:
            signal += min(18, (shock - 1.0) * 25)
        if temp > 38.5:
            signal += 10
        if wbc is not None and wbc > 12.0:
            signal += 8
        if dsofa >= 2:
            signal += 12

        ruled_out: list[str] = []
        caution: list[str] = []
        is_sepsis = True

        if mimic and (lactate is None or lactate < 1.5) and not qsofa and shock < 1.05:
            # mimic fits, sepsis criteria weak
            is_sepsis = False
            signal *= 0.25
            ruled_out.append(mimic)
            caution.append(self.MIMIC_NOTES.get(mimic, mimic))
        elif mimic:
            caution.append(f"{mimic} history present but sepsis criteria also met — review carefully.")

        score = _clamp(signal)
        confidence = 0.88 if (lactate is not None) else 0.6
        reason = (
            f"{'Sepsis likely' if is_sepsis else 'Sepsis NOT likely — mimic fits'}. "
            f"Lactate {lactate}, WBC {wbc}, qSOFA {qsofa}. "
            f"Mimics ruled out: {ruled_out or 'none'}. Cautions: {caution or 'none'}."
        )
        return AgentResult(
            name=self.name, score=round(score, 1), confidence=round(confidence, 2),
            reasoning=reason, model_used="mimic_rule_engine",
            llm_used="none", engine="local",
            extra={"is_sepsis_likely": is_sepsis, "mimics_ruled_out": ruled_out, "caution_flags": caution},
        )


# ── Predictor Agent ───────────────────────────────────────────────────────

class Predictor:
    name = "predictor"

    def assess(self, state: PatientState, f: dict[str, Any], profile: PatientProfile) -> AgentResult:
        rr_slope = f.get("rr_slope", 0.0)
        hr_slope = f.get("hr_slope", 0.0)
        sbp_delta = f.get("sbp_delta_1h", 0.0)
        temp_slope = f.get("temp_slope", 0.0)
        lactate = f.get("lactate_latest")

        # Continuous trajectory score (no step labels) so slope noise near a
        # threshold can't cause big score swings / tier flapping.
        accel = rr_slope * 1.0 + hr_slope * 0.5 + temp_slope * 20
        if accel > 0.6:
            traj = "accelerating"
        elif accel > 0.25:
            traj = "rising"
        elif accel < -0.1:
            traj = "improving"
        else:
            traj = "stable"

        # Trained XGBoost sepsis-risk model drives the score; trajectory refines.
        proba = risk_model.predict_proba(f)
        if proba >= 0:
            base = proba * 100
            if accel > 0.6:
                base += 5
            elif accel < -0.1:
                base -= 8
            score = _clamp(base)
            model_used = "xgboost_sepsis_v1"
            extra_ml = {"ml_proba": round(proba, 3)}
        else:
            # rule fallback if the model file is missing
            score = _clamp(20 + accel * 45 + max(0, -sbp_delta) * 0.8)
            if lactate is not None and lactate > 2.0:
                score = _clamp(score + (lactate - 2.0) * 8)
            model_used = "temporal_rules_fallback"
            extra_ml = {}

        confidence = 0.85
        ml_tag = f" XGBoost P(sepsis)={proba:.2f}." if proba >= 0 else ""
        reason = (
            f"Trajectory {traj} (rr_slope {rr_slope:+.2f}/min, hr_slope {hr_slope:+.2f}/min, "
            f"SBP Δ1h {sbp_delta:+.1f} mmHg, temp_slope {temp_slope:+.3f}/min). "
            f"Rate-of-change composite {accel:+.2f}.{ml_tag}"
        )
        return AgentResult(
            name=self.name, score=round(score, 1), confidence=round(confidence, 2),
            reasoning=reason, model_used=model_used,
            llm_used="none", engine="local",
            extra={**{"trajectory": traj, "rate_of_change": round(accel, 3), "prediction_horizon": "4h"}, **extra_ml},
        )


# ── Narrative Analyst ─────────────────────────────────────────────────────

CONCERN_WORDS = {"unwell", "confused", "drowsy", "clammy", "low", "worse", "septic", "deteriorat",
                 "cold", "oliguric", "awaiting", "distress", "hypotens"}
NEG_WORDS = {"no fever", "comfortable", "stable", "improved", "afebrile", "recovering", "well"}


class Narrative:
    name = "narrative"

    def assess(self, state: PatientState, f: dict[str, Any], profile: PatientProfile) -> AgentResult:
        notes = state.notes[-3:]
        concerns: list[str] = []
        contradictions: list[str] = []
        corroborates = True

        text = " ".join(n.get("text", "") for n in notes).lower()
        for w in CONCERN_WORDS:
            if w in text:
                concerns.append(w)
        for w in NEG_WORDS:
            if w in text:
                contradictions.append(w)

        temp = f.get("temp_now", 0.0)
        if "no fever" in text and temp >= 38.5:
            contradictions.append("note says 'no fever' vs structured temp %.1f°C — antipyretic use?" % temp)
        if "comfortable" in text and f.get("shock_index", 0) > 1.1:
            contradictions.append("note 'comfortable' vs shock_index %.2f" % f.get("shock_index", 0))

        # Trained TF-IDF + LogisticRegression note-concern classifier drives
        # the score; keyword extraction is kept for the human-readable evidence.
        # Classify each note separately and take the max concern so one benign
        # early note doesn't dilute a clearly deteriorating later note.
        proba = -1.0
        if notes:
            per_note = [note_classifier.predict_concern(n.get("text", "")) for n in notes]
            per_note = [p for p in per_note if p >= 0]
            if per_note:
                proba = max(per_note)
        if proba >= 0:
            score = _clamp(proba * 100 + (15 if contradictions else 0))
            if not notes:
                score = 10
            model_used = "tfidf_logreg_ner_v1"
            extra_ml = {"ml_concern_proba": round(proba, 3)}
        else:
            score = _clamp(len(concerns) * 18 + (15 if contradictions else 0))
            if not notes:
                score = 10
            model_used = "keyword_ner_fallback"
            extra_ml = {}
        confidence = 0.78 if notes else 0.4
        ml_tag = f" TF-IDF P(concern)={proba:.2f}." if proba >= 0 else ""
        reason = (
            f"Notes reviewed: {len(notes)}. Concerns: {concerns or 'none'}. "
            f"Corroborates structured data: {corroborates}. "
            f"Contradictions: {contradictions or 'none'}.{ml_tag}"
        )
        return AgentResult(
            name=self.name, score=round(score, 1), confidence=round(confidence, 2),
            reasoning=reason, model_used=model_used,
            llm_used="none", engine="local",
            extra={**{"concerns": concerns, "contradictions": contradictions, "corroborates": corroborates}, **extra_ml},
        )


# registry
AGENTS = [SOFATracker(), Differential(), Predictor(), Narrative()]
AGENT_NAMES = [a.name for a in AGENTS]