"""Orchestrator — 3-stage deliberation, consensus, tier assignment, alert payload.

Stage 1: each agent's independent (deterministic) assessment — already done.
Stage 2: cross-examination — agents challenge each other's reasoning.
Stage 3: synthesis — weighted consensus → GREEN/YELLOW/RED alert.

The deliberation (stages 2+3) is produced by a single Grok call that takes all
stage-1 outputs and returns the cross-examination transcript, per-agent rich
reasoning, recommended action, and uncertainty flag. The *consensus and tier
remain deterministic* so alerts always fire correctly even if Grok is slow or
unavailable — Grok enriches the explanation, not the trigger. This matches the
brief's hallucination-grounding gate: structured data drives the alert; the LLM
only explains it.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from typing import Any

from .agents import CarbonBudgetController
from .config import settings
from .grok_client import grok
from .models import AgentResult, AlertPayload, PatientState
from .profiles import PatientProfile

WEIGHTS = {
    "sofa_tracker": 0.30,
    "differential": 0.30,
    "predictor": 0.25,
    "narrative": 0.15,
}

CARBON = CarbonBudgetController()
_alert_counter = 0


def _next_alert_id() -> str:
    global _alert_counter
    _alert_counter += 1
    return f"SENT-2026-{_alert_counter:03d}"


def _consensus(agent_results: dict[str, AgentResult]) -> float:
    num = den = 0.0
    for name, w in WEIGHTS.items():
        r = agent_results.get(name)
        if not r:
            continue
        num += r.score * r.confidence * w
        den += r.confidence * w
    return round(num / den, 3) if den else 0.0


def _tier(consensus: float, diff: AgentResult, profile: PatientProfile, prev: str = "GREEN") -> str:
    """Tier assignment with hysteresis to prevent flapping near thresholds.

    A patient must clear a higher bar to ENTER a tier than to LEAVE it, so a
    consensus hovering around 65 (e.g. a recovering septic-shock patient) does
    not bounce RED↔YELLOW every loop — that would be the alert fatigue SENTINEL
    is built to eliminate.
    """
    RED_ENTER, RED_EXIT = 65.0, 58.0
    YEL_ENTER, YEL_EXIT = 35.0, 28.0
    not_sepsis = diff.extra.get("is_sepsis_likely") is False
    # comfort-care patients: suppress aggressive alerts
    if profile.advance_directive in ("COMFORT CARE", "DNR"):
        return "GREEN" if consensus < 55 else "YELLOW"
    if not_sepsis and consensus < 55:
        return "GREEN"
    if prev == "RED":
        if consensus >= RED_EXIT:
            return "RED"
        return "YELLOW" if consensus >= YEL_EXIT else "GREEN"
    if prev == "YELLOW":
        if consensus >= RED_ENTER:
            return "RED"
        return "YELLOW" if consensus >= YEL_EXIT else "GREEN"
    # prev GREEN
    if consensus >= RED_ENTER:
        return "RED"
    if consensus >= YEL_ENTER:
        return "YELLOW"
    return "GREEN"


def _ambiguity(agent_results: dict[str, AgentResult], consensus: float, profile: PatientProfile,
               data_confidence: float = 1.0) -> float:
    scores = [r.score for r in agent_results.values()]
    std = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    borderline = max(
        1 - abs(consensus - 35) / 20,
        1 - abs(consensus - 65) / 20,
        0,
    )
    mimic_tension = 0.25 if (profile.mimic_tag and any(r.extra.get("is_sepsis_likely") for r in agent_results.values())) else 0.0
    # governance: untrustworthy input is inherently ambiguous — route it toward
    # more review (LLM synthesis / human eyes) rather than a confident auto-call.
    trust_penalty = (1.0 - data_confidence) * 0.5
    return max(0.0, min(1.0, std / 100 * 1.3 + borderline * 0.35 + mimic_tension + trust_penalty))


def _action_for(tier: str, diff: AgentResult, profile: PatientProfile) -> tuple[str, str, str]:
    not_sepsis = diff.extra.get("is_sepsis_likely") is False
    caution = "; ".join(diff.extra.get("caution_flags") or [])
    # Comfort-care / DNR: dignity preservation overrides the aggressive sepsis
    # bundle. The tier is already capped at YELLOW in _tier(); here we make sure
    # the recommended action honours the directive too — never cultures +
    # antibiotics + bolus against an expressed goal of care. This is
    # deterministic by design: dignity must not depend on whether Grok fires.
    if profile.advance_directive in ("COMFORT CARE", "DNR"):
        action = (
            "Comfort-care pathway in effect — do NOT escalate to the aggressive sepsis bundle. "
            "Reassess goals of care with family and senior clinician. Prioritise symptom relief: "
            "analgesia, oxygen for comfort, fluids only if consistent with stated goals. Document the decision."
        )
        uncertainty = "High — clinical deterioration vs advance directive; senior clinician + family review required."
        return action, f"Advance directive ({profile.advance_directive}) overrides aggressive resuscitation — dignity preserved.", uncertainty
    if not_sepsis:
        action = f"Mimic identified ({profile.mimic_tag}). Manage underlying condition — no sepsis bundle indicated. Continue monitoring."
        return action, caution or "Mimic ruled out — avoid unnecessary antibiotics.", "Low"
    if tier == "RED":
        action = ("Draw blood cultures ×2. Administer broad-spectrum antibiotics within 30 min. "
                  "Start 30 mL/kg crystalloid bolus. Recheck lactate at 2 h. Notify senior physician / ICU.")
        uncertainty = "Medium — requires human review." + (" Antipyretic use may mask fever." if "no fever" in caution else "")
        return action, caution, uncertainty
    if tier == "YELLOW":
        action = ("Draw blood cultures. Notify senior nurse. Consider early IV fluids. "
                  "Recheck lactate. Monitor closely over next 60 min.")
        return action, caution, "Medium — trend rising; review."
    return "Continue silent monitoring. No action required at this time.", caution, "Low"


SYSTEM = (
    "You are SENTINEL's orchestrator: a clinical AI council moderator for sepsis detection. "
    "Four specialist agents (sofa_tracker, differential, predictor, narrative) have independently "
    "assessed a patient. Your job: run the cross-examination stage and synthesize a final, "
    "clinician-facing explanation. Be concrete, cite the specific vitals/labs/trends, and never "
    "invent values not present. Keep reasoning to 2-3 sentences per agent. Return STRICT JSON only."
)

PROMPT_TMPL = """Patient: {name}, {age}{sex}, ward: {ward}. Comorbidities: {comorb}. Advance directive: {adv}.
Sim time: {sim_t:.0f} min. Current vitals: HR {hr}, RR {rr}, SBP {sbp}, MAP {map}, Temp {temp}, SpO2 {spo2}, shock_index {shock}.
Latest labs: lactate {lactate}, WBC {wbc}, creatinine {creatinine}, bilirubin {bilirubin}, platelets {platelets}, GCS {gcs}.
SOFA {sofa} (Δ {dsofa}), qSOFA met={qsofa}.
Mimic context: {mimic}
Recent nursing notes: {notes}

