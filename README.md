# SENTINEL Protocol — Live Demo System

A **fully working** multi-agent sepsis-detection system: a wearable-patch + POCT
**device simulator** streams vitals → a **FastAPI** backend runs the rolling
feature store + 4 specialist agents + orchestrator deliberation (powered by
**xAI Grok**) → a **self-contained dashboard** shows live vitals, the agent
council's reasoning, tiered alerts, HITL controls, and the audit trail.

This is not a mock — the data pipeline, agents, deliberation, alerts, and
human-in-the-loop responses are all real. The only simulated part is the
physical hardware (a wearable patch + i-STAT), replaced by a deterministic
device simulator that streams realistic vitals for 5 patient test cases.

---

## Quick start

```bash
cd /home/devansh/Documents/icam_friday
./sentinel/run.sh
# → http://localhost:8000
```

Or manually:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r sentinel/requirements.txt
cp sentinel/.env.example sentinel/.env      # then add your XAI_API_KEY
python -m sentinel.main
```

Open **http://localhost:8000** in a browser.

---

## Enable Grok (the AI)

The system runs **fully without a key** — a deterministic local-reasoning
fallback produces agent explanations so the demo always works. To get real
Grok deliberation from the 4 agents + orchestrator:

1. Get an xAI API key at https://console.x.ai
2. Edit `sentinel/.env`:
   ```
   XAI_API_KEY=sk-...
   XAI_MODEL=grok-4-fast        # grok-4 / grok-4-fast / grok-3 / grok-3-mini
   ```
3. Restart the server. The header chip flips from `AI: local fallback` to
   `AI: Grok · grok-4-fast`, and agent reasoning cards show a **GROK** badge.

The Carbon Budget Controller calls Grok only for ambiguous cases (the 80%
zero-LLM path stays free); the consensus and alert tier are always
deterministic, so alerts fire correctly even if Grok is slow or offline.

---

## The 6 patient test cases (all stream simultaneously)

| Patient | Ward | Case | Expected SENTINEL behaviour |
|---------|------|------|------------------------------|
| **Karen Whitfield** | ER | Sepsis (the headline case) | GREEN → 🟡 YELLOW (~t=30) → 🔴 RED (~t=44). Caught before collapse. |
| **Anya Petrova** | ICU | Septic shock | 🔴 RED early, de-escalates to YELLOW on recovery. |
| **Maria Sanchez** | ER | Pancreatitis **mimic** | Stays 🟢 GREEN — Differential agent rules out sepsis. |
| **Robert Hayes** | Surgical | Post-op stress **mimic** | Stays 🟢 GREEN — distinguished from sepsis. |
| **James Okafor** | General | Healthy control | Stays 🟢 GREEN — silent monitoring. |
| **Frank Doyle** | Palliative | Sepsis trajectory + **COMFORT CARE directive** | Stays 🟡 YELLOW — physiology says sepsis (consensus ~88) but the advance directive caps the tier and suppresses the aggressive bundle. The council debate leads with **dignity preservation vs the sepsis protocol**; the recommended action is a goals-of-care discussion + symptom relief, never cultures/antibiotics/bolus. The ethics/dignity dimension of the demo. |

---

## Demo runbook (matches the video narrative)

1. **Video plays** — patient wears the patch, info starts flowing.
2. **Open the webapp** (http://localhost:8000). With `DEMO_AUTOCONNECT=1` all
   patches are already streaming (data flowing the moment you open). Set
   `DEMO_AUTOCONNECT=0` in `.env` if you'd rather click **Connect patch** per
   patient to mirror the video live.
3. **Watch the grid** — 5 patients streaming vitals, sparklines, live SOFA.
4. **Click a patient** → drawer opens: live multi-series vitals chart, POCT
   labs, SOFA organ breakdown, the 4 agent cards (score/confidence/reasoning
   with GROK/LOCAL badge), orchestrator consensus, cross-examination, the
   recommended action, uncertainty, carbon cost, and HITL Accept/Modify/Reject.
5. **Karen escalates** GREEN→YELLOW→RED. Open Karen's drawer, hit **Accept** →
   outcome flips to *STABLE — SENTINEL intervention timely*. The
   **counterfactual** panel shows *with vs without SENTINEL*.
6. **Mimics stay GREEN** — open Maria/Robert: the Differential agent's
   reasoning shows the mimic ruled out (no unnecessary antibiotics).
7. **Frank — dignity vs protocol** — open Frank: consensus is high (~88,
   sepsis physiology) but the tier is capped at 🟡 YELLOW by the COMFORT CARE
   directive. The cross-examination leads with the orchestrator asking "do we
   push the bundle?" and answering "No — the directive is authoritative." The
   recommended action is goals-of-care + symptom relief, not the sepsis bundle.
   This is SENTINEL's ethics/dignity safeguard: it detects sepsis *and* refuses
   to override a stated goal of care. Accept → outcome *dignity preserved —
   comfort-care pathway honoured*; the counterfactual shows *aggressive
   resuscitation attempted against directive — prolonged suffering*.
8. **Governance panel** — audit trail of every tier change + clinician
   response; the *About this Agent* model card is always visible.

### Controls
- **Speed**: 0.5× / 1× / 2× / 4× (1 real second = N simulated minutes).
- **Pause/Resume**, **Connect all patches**.

---

## Architecture

```
sentinel/
  config.py          env + Grok/xAI settings
  grok_client.py     xAI (OpenAI-compatible) client + local fallback
  models.py          Vitals / Labs / AgentResult / AlertPayload / PatientState
  profiles.py        6 scripted patient test cases (keyframes + labs + notes)
  governance.py      data-trust layer: rolling-window input confidence + flags
  feature_store.py   rolling 15-min features + SOFA/qSOFA (+ governance trust)
  agents.py          SOFA Tracker, Differential, Predictor, Narrative + Carbon Budget
  orchestrator.py    3-stage deliberation, consensus, tier, alert payload
  simulator.py       device simulator + clinical loop + pub/sub broadcast
  audit.py           append-only audit trail (JSONL)
  ml/                trained ML models (XGBoost + TF-IDF) + trainer
    models.py        loader + feature contract + prediction helpers (with fallback)
    train.py         synthetic-data trainer — run: python -m sentinel.ml.train
    models/          serialized xgb_sepsis.joblib + note_concern.joblib
  main.py            FastAPI: REST + WebSocket + static dashboard
  static/            index.html, app.js, styles.css, chart.umd.min.js (offline)
  .env.example, run.sh
