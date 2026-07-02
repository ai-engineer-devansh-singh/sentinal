"""Rolling feature store.

Keeps the last WINDOW_MINUTES of vitals per patient and computes the
streaming features the agents consume: slopes, deltas, EWMA, shock index,
SOFA/qSOFA. Lightweight and in-memory — enough for a live demo and faithful
to the 15-minute loop described in the tech brief.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .governance import PatientDataGovernance
from .models import LabResult, Vitals

WINDOW = 15.0  # minutes


@dataclass
class PatientFeatures:
    window: deque = field(default_factory=lambda: deque(maxlen=900))
    baseline_sofa: float = 0.0
    _baseline_locked: bool = False
    last_sofa: float = 0.0
    last_lab: LabResult | None = None
    gov: PatientDataGovernance = field(default_factory=PatientDataGovernance)

    def add(self, v: Vitals) -> None:
        # governance: validate every incoming reading before it enters the
        # window. The reading is still appended (so features never starve),
        # but its plausibility is tallied into the rolling trust score.
        self.gov.validate(v)
        self.window.append(v)

    def _in_window(self, t_now: float) -> list[Vitals]:
        return [v for v in self.window if v.t >= t_now - WINDOW]

    def compute(self, t_now: float, lab: LabResult | None) -> dict[str, Any]:
        self.last_lab = lab or self.last_lab
        win = self._in_window(t_now)
        out: dict[str, Any] = {}
        now = win[-1] if win else None
        if now is None:
            return out

        out["hr_now"] = now.hr
        out["rr_now"] = now.rr
        out["sbp_now"] = now.sbp
        out["dbp_now"] = now.dbp
        out["map_now"] = round(now.map, 1)
        out["temp_now"] = now.temp
        out["spo2_now"] = now.spo2
        out["shock_index"] = round(now.hr / max(now.sbp, 1), 2)

        if len(win) >= 3:
            ts = [v.t for v in win]
            hrs = [v.hr for v in win]
            rrs = [v.rr for v in win]
            temps = [v.temp for v in win]
            sbps = [v.sbp for v in win]
            out["hr_slope"] = round(_slope(ts, hrs), 3)  # bpm / min
            out["rr_slope"] = round(_slope(ts, rrs), 3)
            out["temp_slope"] = round(_slope(ts, temps), 4)
            out["sbp_delta_window"] = round(max(sbps) - min(sbps), 1)
            # EWMA for RR
            alpha = 0.3
            ew = rrs[0]
            for r in rrs[1:]:
                ew = alpha * r + (1 - alpha) * ew
            out["rr_ewma"] = round(ew, 2)

        # 1-hour-ish delta if we have enough history
        hist = [v for v in self.window if v.t >= t_now - 60]
        if hist:
            out["sbp_delta_1h"] = round(now.sbp - hist[0].sbp, 1)
            out["hr_delta_1h"] = round(now.hr - hist[0].hr, 1)
        else:
            out["sbp_delta_1h"] = 0.0
            out["hr_delta_1h"] = 0.0

        # SOFA / qSOFA from latest lab + vitals
        sofa, components = _sofa(now, self.last_lab)
        out["sofa_score"] = sofa
        out["sofa_components"] = components
        out["delta_sofa"] = round(sofa - self.last_sofa, 2)  # per-tick change (display only)
        if not self._baseline_locked:
            self.baseline_sofa = sofa
            self._baseline_locked = True
        out["baseline_sofa"] = self.baseline_sofa
        out["delta_from_baseline"] = round(sofa - self.baseline_sofa, 2)  # monotonic trend
        out["qsofa_met"] = _qsofa(now, self.last_lab)
        self.last_sofa = sofa

        if self.last_lab is not None:
            out["lactate_latest"] = self.last_lab.lactate
            out["wbc_latest"] = self.last_lab.wbc
            out["creatinine_latest"] = self.last_lab.creatinine
            out["bilirubin_latest"] = self.last_lab.bilirubin
            out["platelets_latest"] = self.last_lab.platelets
            out["gcs_latest"] = self.last_lab.gcs

        # ── data governance / input trust (rolling window) ─────────────────
        # Every feature dict carries a data_confidence + grade + flags so every
        # downstream consumer (agents, orchestrator, dashboard) sees how
        # trustworthy the underlying feed is. See governance.py.
        last_lab_t = self.last_lab.t if self.last_lab is not None else None
        out["data_trust"] = self.gov.assess(win, t_now, last_lab_t)
        return out


def _slope(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def _sofa(v: Vitals, lab: LabResult | None) -> tuple[float, dict[str, int]]:
    """Practical SOFA from the data we actually have.

    Six organ systems; we approximate where the full variable is missing
    (e.g. SpO2 as a PaO2/FiO2 proxy). This is a demo scoring engine, not a
    clinical device — but it follows Sepsis-3 structure.
    """
    c: dict[str, int] = {}

    # Respiratory: SpO2/FiO2 proxy (room air ~ FiO2 21%)
    if v.spo2 >= 95:
        c["respiratory"] = 0
    elif v.spo2 >= 90:
        c["respiratory"] = 1
    elif v.spo2 >= 85:
        c["respiratory"] = 2
    else:
        c["respiratory"] = 3

    # Coagulation: platelets
    plt = lab.platelets if lab and lab.platelets is not None else 250
    if plt >= 150:
        c["coagulation"] = 0
    elif plt >= 100:
        c["coagulation"] = 1
    elif plt >= 50:
        c["coagulation"] = 2
    elif plt >= 20:
        c["coagulation"] = 3
    else:
        c["coagulation"] = 4

    # Liver: bilirubin (umol/L; ~17.1 = 1 mg/dL)
    bil = lab.bilirubin if lab and lab.bilirubin is not None else 12
    if bil < 20:
        c["liver"] = 0
    elif bil < 33:
        c["liver"] = 1
    elif bil < 50:
        c["liver"] = 2
    elif bil < 100:
        c["liver"] = 3
    else:
        c["liver"] = 4

    # Cardiovascular: MAP (no vasopressors data)
    m = v.map
    if m >= 70:
        c["cardiovascular"] = 0
    elif m >= 65:
        c["cardiovascular"] = 1
    else:
        c["cardiovascular"] = 2

    # CNS: GCS
    gcs = lab.gcs if lab and lab.gcs is not None else 15
    if gcs >= 14:
        c["cns"] = 0
    elif gcs >= 11:
        c["cns"] = 1
    elif gcs >= 8:
        c["cns"] = 2
    else:
        c["cns"] = 3

    # Renal: creatinine
    cr = lab.creatinine if lab and lab.creatinine is not None else 0.9
    if cr < 1.2:
        c["renal"] = 0
    elif cr < 2.0:
        c["renal"] = 1
    elif cr < 3.5:
        c["renal"] = 2
    else:
        c["renal"] = 3

    return float(sum(c.values())), c


def _qsofa(v: Vitals, lab: LabResult | None) -> bool:
    criteria = 0
    if v.rr >= 22:
        criteria += 1
    if v.sbp <= 100:
        criteria += 1
    gcs = lab.gcs if lab and lab.gcs is not None else 15
    if gcs < 15:
        criteria += 1
    return criteria >= 2


class FeatureStore:
    def __init__(self) -> None:
        self._by_patient: dict[str, PatientFeatures] = {}

    def get(self, patient_id: str) -> PatientFeatures:
        if patient_id not in self._by_patient:
            self._by_patient[patient_id] = PatientFeatures()
        return self._by_patient[patient_id]