"""ML model loader + feature contract + prediction helpers with fallback.

Both models are loaded lazily on first use. If anything is missing/broken the
`available` flag stays False and the agents fall back to their rule scores —
the demo never breaks. Run `python -m sentinel.ml.train` to (re)train.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("sentinel.ml")

MODELS_DIR = Path(__file__).resolve().parent / "models"
RISK_PATH = MODELS_DIR / "xgb_sepsis.joblib"
NOTE_PATH = MODELS_DIR / "note_concern.joblib"

# Feature contract — order matters and must match training (train.py).
FEATURE_NAMES = [
    "hr_now", "rr_now", "sbp_now", "map_now", "temp_now", "spo2_now", "shock_index",
    "hr_slope", "rr_slope", "temp_slope", "sbp_delta_1h",
    "sofa_score", "delta_from_baseline", "qsofa_met",
    "lactate", "wbc", "creatinine", "bilirubin", "platelets", "gcs",
]

# Defaults used when a lab hasn't arrived yet (keeps the vector well-formed).
_DEFAULTS = {
    "hr_now": 80, "rr_now": 16, "sbp_now": 120, "map_now": 90, "temp_now": 37.0,
    "spo2_now": 98, "shock_index": 0.7, "hr_slope": 0, "rr_slope": 0, "temp_slope": 0,
    "sbp_delta_1h": 0, "sofa_score": 0, "delta_from_baseline": 0, "qsofa_met": 0,
    "lactate": 1.0, "wbc": 8.0, "creatinine": 0.9, "bilirubin": 12.0, "platelets": 250, "gcs": 15,
}


def features_to_vector(f: dict[str, Any]) -> list[float]:
    """Convert a feature-store dict into the model's input vector."""
    out = []
    for name in FEATURE_NAMES:
        val = f.get(name)
        if val is None or val == "":
            val = _DEFAULTS.get(name, 0)
        if name == "qsofa_met":
            val = 1 if val else 0
        out.append(float(val))
    return out


class RiskModel:
    """XGBoost sepsis-risk classifier wrapper."""

    def __init__(self) -> None:
        self.available = False
        self.model = None
        self._load()

    def _load(self) -> None:
        if not RISK_PATH.exists():
            log.info("XGBoost risk model not found at %s — using rule fallback. Run: python -m sentinel.ml.train", RISK_PATH)
            return
        try:
            import joblib
            bundle = joblib.load(RISK_PATH)
            self.model = bundle["model"]
            self.feature_names = bundle.get("feature_names", FEATURE_NAMES)
            self.available = True
            log.info("XGBoost risk model loaded (features=%d)", len(self.feature_names))
        except Exception as e:
            log.warning("XGBoost load failed: %s — using rule fallback", e)

    def predict_proba(self, f: dict[str, Any]) -> float:
        """Return P(sepsis) in [0,1], or -1 if unavailable (→ caller falls back)."""
        if not self.available:
            return -1.0
        try:
            vec = features_to_vector(f)
            import numpy as np
            proba = self.model.predict_proba([vec])[0]
            return float(proba[1]) if hasattr(proba, "__len__") else float(proba)
        except Exception as e:
            log.warning("risk predict failed: %s", e)
            return -1.0


class NoteModel:
    """TF-IDF + LogisticRegression nursing-note concern classifier."""

    def __init__(self) -> None:
        self.available = False
        self.model = None
        self._load()

    def _load(self) -> None:
        if not NOTE_PATH.exists():
            log.info("Note concern model not found at %s — using rule fallback.", NOTE_PATH)
            return
        try:
            import joblib
            b = joblib.load(NOTE_PATH)
            self.clf = b["clf"]
            self.vec = b["vectorizer"]
            self.available = True
            log.info("Note concern model loaded")
        except Exception as e:
            log.warning("note model load failed: %s — using rule fallback", e)

    def predict_concern(self, text: str) -> float:
        """Return P(note indicates concern) in [0,1], or -1 if unavailable."""
        if not self.available or not text:
            return -1.0
        try:
            Xt = self.vec.transform([text])
            proba = self.clf.predict_proba(Xt)[0]
            return float(proba[1])
        except Exception as e:
            log.warning("note predict failed: %s", e)
            return -1.0


ml = RiskModel()
note_classifier = NoteModel()