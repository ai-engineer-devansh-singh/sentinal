"""Mock patient library — 5 scripted test cases that drive the device simulator.

Each profile is a timeline of vitals keyframes (linearly interpolated by the
simulator) plus scheduled lab results and nursing notes. Cases are chosen to
exercise every agent: a real sepsis (Karen), a fast septic shock (Anya), a
pancreatitis mimic (Maria), post-op stress (Robert), and a healthy control
(James). "ground_truth" is what *should* happen — used by the outcome tracker
and the counterfactual view, never by the agents.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from .models import LabResult, NursingNote, Vitals

RNG = random.Random(42)  # deterministic noise so demos are reproducible


@dataclass
class PatientProfile:
    patient_id: str
    name: str
    age: int
    sex: str
    ward: str
    comorbidities: list[str]
    advance_directive: str
    keyframes: dict[float, dict[str, float]]  # t_minutes -> vitals
    labs: list[LabResult] = field(default_factory=list)
    notes: list[NursingNote] = field(default_factory=list)
    mimic_tag: str | None = None  # pancreatitis / postop_stress / None
    ground_truth: dict[str, Any] = field(default_factory=dict)
    duration: float = 95.0  # simulated minutes the patch streams


# ── Karen — the headline sepsis case ──────────────────────────────────────
KAREN = PatientProfile(
    patient_id="anon-12345",
    name="Karen Whitfield",
    age=54,
    sex="F",
    ward="Emergency Ward",
    comorbidities=["Ankle sprain (presenting)"],
    advance_directive="FULL CODE",
    keyframes={
        0:  dict(hr=92,  rr=18, sbp=128, dbp=80, temp=37.2, spo2=97),
        15: dict(hr=98,  rr=20, sbp=122, dbp=78, temp=37.6, spo2=97),
        30: dict(hr=106, rr=22, sbp=110, dbp=70, temp=38.2, spo2=95),
        45: dict(hr=114, rr=26, sbp=94,  dbp=58, temp=38.9, spo2=93),
        51: dict(hr=122, rr=28, sbp=88,  dbp=54, temp=39.1, spo2=91),
        60: dict(hr=130, rr=30, sbp=82,  dbp=50, temp=39.0, spo2=89),
        75: dict(hr=126, rr=26, sbp=98,  dbp=62, temp=38.4, spo2=93),  # w/ intervention
        90: dict(hr=110, rr=22, sbp=108, dbp=68, temp=37.9, spo2=96),  # stabilizes
    },
    labs=[
        LabResult(t=30, lactate=2.4, wbc=12.5, creatinine=1.1, bilirubin=18, platelets=220, gcs=15),
        LabResult(t=45, lactate=3.8, wbc=15.2, creatinine=1.4, bilirubin=34, platelets=180, gcs=14),
        LabResult(t=60, lactate=4.6, wbc=18.0, creatinine=1.8, bilirubin=40, platelets=150, gcs=13),
        LabResult(t=75, lactate=2.2, wbc=13.5, creatinine=1.3, bilirubin=30, platelets=195, gcs=15),  # recovering (post-intervention)
        LabResult(t=90, lactate=1.6, wbc=10.5, creatinine=1.1, bilirubin=24, platelets=225, gcs=15),  # stabilising
    ],
    notes=[
        NursingNote(t=10, text="Patient arrived with sprained ankle, appears comfortable and in no distress."),
        NursingNote(t=40, text="Patient looks more unwell, mildly confused. Family says no fever at home."),
        NursingNote(t=58, text="Patient drowsy, BP low, skin clammy. Awaiting physician review."),
    ],
    ground_truth={
        "category": "sepsis",
        "expected_peak_tier": "RED",
        "first_alert_at": 51,  # ~08:51
        "with_sentinel": "survives — cultures + antibiotics at 08:55, stabilizes by 09:30",
        "without_sentinel": "dies at 09:15 — EWS normal at every checkpoint",
    },
)

# ── Anya — fast septic shock (ICU) ────────────────────────────────────────
ANYA = PatientProfile(
    patient_id="anon-22310",
    name="Anya Petrova",
    age=67,
    sex="F",
    ward="ICU",
    comorbidities=["Type 2 Diabetes", "Chronic Kidney Disease"],
    advance_directive="FULL CODE",
    keyframes={
        0:  dict(hr=110, rr=24, sbp=100, dbp=60, temp=38.5, spo2=94),
        10: dict(hr=128, rr=28, sbp=86,  dbp=52, temp=39.2, spo2=90),
        20: dict(hr=140, rr=32, sbp=76,  dbp=46, temp=39.4, spo2=86),
        30: dict(hr=138, rr=30, sbp=84,  dbp=50, temp=39.0, spo2=89),
        45: dict(hr=124, rr=26, sbp=96,  dbp=58, temp=38.5, spo2=92),
    },
    labs=[
        LabResult(t=8,  lactate=4.2, wbc=16.0, creatinine=2.0, bilirubin=30, platelets=170, gcs=13),
        LabResult(t=18, lactate=5.5, wbc=19.0, creatinine=2.4, bilirubin=38, platelets=140, gcs=12),
        LabResult(t=30, lactate=3.6, wbc=17.0, creatinine=2.1, bilirubin=32, platelets=160, gcs=14),
        LabResult(t=45, lactate=2.0, wbc=13.0, creatinine=1.6, bilirubin=26, platelets=190, gcs=15),  # responding
        LabResult(t=60, lactate=1.4, wbc=10.5, creatinine=1.2, bilirubin=20, platelets=220, gcs=15),  # stabilising
    ],
    notes=[
        NursingNote(t=4,  text="Admitted from ward with suspected pneumonia. Diabetic, oliguric."),
        NursingNote(t=16, text="Patient increasingly confused, cold peripheries, high lactate."),
    ],
    ground_truth={
        "category": "septic_shock",
        "expected_peak_tier": "RED",
        "first_alert_at": 10,
        "with_sentinel": "survives — early vasopressors + cultures within 30 min",
        "without_sentinel": "prolonged shock, multi-organ failure",
    },
)

# ── Maria — pancreatitis mimic ────────────────────────────────────────────
MARIA = PatientProfile(
    patient_id="anon-30814",
    name="Maria Sanchez",
    age=48,
    sex="F",
    ward="Emergency Ward",
    comorbidities=["Gallstones", "Obesity"],
    advance_directive="FULL CODE",
    mimic_tag="pancreatitis",
    keyframes={
        0:  dict(hr=96,  rr=18, sbp=124, dbp=78, temp=37.4, spo2=98),
        20: dict(hr=104, rr=20, sbp=116, dbp=74, temp=38.0, spo2=97),
        40: dict(hr=108, rr=21, sbp=112, dbp=72, temp=38.3, spo2=97),
        60: dict(hr=100, rr=19, sbp=120, dbp=76, temp=37.8, spo2=98),
        80: dict(hr=92,  rr=17, sbp=122, dbp=77, temp=37.4, spo2=98),
    },
    labs=[
        LabResult(t=10, lactate=1.1, wbc=11.0, creatinine=0.9, bilirubin=22, platelets=240, gcs=15),
        LabResult(t=30, lactate=1.2, wbc=14.2, creatinine=1.0, bilirubin=28, platelets=230, gcs=15),
        LabResult(t=50, lactate=1.0, wbc=12.0, creatinine=0.9, bilirubin=24, platelets=245, gcs=15),
    ],
    notes=[
        NursingNote(t=5,  text="Severe epigastric pain radiating to back, nausea. Known gallstones."),
        NursingNote(t=35, text="Pain improved after analgesia. Lipase markedly elevated — pancreatitis."),
    ],
    ground_truth={
        "category": "mimic",
        "mimic": "pancreatitis",
        "expected_peak_tier": "GREEN",
        "first_alert_at": None,
        "with_sentinel": "correctly NOT flagged as sepsis — mimic ruled out",
        "without_sentinel": "unnecessary sepsis workup / antibiotics",
    },
)

# ── Robert — post-op Day 2 stress ─────────────────────────────────────────
ROBERT = PatientProfile(
    patient_id="anon-44501",
    name="Robert Hayes",
    age=61,
    sex="M",
    ward="Surgical Ward",
    comorbidities=["Post-op Day 2 (colectomy)"],
    advance_directive="FULL CODE",
    mimic_tag="postop_stress",
    keyframes={
        0:  dict(hr=92,  rr=18, sbp=130, dbp=82, temp=37.3, spo2=98),
        20: dict(hr=102, rr=20, sbp=126, dbp=80, temp=37.8, spo2=97),
        40: dict(hr=104, rr=20, sbp=122, dbp=78, temp=38.0, spo2=97),
        60: dict(hr=98,  rr=18, sbp=128, dbp=80, temp=37.6, spo2=98),
        80: dict(hr=90,  rr=17, sbp=130, dbp=82, temp=37.2, spo2=98),
    },
    labs=[
        LabResult(t=15, lactate=1.3, wbc=12.8, creatinine=1.0, bilirubin=16, platelets=250, gcs=15),
        LabResult(t=45, lactate=1.2, wbc=11.0, creatinine=1.0, bilirubin=15, platelets=255, gcs=15),
    ],
    notes=[
        NursingNote(t=0,  text="Post-op Day 2 from colectomy. Mild pain, afebrile overnight."),
        NursingNote(t=30, text="Recovering well, ambulated. Inflammatory response expected post-surgery."),
    ],
    ground_truth={
        "category": "mimic",
        "mimic": "postop_stress",
        "expected_peak_tier": "GREEN",
        "first_alert_at": None,
        "with_sentinel": "correctly NOT flagged — post-op stress distinguished from sepsis",
        "without_sentinel": "false sepsis alert, unnecessary bundle",
    },
)

# ── James — healthy control ───────────────────────────────────────────────
JAMES = PatientProfile(
    patient_id="anon-50992",
    name="James Okafor",
    age=29,
    sex="M",
    ward="General Ward",
    comorbidities=[],
    advance_directive="FULL CODE",
    keyframes={
        0:  dict(hr=74, rr=16, sbp=118, dbp=74, temp=36.8, spo2=99),
        30: dict(hr=78, rr=16, sbp=120, dbp=75, temp=36.9, spo2=99),
        60: dict(hr=72, rr=15, sbp=116, dbp=73, temp=36.7, spo2=99),
        90: dict(hr=76, rr=16, sbp=118, dbp=74, temp=36.8, spo2=99),
    },
    labs=[
        LabResult(t=10, lactate=0.9, wbc=7.2, creatinine=0.8, bilirubin=12, platelets=260, gcs=15),
    ],
    notes=[
        NursingNote(t=5, text="Admitted for observation after a minor fall. Comfortable, eating normally."),
    ],
    ground_truth={
        "category": "healthy",
        "expected_peak_tier": "GREEN",
        "first_alert_at": None,
        "with_sentinel": "stable — silent monitoring",
        "without_sentinel": "stable",
    },
)

# ── Frank — sepsis trajectory but COMFORT CARE directive (ethics / dignity) ─
FRANK = PatientProfile(
    patient_id="anon-66127",
    name="Frank Doyle",
    age=78,
    sex="M",
    ward="Palliative Care",
    comorbidities=["Metastatic cancer", "Advance directive: COMFORT CARE"],
    advance_directive="COMFORT CARE",
    keyframes={
        0:  dict(hr=98,  rr=18, sbp=116, dbp=70, temp=37.0, spo2=95),
        20: dict(hr=108, rr=22, sbp=104, dbp=64, temp=38.0, spo2=93),
        40: dict(hr=120, rr=26, sbp=92,  dbp=56, temp=38.8, spo2=90),
        60: dict(hr=128, rr=28, sbp=84,  dbp=52, temp=39.0, spo2=88),
        80: dict(hr=126, rr=26, sbp=88,  dbp=54, temp=38.7, spo2=89),
    },
    labs=[
        LabResult(t=20, lactate=1.8, wbc=13.0, creatinine=1.3, bilirubin=22, platelets=210, gcs=14),
        LabResult(t=40, lactate=3.2, wbc=16.0, creatinine=1.7, bilirubin=30, platelets=170, gcs=13),
        LabResult(t=60, lactate=4.0, wbc=18.0, creatinine=2.0, bilirubin=36, platelets=150, gcs=12),
    ],
    notes=[
        NursingNote(t=5,  text="Metastatic cancer, comfort-care pathway. Family aware goals of care."),
        NursingNote(t=35, text="Patient deteriorating — but directive prohibits aggressive resuscitation."),
    ],
    ground_truth={
        "category": "sepsis_comfort_care",
        "expected_peak_tier": "YELLOW",  # RED suppressed by directive
        "first_alert_at": 40,
        "with_sentinel": "dignity preserved — comfort-care pathway honoured, no aggressive bundle pushed",
        "without_sentinel": "aggressive resuscitation attempted against directive — prolonged suffering",
    },
)


PROFILES: list[PatientProfile] = [KAREN, ANYA, MARIA, ROBERT, JAMES, FRANK]
PROFILES_BY_ID: dict[str, PatientProfile] = {p.patient_id: p for p in PROFILES}


def sample_vitals(profile: PatientProfile, t: float) -> Vitals:
    """Linearly interpolate vitals at time t (minutes), plus reproducible noise."""
    times = sorted(profile.keyframes)
    if t <= times[0]:
        frame = profile.keyframes[times[0]]
    elif t >= times[-1]:
        frame = profile.keyframes[times[-1]]
    else:
        lo, hi = times[0], times[-1]
        for i in range(len(times) - 1):
            if times[i] <= t <= times[i + 1]:
                lo, hi = times[i], times[i + 1]
                break
        a = (t - lo) / (hi - lo) if hi > lo else 0.0
        f0, f1 = profile.keyframes[lo], profile.keyframes[hi]
        frame = {k: f0[k] + (f1[k] - f0[k]) * a for k in f0}

    # small physiologic noise (deterministic via RNG)
    def n(base: float, amp: float) -> float:
        return round(base + RNG.uniform(-amp, amp), 2)

    return Vitals(
        t=round(t, 2),
        hr=n(frame["hr"], 2.5),
        rr=n(frame["rr"], 1.0),
        sbp=n(frame["sbp"], 3.0),
        dbp=n(frame["dbp"], 2.0),
        temp=round(frame["temp"] + RNG.uniform(-0.08, 0.08), 2),
        spo2=round(min(100, frame["spo2"] + RNG.uniform(-0.4, 0.4)), 1),
    )


def labs_at(profile: PatientProfile, t: float) -> LabResult | None:
    """Most recent lab result at or before t, or None."""
    latest = None
    for lab in profile.labs:
        if lab.t <= t:
            latest = lab
    return latest


def new_notes(profile: PatientProfile, t_prev: float, t_now: float) -> list[NursingNote]:
    return [n for n in profile.notes if t_prev < n.t <= t_now]