```

## ML models (trained, not faked)

Two real trained models drive the agents, with automatic rule fallback if a
model file is missing so the demo never breaks:

| Agent | Model | Trained on | Role |
|-------|-------|-----------|------|
| **Predictor** | XGBoost classifier | synthetic cohort from the 5 profiles (7,200 samples, 30 noise seeds × replay) | P(sepsis) drives the risk score |
| **Narrative** | TF-IDF + LogisticRegression | augmented nursing notes | P(note indicates concern) |
| SOFA Tracker | Sepsis-3 rule engine | — | SOFA/qSOFA scoring (rules by definition) |
| Differential | mimic rule engine | — | mimic rule-out (clinical logic) |

Retrain any time: `python -m sentinel.ml.train` (deterministic, ~10 s).
Holdout metrics print on training (XGBoost: AUC 1.00, sepsis recall 0.99 on the
synthetic holdout). The model correctly separates sepsis from mimics:
Karen → P(sepsis)=1.0 (RED), Maria (pancreatitis mimic) → P(sepsis)=0.0 (GREEN).

Data flow: `device simulator → governance (validate each reading) → feature store
(rolling window + trust score) → 4 agents (stage 1, deterministic, confidence
capped by data trust) → orchestrator (stage 2 cross-exam + stage 3 synthesis via
Grok, ambiguity raised on low trust) → alert payload (+ data_trust) → WebSocket
broadcast → dashboard`. Clinician response → audit + outcome tracker.

---

## Data governance & input trust (medical-grade trustworthiness)

A clinical AI must not make confident calls on untrustworthy input. SENTINEL
runs a **data-governance layer on top of the rolling feature window**
(`governance.py`) that attests how trustworthy each patient's feed is, every
tick, and propagates that trust everywhere a decision is made:

1. **Per-reading validation** — every vitals reading is checked for physiologic
   plausibility (HR 25–220, RR 4–40, SBP 40–250, temp 30–44 °C, SpO2 50–100),
   per-tick spikes (e.g. HR Δ > 35 in one tick), and flatline (sensor stuck:
   HR std < 0.5 over the last 6 readings). Implausible readings are flagged and
   tallied into a stay-wide pass-rate; they still enter the window so features
   never starve, but their effect is honest.
2. **Rolling-window confidence score (0–1)** — over the 15-minute window:
   coverage (% of window with data), feed freshness (gap since last reading),
   plausibility pass-rate, signal stability (flatline check), lab staleness
   (POCT lactate older than 30 min degrades trust), and a source-trust baseline
   (wearable patch 0.92, POCT 0.95 — a stand-in for device attestation in a
   pilot). Blended into one score with an auditable grade: **HIGH ≥ 0.85,
   MEDIUM ≥ 0.60, LOW < 0.60**.
3. **Trust propagates to every decision** — every agent's `confidence` is
   capped by `data_confidence` (no confident sepsis call on bad data); the
   orchestrator raises `ambiguity` when trust is low (→ more LLM/human review,
   never a confident auto-call); the alert payload carries `data_trust`; and a
   **LOW DATA TRUST** warning is appended to the differential note +
   uncertainty so the clinician sees it before acting.
4. **Visible + auditable** — the dashboard shows a **DATA · grade · %** chip on
   every patient card and a *Data governance* panel in the drawer (coverage /
   freshness / plausibility / stability / lab-age / source-trust bars + flags).
   Every trust-grade transition (e.g. MEDIUM → HIGH as the window fills) is
   written to the audit trail as a `governance` event with the flags that
   caused it.

In the scripted demo the feed is clean, so trust ramps MEDIUM → HIGH in the
first ~15 seconds per patient and stays HIGH — the message to the audience is
"SENTINEL attests the feed is trustworthy before it scores." The detection
paths (flatline, stale labs, implausible values, low coverage, stale feed) are
exercised by unit tests and logged via the audit trail.

---

## Notes / honest limitations

- SOFA uses practical proxies (SpO2 for PaO2/FiO2, MAP without vasopressors)
  from the demo data — it follows Sepsis-3 structure but is **not** a validated
  clinical device.
- Vitals are scripted + interpolated with reproducible noise (seed 42) so every
  demo run is identical and predictable.
- No EHR/HL7 integration (the #1 technical risk in the brief) — this is the
  standalone demo build, not the hospital pilot.# sentinal
# sentinal
