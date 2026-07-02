"""Batch simulation — compute the brief's headline KPIs, equity, and drift.

Runs N synthetic patients (profile + noise seed + severity) through the *real*
pipeline (FeatureStore + the 4 agents + orchestrator tier with hysteresis +
EMA), then aggregates:

  KPIs  : sensitivity, specificity, median time-to-alert, override rate,
          mortality reduction vs no-SENTINEL, NPC-LS (net carbon saved / life)
  Equity: AUROC disaggregated by sex and age bucket (checkpoint 2 / SDG 10)
  Drift : AUROC over chronological windows + SPC control limits + qSOFA
          auto-fallback flag (checkpoint 3)

Results are cached per N and refreshed on demand. Run headless (no WebSocket),
so it's safe to call from the API.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from typing import Any

from .agents import AGENTS
from .config import settings
from .feature_store import FeatureStore
from .ml import ml as risk_model
from .models import LabResult, NursingNote, PatientState
from .orchestrator import _consensus, _tier
from .profiles import PROFILES, PatientProfile

TTA_CUTOFF_MIN = 60.0  # ward target: antibiotics within 60 min
CARBON_PER_ICU_ADMISSION_KG = 280.0  # CO₂ averted by preventing one ICU stay

_CONCERN_NOTES = [
    "Patient looks more unwell, mildly confused.",
    "Patient drowsy, BP low, skin clammy.",
    "Increasingly confused, cold peripheries, high lactate.",
    "Appears septic, tachycardic and hypotensive.",
]
_BENIGN_NOTES = [
    "Comfortable, in no acute distress.",
    "Stable overnight, vitals normal, no concerns.",
    "Recovering well, eating normally.",
    "Pain improved after analgesia, no red flags.",
]


# ── synthetic cohort generator (diverse, realistic overlap) ────────────────
def _gen_sepsis(rng: random.Random, idx: int) -> PatientProfile:
    onset = rng.uniform(15, 45)
    sev = rng.uniform(0.45, 1.3)  # mild → severe (mild may be missed → FN)
    b_hr, b_rr, b_sbp = rng.uniform(80, 95), rng.uniform(16, 18), rng.uniform(120, 132)
    b_temp, b_spo2 = rng.uniform(36.9, 37.4), rng.uniform(97, 99)
    d_hr, d_rr, d_sbp = sev * rng.uniform(20, 50), sev * rng.uniform(6, 12), sev * rng.uniform(20, 50)
    d_temp, d_spo2 = sev * rng.uniform(1.0, 2.2), sev * rng.uniform(3, 8)
    lact_peak = 0.8 + sev * rng.uniform(1.2, 3.8)

    def vit(t):
        p = 0.0 if t < onset else min(1.0, (t - onset) / 20.0)
        sbp = b_sbp - d_sbp * p
        return dict(hr=b_hr + d_hr * p, rr=b_rr + d_rr * p, sbp=sbp,
                    dbp=sbp * 0.62, temp=b_temp + d_temp * p, spo2=max(84, b_spo2 - d_spo2 * p))

    keyframes = {t: vit(t) for t in (0, 20, 40, 60, 80, 100)}

    def lab(t):
        p = 0.0 if t < onset else min(1.0, (t - onset) / 20.0)
        return LabResult(t=t, lactate=round(0.9 + lact_peak * p, 2), wbc=round(9 + sev * 9 * p, 1),
                         creatinine=round(0.9 + sev * 1.0 * p, 2), bilirubin=round(14 + sev * 22 * p, 1),
                         platelets=int(260 - sev * 120 * p), gcs=15 - int(sev * 2.5 * p))

    labs = [lab(onset + 6), lab(onset + 26)]
    notes = [NursingNote(t=onset + 12, text=rng.choice(_CONCERN_NOTES))]
    return PatientProfile(
        patient_id=f"gen-sepsis-{idx}", name=f"Synth-Sepsis-{idx}", age=rng.randint(28, 82),
        sex=rng.choice(["F", "M"]), ward="Batch Cohort", comorbidities=[], advance_directive="FULL CODE",
        keyframes=keyframes, labs=labs, notes=notes,
        ground_truth={"category": "sepsis", "expected_peak_tier": "RED", "first_alert_at": onset + 15, "onset": onset},
    )


def _gen_mimic(rng: random.Random, idx: int) -> PatientProfile:
    # inflammation (pancreatitis-like) but lactate LOW → should stay non-sepsis.
    # A severe mimic may transiently false-alarm → FP.
    sev = rng.uniform(0.6, 1.25)
    b_hr = rng.uniform(92, 104) + sev * 6
    b_temp = rng.uniform(37.6, 38.3) + sev * 0.3
    b_sbp = rng.uniform(112, 124) - sev * 4
    keyframes = {t: dict(hr=b_hr + rng.uniform(-2, 2), rr=rng.uniform(17, 20), sbp=b_sbp,
                         dbp=b_sbp * 0.63, temp=b_temp, spo2=rng.uniform(95, 98))
                 for t in (0, 20, 40, 60, 80, 100)}
    labs = [LabResult(t=15, lactate=round(rng.uniform(0.9, 2.2), 2), wbc=round(rng.uniform(11, 16), 1),
                      creatinine=round(rng.uniform(0.8, 1.2), 2), bilirubin=round(rng.uniform(18, 34), 1),
                      platelets=int(rng.uniform(200, 260)), gcs=15)]
    notes = [NursingNote(t=10, text=rng.choice(_BENIGN_NOTES))]
    return PatientProfile(
        patient_id=f"gen-mimic-{idx}", name=f"Synth-Mimic-{idx}", age=rng.randint(30, 78),
        sex=rng.choice(["F", "M"]), ward="Batch Cohort", comorbidities=[], advance_directive="FULL CODE",
        keyframes=keyframes, labs=labs, notes=notes, mimic_tag="pancreatitis",
        ground_truth={"category": "mimic", "expected_peak_tier": "GREEN", "first_alert_at": None},
    )


def _gen_healthy(rng: random.Random, idx: int) -> PatientProfile:
    b_hr, b_sbp = rng.uniform(68, 82), rng.uniform(110, 124)
    keyframes = {t: dict(hr=b_hr + rng.uniform(-3, 3), rr=rng.uniform(14, 17), sbp=b_sbp,
                         dbp=b_sbp * 0.62, temp=rng.uniform(36.6, 37.1), spo2=rng.uniform(97, 99))
                 for t in (0, 20, 40, 60, 80, 100)}
    labs = [LabResult(t=10, lactate=round(rng.uniform(0.7, 1.1), 2), wbc=round(rng.uniform(6, 9), 1),
                      creatinine=0.9, bilirubin=12, platelets=260, gcs=15)]
    notes = [NursingNote(t=8, text=rng.choice(_BENIGN_NOTES))]
    return PatientProfile(
        patient_id=f"gen-healthy-{idx}", name=f"Synth-Healthy-{idx}", age=rng.randint(22, 70),
        sex=rng.choice(["F", "M"]), ward="Batch Cohort", comorbidities=[], advance_directive="FULL CODE",
        keyframes=keyframes, labs=labs, notes=notes,
        ground_truth={"category": "healthy", "expected_peak_tier": "GREEN", "first_alert_at": None},
    )


def _interp(profile: PatientProfile, t: float) -> dict[str, float]:
    times = sorted(profile.keyframes)
    if t <= times[0]:
        return dict(profile.keyframes[times[0]])
    if t >= times[-1]:
        return dict(profile.keyframes[times[-1]])
    for i in range(len(times) - 1):
        if times[i] <= t <= times[i + 1]:
            lo, hi = times[i], times[i + 1]
            a = (t - lo) / (hi - lo) if hi > lo else 0.0
            f0, f1 = profile.keyframes[lo], profile.keyframes[hi]
            return {k: f0[k] + (f1[k] - f0[k]) * a for k in f0}
    return dict(profile.keyframes[times[-1]])


def _noisy_vitals(profile, t, rng, sev):
    f = _interp(profile, t)
    n = lambda b, amp: b + rng.uniform(-amp, amp)
    v = type("V", (), {
        "t": t, "hr": n(f["hr"], 3) * sev, "rr": n(f["rr"], 1.2), "sbp": n(f["sbp"], 3.5) / sev,
        "dbp": n(f["dbp"], 2.2) / sev, "temp": f["temp"] + rng.uniform(-0.1, 0.1),
        "spo2": min(100, f["spo2"] + rng.uniform(-0.5, 0.5)),
    })()
    v.map = v.dbp + (v.sbp - v.dbp) / 3.0
    return v


def _latest_lab(profile, t):
    latest = None
    for lab in profile.labs:
        if lab.t <= t:
            latest = lab
    return latest


@dataclass
class PatientRun:
    profile_id: str
    name: str
    sex: str
    age: int
    label: int  # 1 = true sepsis/septic shock, 0 = non-case
    peak_consensus: float
    first_alert_t: float | None
    onset: float | None
    reached_red: bool
    comfort_care: bool
    outcome_with: str
    outcome_without: str
    carbon_g: float
    directive: str


def _label_for(profile: PatientProfile) -> int:
    return 1 if profile.ground_truth.get("category") in ("sepsis", "septic_shock") else 0


def run_one(profile: PatientProfile, seed: int) -> PatientRun:
    rng = random.Random(seed)
    sev = rng.uniform(0.9, 1.12)  # severity multiplier per run
    fs = FeatureStore()
    state = PatientState(
        patient_id=profile.patient_id, name=profile.name, age=profile.age, sex=profile.sex,
        ward=profile.ward, comorbidities=list(profile.comorbidities), advance_directive=profile.advance_directive,
    )
    for note in profile.notes:
        state.notes.append({"t": note.t, "text": note.text})

    consensus_ema: float | None = None
    last_tier = "GREEN"
    first_alert: float | None = None
    peak = 0.0
    reached_red = False
    carbon = 0.0

    t = 0.0
    while t <= profile.duration:
        v = _noisy_vitals(profile, t, rng, sev)
        pf = fs.get(profile.patient_id); pf.add(v)
        lab = _latest_lab(profile, t)
        # realistic POCT measurement noise on lactate (drives occasional false alarms)
        if lab is not None and lab.lactate is not None:
            from dataclasses import replace
            lab = replace(lab, lactate=round(max(0.3, lab.lactate + rng.uniform(-0.28, 0.28)), 2))
        feats = pf.compute(t, lab)
        live = {a.name: a.assess(state, feats, profile) for a in AGENTS}
        state.agent_results = live
        raw = _consensus(live)
        consensus_ema = raw if consensus_ema is None else 0.3 * raw + 0.7 * consensus_ema
        tier = _tier(consensus_ema, live["differential"], profile, last_tier)
        if tier != last_tier and tier in ("YELLOW", "RED"):
            if first_alert is None:
                first_alert = t
            carbon += 0.15
        if tier == "RED":
            reached_red = True
        peak = max(peak, consensus_ema)
        last_tier = tier
        t += 1.0

    label = _label_for(profile)
    onset = profile.ground_truth.get("onset")
    if profile.advance_directive in ("COMFORT CARE", "DNR"):
        outcome_with = "dignity"; outcome_without = "over_resuscitated"
    elif label == 1:
        # survives if alerted within TTA cutoff of onset
        tta = (first_alert - onset) if (first_alert is not None and onset is not None) else None
        outcome_with = "survives" if (tta is not None and tta <= TTA_CUTOFF_MIN) else "dies"
        outcome_without = "dies"
    else:
        outcome_with = "stable" if not reached_red else "over_treated"
        outcome_without = "stable"

    return PatientRun(
        profile_id=profile.patient_id, name=profile.name, sex=profile.sex, age=profile.age,
        label=label, peak_consensus=round(peak, 2), first_alert_t=first_alert, onset=onset,
        reached_red=reached_red,
        comfort_care=profile.advance_directive in ("COMFORT CARE", "DNR"),
        outcome_with=outcome_with, outcome_without=outcome_without, carbon_g=round(carbon, 3),
        directive=profile.advance_directive,
    )


def _auroc(scores: list[float], labels: list[int]) -> float:
    if len(set(labels)) < 2:
        return float("nan")
    try:
        from sklearn.metrics import roc_auc_score
        return float(roc_auc_score(labels, scores))
    except Exception:
        return float("nan")


def compute_metrics(n: int = 80) -> dict[str, Any]:
    runs: list[PatientRun] = []
    rng = random.Random(123)
    for i in range(n):
        r = rng.random()
        if r < 0.50:
            profile = _gen_sepsis(rng, i)
        elif r < 0.78:
            profile = _gen_mimic(rng, i)
        else:
            profile = _gen_healthy(rng, i)
        runs.append(run_one(profile, seed=1000 + i * 7))

    # ── KPIs ──────────────────────────────────────────────────────────────
    septic = [r for r in runs if r.label == 1 and not r.comfort_care]
    noncase = [r for r in runs if r.label == 0]
    # TTA measured from clinical onset (patient already septic on arrival)
    def tta(r: PatientRun) -> float | None:
        if r.first_alert_t is not None and r.onset is not None:
            return r.first_alert_t - r.onset
        return None
    tp = sum(1 for r in septic if tta(r) is not None)                    # caught in window
    fn = sum(1 for r in septic if tta(r) is None)                        # never alerted
    fp = sum(1 for r in noncase if r.reached_red)                        # false RED
    tn = sum(1 for r in noncase if not r.reached_red)                    # correct silent
    sens = tp / (tp + fn) if (tp + fn) else 0.0
    spec = tn / (tn + fp) if (tn + fp) else 0.0

    alert_ttas = [tta(r) for r in septic if tta(r) is not None]
    median_tta = statistics.median(alert_ttas) if alert_ttas else None

    # override: clinicians reject false REDs (FP) + small noise on true REDs
    red_true = sum(1 for r in septic if r.reached_red)
    red_false = fp
    overrides = red_false + int(red_true * 0.05)
    total_red = red_true + red_false
    override_rate = overrides / total_red if total_red else 0.0

    # mortality reduction (relative) — septic cases saved by timely alert
    deaths_without = len(septic)  # without timely detection, all septic die
    deaths_with = sum(1 for r in septic if r.outcome_with == "dies")
    mort_reduction = (deaths_without - deaths_with) / deaths_without if deaths_without else 0.0

    # NPC-LS: net CO₂ saved per life saved (kg)
    lives_saved = deaths_without - deaths_with
    carbon_saved_kg = lives_saved * CARBON_PER_ICU_ADMISSION_KG
    carbon_used_g = sum(r.carbon_g for r in runs)
    npc_ls = (carbon_saved_kg - carbon_used_g / 1000.0) / lives_saved if lives_saved else 0.0

    overall_auroc = _auroc([r.peak_consensus for r in runs], [r.label for r in runs])

    # ── Equity: disaggregated AUROC ───────────────────────────────────────
    def group(filter_fn):
        sub = [r for r in runs if filter_fn(r)]
        return {
            "n": len(sub), "pos": sum(r.label for r in sub),
            "auroc": _auroc([r.peak_consensus for r in sub], [r.label for r in sub]),
        }
    equity = {
        "by_sex": {
            "F": group(lambda r: r.sex == "F"),
            "M": group(lambda r: r.sex == "M"),
        },
        "by_age": {
            "<40": group(lambda r: r.age < 40),
            "40-65": group(lambda r: 40 <= r.age <= 65),
            ">65": group(lambda r: r.age > 65),
        },
    }

    # ── Drift: AUROC over chronological windows + SPC limits ──────────────
    W = 5
    window_aurocs = []
    per = max(1, len(runs) // W)
    for wi in range(W):
        chunk = runs[wi * per: (wi + 1) * per] if wi < W - 1 else runs[wi * per:]
        window_aurocs.append(_auroc([r.peak_consensus for r in chunk], [r.label for r in chunk]))
    valid = [a for a in window_aurocs if a == a]  # drop NaN
    mean_a = statistics.mean(valid) if valid else 0.0
    std_a = statistics.pstdev(valid) if len(valid) > 1 else 0.0
    ucl, lcl = mean_a + 2 * std_a, mean_a - 2 * std_a
    drift_detected = any((a == a and a < 0.70) or (a == a and a < lcl) for a in window_aurocs)

    return {
        "n": n,
        "kpis": {
            "sensitivity": round(sens, 3),
            "specificity": round(spec, 3),
            "median_tta_min": round(median_tta, 1) if median_tta is not None else None,
            "override_rate": round(override_rate, 3),
            "mortality_reduction": round(mort_reduction, 3),
            "npc_ls_kg_per_life": round(npc_ls, 1),
            "overall_auroc": round(overall_auroc, 3),
            "targets": {  # brief section 9 targets
                "sensitivity": ">0.85", "specificity": ">0.70", "median_tta_min": "<=10",
                "override_rate": "<0.20", "mortality_reduction": ">=0.187",
            },
            "counts": {"tp": tp, "fn": fn, "fp": fp, "tn": tn, "septic": len(septic), "noncase": len(noncase)},
        },
        "equity": equity,
        "drift": {
            "windows": [round(a, 3) if a == a else None for a in window_aurocs],
            "mean": round(mean_a, 3), "ucl": round(ucl, 3), "lcl": round(lcl, 3),
            "drift_detected": drift_detected,
            "fallback": "qSOFA" if drift_detected else "none",
        },
        "carbon_used_g": round(carbon_used_g, 2),
        "grok_enabled": settings.grok_enabled,
    }


# ── cache ─────────────────────────────────────────────────────────────────
_cache: dict[int, dict[str, Any]] = {}


def get_metrics(n: int = 80) -> dict[str, Any]:
    if n not in _cache:
        _cache[n] = compute_metrics(n)
    return _cache[n]


def refresh(n: int = 80) -> dict[str, Any]:
    _cache.pop(n, None)
    return get_metrics(n)