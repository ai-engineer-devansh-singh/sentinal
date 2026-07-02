"""Train SENTINEL's ML models on synthetic data from the patient library.

Generates feature samples by replaying each profile with many noise seeds,
labels them by ground truth, and trains:
  1. XGBoost sepsis-risk classifier  -> models/xgb_sepsis.joblib
  2. TF-IDF + LogisticRegression note-concern classifier -> models/note_concern.joblib

Run:  python -m sentinel.ml.train
Deterministic (fixed seeds). Prints metrics so you can see it actually learned.
"""
from __future__ import annotations

import random
from collections import Counter

import numpy as np

from ..feature_store import FeatureStore, _sofa, _qsofa
from ..models import LabResult
from ..profiles import PROFILES, PatientProfile

SEED = 7
RNG = random.Random(SEED)

# ── noisy vitals sampler (varied per seed, unlike the fixed demo RNG) ──────
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


def noisy_vitals(profile, t, rng):
    f = _interp(profile, t)
    n = lambda b, amp: b + rng.uniform(-amp, amp)
    v = type("V", (), {
        "t": t, "hr": n(f["hr"], 3), "rr": n(f["rr"], 1.2), "sbp": n(f["sbp"], 3.5),
        "dbp": n(f["dbp"], 2.2), "temp": f["temp"] + rng.uniform(-0.1, 0.1),
        "spo2": min(100, f["spo2"] + rng.uniform(-0.5, 0.5)),
    })()
    v.map = v.dbp + (v.sbp - v.dbp) / 3.0
    return v


def latest_lab(profile, t):
    latest = None
    for lab in profile.labs:
        if lab.t <= t:
            latest = lab
    return latest


# ── generate feature samples ──────────────────────────────────────────────
def generate_samples(n_seeds=30, step=2.0):
    X, y = [], []
    for profile in PROFILES:
        gt = profile.ground_truth
        is_sepsis_case = gt.get("category") in ("sepsis", "septic_shock")
        onset = 0.30 * (gt.get("first_alert_at") or 30) if is_sepsis_case else None
        for seed in range(n_seeds):
            rng = random.Random(SEED * 1000 + seed + hash(profile.patient_id) % 1000)
            # small per-run bias so trajectories vary across seeds
            bias = {k: rng.uniform(-0.9, 0.9) for k in ("hr", "rr", "sbp", "temp")}
            fs = FeatureStore()
            t = 0.0
            while t <= profile.duration:
                vit = noisy_vitals(profile, t, rng)
                # apply per-run bias to make seeds genuinely different
                vit.hr += bias["hr"]; vit.rr += bias["rr"]
                vit.sbp += bias["sbp"]; vit.temp += bias["temp"]
                pf = fs.get(profile.patient_id); pf.add(vit)
                lab = latest_lab(profile, t)
                feats = pf.compute(t, lab)
                from .models import features_to_vector
                vec = features_to_vector(feats)
                label = 1 if (is_sepsis_case and t >= onset) else 0
                X.append(vec); y.append(label)
                t += step
    return np.array(X), np.array(y)


# ── note concern dataset (with augmentation) ──────────────────────────────
POS_NOTES = [
    "Patient looks more unwell, mildly confused. Family says no fever at home.",
    "Patient drowsy, BP low, skin clammy. Awaiting physician review.",
    "Patient increasingly confused, cold peripheries, high lactate.",
    "Patient appears septic, tachycardic, hypotensive, oliguric.",
    "Deteriorating fast, mottled skin, barely responsive.",
]
NEG_NOTES = [
    "Patient arrived with sprained ankle, appears comfortable and in no distress.",
    "Admitted for observation after a minor fall. Comfortable, eating normally.",
    "Post-op Day 2 from colectomy. Mild pain, afebrile overnight.",
    "Recovering well, ambulated. Inflammatory response expected post-surgery.",
    "Pain improved after analgesia. Lipase elevated, consistent with pancreatitis.",
    "Severe epigastric pain radiating to back, nausea. Known gallstones.",
    "Stable overnight, vitals within normal limits, no acute concerns.",
]
FILLER = ["noted", "this morning", "on the ward", "per nursing assessment", "currently",
          "patient", "remains", "denies", "reports", "observed"]


def augment_notes(notes, label, n=40, rng=RNG):
    out = []
    for _ in range(n):
        base = rng.choice(notes)
        words = base.split()
        # random word dropout
        kept = [w for w in words if rng.random() > 0.18]
        if rng.random() > 0.5 and kept:
            kept.insert(rng.randrange(len(kept) + 1), rng.choice(FILLER))
        out.append((" ".join(kept) if kept else base, label))
    return out


def generate_notes():
    data = augment_notes(POS_NOTES, 1, n=60) + augment_notes(NEG_NOTES, 0, n=60)
    texts = [d[0] for d in data]
    labels = np.array([d[1] for d in data])
    return texts, labels


# ── train ─────────────────────────────────────────────────────────────────
def train():
    from .models import MODELS_DIR, RISK_PATH, NOTE_PATH
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    print("[1/2] Generating feature samples…")
    X, y = generate_samples()
    counts = Counter(y.tolist())
    print(f"      samples={len(y)}  pos={counts[1]}  neg={counts[0]}  ({counts[1]/len(y)*100:.1f}% positive)")

    print("[1/2] Training XGBoost sepsis-risk classifier…")
    import joblib
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import roc_auc_score, classification_report, accuracy_score
    from xgboost import XGBClassifier

    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2, random_state=SEED, stratify=y)
    spw = (len(ytr) - sum(ytr)) / max(sum(ytr), 1)
    xgb = XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.08, subsample=0.9,
        colsample_bytree=0.9, scale_pos_weight=spw, eval_metric="logloss",
        random_state=SEED, n_jobs=2, reg_lambda=1.0,
    )
    xgb.fit(Xtr, ytr)
    pred = xgb.predict(Xte)
    proba = xgb.predict_proba(Xte)[:, 1]
    print(f"      holdout accuracy={accuracy_score(yte, pred):.3f}  AUC={roc_auc_score(yte, proba):.3f}")
    print(classification_report(yte, pred, target_names=["non-sepsis", "sepsis"], digits=3))
    joblib.dump({"model": xgb, "feature_names": __import__("sentinel.ml.models", fromlist=["FEATURE_NAMES"]).FEATURE_NAMES}, RISK_PATH)
    print(f"      saved -> {RISK_PATH}")

    print("[2/2] Training TF-IDF + LogisticRegression note-concern classifier…")
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    texts, labels = generate_notes()
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True)
    Xn = vec.fit_transform(texts)
    clf = LogisticRegression(max_iter=400, C=2.0, random_state=SEED)
    clf.fit(Xn, labels)
    npred = clf.predict(Xn)
    print(f"      train accuracy={accuracy_score(labels, npred):.3f}  (samples={len(labels)})")
    joblib.dump({"clf": clf, "vectorizer": vec}, NOTE_PATH)
    print(f"      saved -> {NOTE_PATH}")
    print("\nTraining complete. Models will auto-load on next server start.")


if __name__ == "__main__":
    train()