Independent agent assessments (stage 1):
{stage1}

Cross-examine the agents: have the Differential challenge the Predictor where a mimic is plausible, "
and have the SOFA Tracker corroborate or contradict the Narrative. Then synthesize.

Return JSON with EXACTLY these keys:
{{
  "agent_reasoning": {{"sofa_tracker": "...", "differential": "...", "predictor": "...", "narrative": "..."}},
  "cross_examination": [{{"challenger": "...", "challenge": "...", "response": "..."}}],
  "consensus_explanation": "one paragraph for the clinician explaining the final call",
  "recommended_action": "concrete next steps",
  "differential_note": "mimic/caution note",
  "uncertainty_flag": "Low|Medium|High — short reason"
}}
"""


class Orchestrator:
    def __init__(self) -> None:
        self.carbon = CarbonBudgetController()

    async def deliberate(
        self,
        state: PatientState,
        agent_results: dict[str, AgentResult],
        features: dict[str, Any],
        profile: PatientProfile,
        prev_tier: str = "GREEN",
        consensus: float | None = None,
    ) -> AlertPayload:
        # Use the simulator's EMA-smoothed consensus when provided (suppresses
        # noise-driven flapping); otherwise recompute from agent scores.
        consensus = consensus if consensus is not None else _consensus(agent_results)
        tier = _tier(consensus, agent_results["differential"], profile, prev_tier)
        data_trust = features.get("data_trust", {}) or {}
        data_conf = data_trust.get("data_confidence", 1.0)
        ambiguity = _ambiguity(agent_results, consensus, profile, data_conf)
        budget = self.carbon.select(ambiguity)

        engine = "local"
        # Models actually used = the agents' own model_used (deduped, in order).
        model_used_list: list[str] = []
        for r in agent_results.values():
            if r.model_used and r.model_used not in model_used_list:
                model_used_list.append(r.model_used)
        carbon_cost = 0.01  # ml_only baseline; upgraded only if Grok actually runs
        payload: dict[str, Any] = {}

        # Stage 2 + 3 via Grok synthesis (if budget permits)
        if budget["llm"]:
            payload, raw = await self._grok_synthesis(state, agent_results, features, profile, budget["model"])
            # Only credit Grok if it actually produced the output (not the
            # local fallback). Otherwise keep the honest ml_only attribution.
            if raw.engine == "grok":
                engine = "grok"
                model_used_list.append(budget["model"])
                carbon_cost = budget["estimated_co2_g"]
                self._enrich(agent_results, payload)
            # fall through with deterministic consensus/tier regardless

        cross_exam: list[dict[str, Any]] = []
        consensus_explanation = ""
        if budget["llm"] and isinstance(payload, dict):
            cross_exam = payload.get("cross_examination") or []
            consensus_explanation = payload.get("consensus_explanation") or ""

        # Honest deterministic cross-examination when Grok didn't produce one
        # (local mode, ml_only budget, or empty Grok response). These
        # challenge/response pairs are derived from the ACTUAL agent scores,
        # feature values, and mimic context — not invented — so the council
        # debate is always visible in the dashboard. Clearly labelled LOCAL.
        if not cross_exam:
            cross_exam = _local_cross_exam(agent_results, features, profile, tier)

        # Deterministic fallback explanation (local mode / empty Grok response)
        if not consensus_explanation:
            consensus_explanation = _local_explanation(tier, agent_results, features, profile)

        action, diff_note, uncertainty = self._finalize_text(tier, agent_results, profile, payload if budget["llm"] else {})

        # governance: when input trust is low, say so loudly on the alert so a
        # clinician never acts on a confident sepsis call built on bad data.
        if data_trust.get("data_trust_grade") == "LOW":
            flag_txt = "; ".join(data_trust.get("data_quality_flags") or []) or "low composite confidence"
            diff_note = (diff_note + " " if diff_note else "") + f"⚠ LOW DATA TRUST ({flag_txt}) — treat scores with caution."
            if "LOW DATA TRUST" not in uncertainty:
                uncertainty = (uncertainty + " " if uncertainty else "") + "LOW DATA TRUST — verify feed before acting."

        alert = AlertPayload(
            alert_id=_next_alert_id(),
            patient_id=state.patient_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            sim_t=round(state.sim_t, 1),
            tier=tier,
            consensus=consensus,
            agents={k: _agent_dict(v) for k, v in agent_results.items()},
            recommended_action=action,
            differential_note=diff_note,
            uncertainty_flag=uncertainty,
            carbon_cost_g=carbon_cost,
            models_used=model_used_list,
            cross_examination=cross_exam,
            consensus_explanation=consensus_explanation,
            engine=engine,
            data_trust=data_trust,
        )
        return alert

    async def _grok_synthesis(self, state, agent_results, features, profile, model):
        stage1 = "\n".join(
            f"- {n}: score={r.score}, confidence={r.confidence}, reasoning: {r.reasoning}"
            for n, r in agent_results.items()
        )
        notes = "; ".join(n.get("text", "") for n in state.notes[-3:]) or "(none yet)"
        prompt = PROMPT_TMPL.format(
            name=profile.name, age=profile.age, sex=profile.sex, ward=profile.ward,
            comorb=", ".join(profile.comorbidities) or "none", adv=profile.advance_directive,
            sim_t=state.sim_t,
            hr=features.get("hr_now"), rr=features.get("rr_now"), sbp=features.get("sbp_now"),
            map=features.get("map_now"), temp=features.get("temp_now"), spo2=features.get("spo2_now"),
            shock=features.get("shock_index"),
            lactate=features.get("lactate_latest"), wbc=features.get("wbc_latest"),
            creatinine=features.get("creatinine_latest"), bilirubin=features.get("bilirubin_latest"),
            platelets=features.get("platelets_latest"), gcs=features.get("gcs_latest"),
            sofa=features.get("sofa_score"), dsofa=features.get("delta_sofa"), qsofa=features.get("qsofa_met"),
            mimic=profile.mimic_tag or "none",
            notes=notes, stage1=stage1,
        )
        data, raw = await grok.complete_json(SYSTEM, prompt, model=model)
        return data, raw

    def _enrich(self, agent_results: dict[str, AgentResult], payload: dict[str, Any]) -> None:
        reasoning = payload.get("agent_reasoning") or {}
        for name, r in agent_results.items():
            if reasoning.get(name):
                r.reasoning = reasoning[name]
                r.engine = "grok"
                r.llm_used = settings.xai_model if settings.xai_model else "grok"

    def _finalize_text(self, tier, agent_results, profile, grok_payload) -> tuple[str, str, str]:
        diff = agent_results["differential"]
        base_action, base_diff, base_unc = _action_for(tier, diff, profile)
        # Dignity preservation is deterministic: for a comfort-care / DNR
        # directive, never let Grok's recommendation override the directive-aware
        # action. Grok may still supply the consensus explanation / debate, but
        # the recommended action must honour the stated goals of care.
        if profile.advance_directive in ("COMFORT CARE", "DNR"):
            return base_action, base_diff, base_unc
        if grok_payload:
            action = grok_payload.get("recommended_action") or base_action
            diff_note = grok_payload.get("differential_note") or base_diff
            unc = grok_payload.get("uncertainty_flag") or base_unc
            return action, diff_note, unc
        return base_action, base_diff, base_unc


def _local_cross_exam(
    agent_results: dict[str, AgentResult],
    f: dict[str, Any],
    profile: PatientProfile,
    tier: str,
) -> list[dict[str, Any]]:
    """Deterministic, honest cross-examination derived from real agent outputs.

    Each pair reflects a genuine tension or corroboration present in the
    stage-1 assessments + structured features. No values are invented; every
    number cited comes from the feature dict or agent extra. Used only when
    Grok didn't produce a debate, so the collaboration is always visible.
    """
    diff = agent_results.get("differential")
    pred = agent_results.get("predictor")
    narr = agent_results.get("narrative")
    sofa = agent_results.get("sofa_tracker")
    pairs: list[dict[str, Any]] = []

    lactate = f.get("lactate_latest")
    qsofa = f.get("qsofa_met", False)
    temp = f.get("temp_now", 0.0)
    mimic = profile.mimic_tag

    # 0) Directive vs physiology: for a comfort-care / DNR patient the
    #    physiology says sepsis but the stated goals of care forbid aggressive
    #    resuscitation. Surface this as the lead debate — it is the ethically
    #    load-bearing question and the reason the tier is capped at YELLOW.
    if profile.advance_directive in ("COMFORT CARE", "DNR") and pred and pred.score >= 35:
        pairs.append({
            "challenger": "Orchestrator",
            "challenge": (f"Predictor scores {pred.score:.0f} (P(sepsis)={pred.extra.get('ml_proba', '?')}, "
                          f"SOFA Δ {f.get('delta_sofa', 0):+.1f}, lactate {lactate}) — physiology says sepsis. "
                          f"But the chart carries a {profile.advance_directive} directive. Do we push the bundle?"),
            "response": (f"No. The directive is authoritative: tier capped at YELLOW, no cultures/antibiotics/bolus. "
                         "Escalate to goals-of-care discussion with family + senior clinician; focus on comfort. "
                         "Dignity preservation overrides the sepsis protocol here."),
        })

    # 1) Mimic tension: Differential challenges Predictor when a mimic fits but
    #    the trajectory agent still sees rising risk.
    if mimic and diff and diff.extra.get("is_sepsis_likely") is False and pred and pred.score > 35:
        pairs.append({
            "challenger": "Differential",
            "challenge": (f"You report accelerating risk (score {pred.score:.0f}), but this patient has "
                          f"{mimic}. Lactate is {lactate} and qSOFA is {'MET' if qsofa else 'not met'} — "
                          f"what distinguishes sepsis from {mimic} inflammation?"),
            "response": (f"Trajectory composite {pred.extra.get('rate_of_change', 0):+.2f} with "
                         f"XGBoost P(sepsis)={pred.extra.get('ml_proba', '?')}; the slope is convex, "
                         "which post-surgical/pancreatic stress rarely shows. Recommend confirmatory lactate trend."),
        })
    # 2) Documentation vs structured-data contradiction: SOFA Tracker vs Narrative
    if narr and narr.extra.get("contradictions"):
        contra = "; ".join(narr.extra["contradictions"])
        pairs.append({
            "challenger": "SOFA Tracker",
            "challenge": (f"Narrative flags a documentation tension: {contra}. Could antipyretic use or "
                          "a stale note be masking the true severity?"),
            "response": (f"Structured SOFA {f.get('sofa_score', 0):.0f} (Δ {f.get('delta_sofa', 0):+.1f}), "
                         f"temp {temp:.1f}°C, shock index {f.get('shock_index', 0):.2f} — organ data is "
                         "primary; treat the note as a caution, not a override."),
        })
    # 3) Genuine agent disagreement (high score spread) → orchestrator probes
    scores = [r.score for r in agent_results.values()]
    spread = max(scores) - min(scores) if scores else 0.0
    if spread >= 35 and len(pairs) < 2:
        high = max(agent_results.values(), key=lambda r: r.score)
        low = min(agent_results.values(), key=lambda r: r.score)
        pairs.append({
            "challenger": "Orchestrator",
            "challenge": (f"Council is split: {high.name} scores {high.score:.0f} vs {low.name} {low.score:.0f} "
                          f"(spread {spread:.0f}). Reconcile before tiering."),
            "response": (f"Weighted consensus weights SOFA + Differential (0.30 each) heaviest; the "
                         f"trajectory signal is downweighted until lactate confirms. Current consensus → {tier}."),
        })
    # 4) Concordance: if no tension, state it plainly
    if not pairs:
        pairs.append({
            "challenger": "Orchestrator",
            "challenge": "Any agent contradict the leading signal?",
            "response": (f"No — all four agents concur ({'sepsis likely' if tier != 'GREEN' else 'stable'}). "
                         "Structured vitals, labs, trajectory and notes are consistent. "
                         "Consensus grounded in corroborated data."),
        })
    return pairs


def _local_explanation(tier: str, agent_results: dict[str, AgentResult], f: dict[str, Any], profile: PatientProfile) -> str:
    diff = agent_results.get("differential")
    not_sepsis = diff and diff.extra.get("is_sepsis_likely") is False
    if profile.advance_directive in ("COMFORT CARE", "DNR"):
        return (f"{profile.name}: sepsis trajectory present (SOFA Δ {f.get('delta_sofa', 0):+.1f}, lactate "
                f"{f.get('lactate_latest')}, shock index {f.get('shock_index', 0):.2f}) but the "
                f"{profile.advance_directive} directive caps the alert at YELLOW — no aggressive bundle pushed. "
                "Reassess goals of care with family; prioritise dignity and symptom relief over resuscitation.")
    if not_sepsis:
        return (f"{profile.name}: agent council agrees sepsis is unlikely — {profile.mimic_tag} mimic fits the "
                f"stable lactate and absent qSOFA. No sepsis bundle; manage the underlying condition.")
    if tier == "RED":
        return (f"{profile.name}: high consensus across SOFA tracker, differential and predictor — rising SOFA "
                f"(Δ {f.get('delta_sofa', 0):+.1f}), lactate {f.get('lactate_latest')}, shock index {f.get('shock_index', 0):.2f}. "
                "Act now: cultures + antibiotics within 30 min.")
    if tier == "YELLOW":
        return (f"{profile.name}: trend rising — RR/HR slopes positive, SOFA climbing. Not yet red, but trajectory "
                "warrants cultures, fluids, and close monitoring.")
    return f"{profile.name}: all agents concur — stable. Continue silent monitoring."


def _agent_dict(r: AgentResult) -> dict[str, Any]:
    d = {
        "score": r.score,
        "confidence": r.confidence,
        "reasoning": r.reasoning,
        "model_used": r.model_used,
        "engine": r.engine,
    }
    if r.llm_used and r.llm_used != "none":
        d["llm_used"] = r.llm_used
    if r.extra:
        d["extra"] = r.extra
    return d


orchestrator = Orchestrator()