"""Device simulator + clinical loop.

Drives the whole demo: each connected patient's wearable patch + POCT feed
streams vitals on a tick (1 real second = `speed` simulated minutes). Every
tick we update the rolling feature store and run the four deterministic
agents for a live risk badge. Full deliberation (which may call Grok) runs
on a throttle and in a background task so streaming never blocks on LLM
latency. Results are broadcast to all dashboard subscribers via asyncio
queues.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .agents import AGENTS
from .audit import audit
from .feature_store import FeatureStore
from .models import PatientState
from .orchestrator import orchestrator
from .profiles import PROFILES, PatientProfile, labs_at, new_notes, sample_vitals

log = logging.getLogger("sentinel.sim")

DELIB_INTERVAL = 4.0  # sim minutes between full deliberations
HISTORY_LEN = 120


class Simulator:
    def __init__(self, autoconnect: bool = True, speed: float = 1.0) -> None:
        self.states: dict[str, PatientState] = {}
        self.profiles: dict[str, PatientProfile] = {p.patient_id: p for p in PROFILES}
        self.features = FeatureStore()
        self.subscribers: set[asyncio.Queue] = set()
        self.speed = speed
        self.paused = False
        self.autoconnect = autoconnect
        self._task: asyncio.Task | None = None
        self._last_delib_t: dict[str, float] = {}
        self._last_delib_tier: dict[str, str] = {}
        self._delib_inflight: dict[str, bool] = {}
        self._last_trust_grade: dict[str, str] = {}  # governance transition tracking

        for p in PROFILES:
            self.states[p.patient_id] = PatientState(
                patient_id=p.patient_id, name=p.name, age=p.age, sex=p.sex,
                ward=p.ward, comorbidities=list(p.comorbidities),
                advance_directive=p.advance_directive,
            )

    # ── lifecycle ──────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        if self.autoconnect:
            for pid in self.states:
                self.connect(pid)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def connect(self, patient_id: str) -> bool:
        s = self.states.get(patient_id)
        if s and not s.connected:
            s.connected = True
            s.started_real = asyncio.get_event_loop().time()
            self._last_delib_t[patient_id] = -999
            self._last_delib_tier[patient_id] = "GREEN"
            return True
        return False

    def disconnect(self, patient_id: str) -> None:
        s = self.states.get(patient_id)
        if s:
            s.connected = False

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.25, min(8.0, float(speed)))

    def set_paused(self, paused: bool) -> None:
        self.paused = paused

    def _audit_governance(self, pid: str, trust: dict[str, Any]) -> None:
        """Log a governance event when input trust changes grade.

        Traceability for the medical-governance requirement: every escalation
        into LOW/MEDIUM trust (and recovery back to HIGH) is written to the
        audit trail with the flags that caused it.
        """
        grade = trust.get("data_trust_grade", "HIGH")
        prev = self._last_trust_grade.get(pid, "HIGH")
        if grade == prev:
            return
        self._last_trust_grade[pid] = grade
        audit.append({
            "event": "governance", "patient_id": pid, "trust_grade": grade,
            "from": prev, "data_confidence": trust.get("data_confidence"),
            "flags": trust.get("data_quality_flags", []),
            "sim_t": self.states[pid].sim_t,
        })

    # ── pub/sub ────────────────────────────────────────────────────────
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def _broadcast(self, msg: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # drop if dashboard can't keep up

    # ── main loop ──────────────────────────────────────────────────────
    async def _loop(self) -> None:
        from .config import settings
        tick_s = settings.tick_seconds
        while True:
            await asyncio.sleep(tick_s)
            if self.paused:
                continue
            delta = self.speed * tick_s  # sim minutes advanced this tick
            for pid, state in self.states.items():
                if not state.connected:
                    continue
                try:
                    await self._step_patient(pid, state, delta)
                except Exception as e:
                    log.exception("step failed for %s: %s", pid, e)
            self._broadcast({"type": "tick", "patients": self.snapshot()})

    async def _step_patient(self, pid: str, state: PatientState, delta: float) -> None:
        profile = self.profiles[pid]
        state.sim_t = min(state.sim_t + delta, profile.duration)

        v = sample_vitals(profile, state.sim_t)
        pf = self.features.get(pid)
        pf.add(v)

        lab = labs_at(profile, state.sim_t)
        feats = pf.compute(state.sim_t, lab)

        # new notes?
        t_prev = state.sim_t - delta
        for note in new_notes(profile, t_prev, state.sim_t):
            state.notes.append({"t": note.t, "text": note.text})

        # update broadcastable state
        state.vitals_now = {
            "t": v.t, "hr": v.hr, "rr": v.rr, "sbp": v.sbp, "dbp": v.dbp,
            "map": round(v.map, 1), "temp": v.temp, "spo2": v.spo2,
        }
        state.vitals_history.append(state.vitals_now)
        if len(state.vitals_history) > HISTORY_LEN:
            state.vitals_history = state.vitals_history[-HISTORY_LEN:]
        if lab is not None:
            state.labs = {
                "lactate": lab.lactate, "wbc": lab.wbc, "creatinine": lab.creatinine,
                "bilirubin": lab.bilirubin, "platelets": lab.platelets, "gcs": lab.gcs,
                "t": lab.t,
            }
        state.sofa_score = feats.get("sofa_score", 0.0)
        state.delta_sofa = feats.get("delta_sofa", 0.0)
        state.qsofa_met = feats.get("qsofa_met", False)
        state.shock_index = feats.get("shock_index", 0.0)
        state.features = feats
        state.data_trust = feats.get("data_trust", {})

        # live deterministic agents (cheap, every tick) for the badge
        live: dict[str, Any] = {}
        for agent in AGENTS:
            live[agent.name] = agent.assess(state, feats, profile)
        # ── governance gate: no agent may be more confident than the input ──
        # A medical AI must not make a confident sepsis call on untrustworthy
        # data. Cap every agent's confidence by the rolling data_confidence.
        data_conf = feats.get("data_trust", {}).get("data_confidence", 1.0)
        if data_conf < 1.0:
            for r in live.values():
                r.confidence = round(r.confidence * data_conf, 3)
        state.agent_results = live

        # audit a governance transition when trust drops into LOW
        self._audit_governance(pid, feats.get("data_trust", {}))

        from .orchestrator import _consensus, _tier
        prev_tier = self._last_delib_tier.get(pid, "GREEN")
        raw_consensus = _consensus(live)
        # EMA-smooth the consensus so per-tick vitals noise can't flap the tier.
        if state.consensus_ema is None:
            state.consensus_ema = raw_consensus
        else:
            state.consensus_ema = round(0.3 * raw_consensus + 0.7 * state.consensus_ema, 3)
        consensus = state.consensus_ema
        live_tier = _tier(consensus, live["differential"], profile, prev_tier)
        state.tier = live_tier

        # full deliberation (possibly Grok) on a throttle / on tier change
        due = state.sim_t - self._last_delib_t.get(pid, -999) >= DELIB_INTERVAL
        changed = live_tier != prev_tier
        if (due or changed) and not self._delib_inflight.get(pid):
            self._delib_inflight[pid] = True
            self._last_delib_t[pid] = state.sim_t
            asyncio.create_task(self._run_deliberation(pid, live, feats, profile, prev_tier, consensus))

        # end-of-stay outcome
        if state.sim_t >= profile.duration and state.outcome == "MONITORING":
            state.outcome = self._compute_outcome(profile, state)
            self._broadcast({"type": "outcome", "patient_id": pid, "outcome": state.outcome})
            audit.append({"event": "outcome", "patient_id": pid, "outcome": state.outcome,
                          "sim_t": state.sim_t})

    async def _run_deliberation(self, pid, live, feats, profile, prev_tier="GREEN", consensus=None):
        state = self.states[pid]
        try:
            alert = await orchestrator.deliberate(state, live, feats, profile, prev_tier, consensus)
            state.current_alert = alert
            state.tier = alert.tier
            state.pending_alert = False
            self._last_delib_tier[pid] = alert.tier
            # GREEN is silent: only log + broadcast on a tier CHANGE
            # (escalation or de-escalation). The drawer still shows the latest
            # GREEN assessment via the tick stream.
            if alert.tier != prev_tier:
                audit.append({"event": "alert", "alert_id": alert.alert_id, "patient_id": pid,
                               "tier": alert.tier, "consensus": alert.consensus,
                               "engine": alert.engine, "sim_t": alert.sim_t,
                               "from": prev_tier})
                self._broadcast({"type": "alert", "patient_id": pid, "alert": _alert_dict(alert)})
        except Exception as e:
            log.exception("deliberation failed for %s: %s", pid, e)
        finally:
            self._delib_inflight[pid] = False

    def _compute_outcome(self, profile: PatientProfile, state: PatientState) -> str:
        gt = profile.ground_truth
        cat = gt.get("category")
        # Honour advance directives first — dignity preservation (ethics)
        if profile.advance_directive in ("COMFORT CARE", "DNR"):
            return "DIGNITY PRESERVED — comfort-care pathway honoured"
        acted = state.clinician_action in ("ACCEPT", "MODIFY")
        if cat in ("sepsis", "septic_shock"):
            return "STABLE — SENTINEL intervention timely" if acted else "DECEASED — delayed recognition"
        if cat == "mimic":
            not_sepsis = state.agent_results.get("differential")
            ok = not_sepsis and not_sepsis.extra.get("is_sepsis_likely") is False
            return "STABLE — mimic correctly managed" if ok else "OVER-TREATED — false sepsis bundle"
        return "DISCHARGED — stable"

    # ── HITL ───────────────────────────────────────────────────────────
    def respond(self, patient_id: str, action: str, reason: str, clinician_id: str) -> dict[str, Any]:
        state = self.states.get(patient_id)
        if not state:
            return {"ok": False, "error": "unknown patient"}
        state.clinician_action = action
        audit.append({"event": "hitl", "patient_id": patient_id, "action": action,
                       "reason": reason, "clinician_id": clinician_id})
        self._broadcast({"type": "hitl", "patient_id": patient_id, "action": action, "reason": reason})
        return {"ok": True, "outcome_preview": "intervention recorded"}

    # ── snapshot ───────────────────────────────────────────────────────
    def snapshot(self) -> list[dict[str, Any]]:
        out = []
        for pid, s in self.states.items():
            d = s.to_dict()
            gt = self.profiles[pid].ground_truth
            d["ground_truth"] = gt
            d["mimic_tag"] = self.profiles[pid].mimic_tag
            d["duration"] = self.profiles[pid].duration
            out.append(d)
        return out


def _alert_dict(a) -> dict[str, Any]:
    return json.loads(json.dumps(a.__dict__, default=str))


sim = Simulator(autoconnect=False, speed=1.0)  # configured by main at startup