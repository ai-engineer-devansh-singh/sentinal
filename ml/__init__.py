"""Trained ML models for SENTINEL agents.

- XGBoost sepsis-risk classifier (used by the Predictor agent)
- TF-IDF + LogisticRegression nursing-note concern classifier (used by the
  Narrative agent)

Models are trained on synthetic data generated from the patient library
(see train.py) and serialized with joblib. The loader falls back gracefully
to the rule-based scores if a model file is missing or fails to load, so the
demo always runs. SOFA Tracker and Differential remain rule engines — SOFA
is a rule-based scoring system by definition (Sepsis-3), and mimic rule-out
is clinical logic, not a learned pattern.
"""
from .models import FEATURE_NAMES, features_to_vector, ml, note_classifier

__all__ = ["FEATURE_NAMES", "features_to_vector", "ml", "note_classifier"]