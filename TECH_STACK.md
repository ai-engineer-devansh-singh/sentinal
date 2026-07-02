# SENTINEL Protocol — Tech Stack & Project Detail

> **SENTINEL** is a fully-working, multi-agent sepsis-detection system built
> for a live demo (Erasmus Toulouse, July 3 2026). A wearable-patch + POCT
> **device simulator** streams vitals → a **FastAPI** backend runs a rolling
> feature store, 4 specialist agents, and an orchestrator deliberation council
> (powered by **xAI Grok**) → a **self-contained dashboard** shows live vitals,
> agent reasoning, tiered alerts, HITL controls, and an audit trail.
>
> This is not a mock: the data pipeline, agents, deliberation, alerts, and
> human-in-the-loop responses are all real. Only the physical hardware is
> simulated by a deterministic device simulator streaming 5 scripted patient
> cases.

---

## 1. At a glance

| Aspect | Choice |
|---|---|
| Language | Python 3.10 |
| Backend framework | FastAPI + Uvicorn (ASGI) |
| Real-time transport | Native WebSocket (asyncio queues, pub/sub broadcast) |
| LLM / deliberation | xAI **Grok** (`grok-4`, `grok-4-fast`) via OpenAI-compatible SDK, with deterministic **local-reasoning fallback** |
| ML (deterministic scoring) | XGBoost + scikit-learn (TF-IDF + LogisticRegression), persisted with joblib |
| Clinical scoring | Sepsis-3 rule engine: SOFA / qSOFA / shock index |
| Data layer | In-memory state + append-only JSONL audit trail (no DB) |
| Frontend | Vanilla HTML/CSS/JS, single page, offline `chart.umd.min.js` |
| Config | `.env` + `python-dotenv` (dataclass `Settings`) |
| Validation | Pydantic v2 (HTTP API surface) |
| Packaging | Run as a Python module (`python -m sentinel.main`) |

---

## 2. System architecture

```
sentinel/
  config.py          env + Grok/xAI settings (dataclass Settings)
  grok_client.py     xAI (OpenAI-compatible) async client + local fallback
  models.py          Vitals / Labs / NursingNote / AgentResult / AlertPayload / PatientState
  profiles.py        5 scripted patient test cases (keyframes + labs + notes)
  feature_store.py   rolling 15-min features + SOFA/qSOFA (deque-based)
  agents.py          SOFA Tracker, Differential, Predictor, Narrative + Carbon Budget Controller
  orchestrator.py    3-stage deliberation, consensus, tier, alert payload
  simulator.py       device simulator + clinical loop + pub/sub broadcast
  audit.py           append-only audit trail (JSONL)
  batch.py           synthetic cohort batch run → KPIs, equity, drift
  main.py            FastAPI: REST + WebSocket + static dashboard
  ml/                trained ML models + trainer
    models.py        loader + feature contract + prediction helpers (with fallback)
    train.py         synthetic-data trainer — run: python -m sentinel.ml.train
    models/          serialized xgb_sepsis.joblib + note_concern.joblib
  static/            index.html, app.js (904 lines), styles.css, chart.umd.min.js (offline)
  .env.example       configuration template
  run.sh             one-shot launcher (venv → pip → run)
  requirements.txt   pinned dependency list
  audit_trail.jsonl  persisted append-only event log
```

### Data flow
```
device simulator
  → feature store (rolling 15-min window, slopes/deltas/EWMA/SOFA/qSOFA)
  → 4 agents (stage 1, deterministic zero-LLM path)
  → orchestrator (stage 2 cross-exam + stage 3 synthesis via Grok)
  → alert payload (tier + consensus + recommended action)
  → WebSocket broadcast → dashboard
Clinician response (Accept/Modify/Reject) → audit trail + outcome tracker
```

---

## 3. Backend stack (Python)

### Runtime
- **Python 3.10** — type hints (`from __future__ import annotations`), dataclasses, asyncio.
- **FastAPI `>=0.110`** — REST + WebSocket endpoints, async-native.
- **Uvicorn `[standard] >=0.29`** — ASGI server.
- **Pydantic `>=2.7`** — request/response models for the REST API
  (`SpeedCmd`, `PauseCmd`, `RespondCmd`).
- **python-dotenv `>=1.0`** — loads `.env` into `Settings`.

### AI / LLM
- **openai SDK `>=1.30`** — used as an **OpenAI-compatible client** pointed at
  xAI's endpoint (`https://api.x.ai/v1`). `AsyncOpenAI` for non-blocking calls.
- **xAI Grok** models: `grok-4` (large, for hard cases) and `grok-4-fast`
  (default). Configurable via `XAI_MODEL` / `XAI_MODEL_LARGE`.
- **Local-reasoning fallback** — if no key, auth fails, or the call errors,
  a deterministic stub produces a clinician-style sentence so the demo always
  runs. Every result carries an `engine` field (`"grok" | "local"`) surfaced
  in the UI as a `GROK` / `LOCAL` badge.

### ML (trained, not faked)
- **XGBoost `>=2.0`** — sepsis-risk classifier (`xgb_sepsis.joblib`) driving
  the Predictor agent's P(sepsis). Trained on a synthetic cohort replayed
  from the 5 profiles (7,200 samples, 30 noise seeds). Holdout AUC 1.00,
  sepsis recall 0.99.
- **scikit-learn `>=1.4`** — TF-IDF + LogisticRegression nursing-note
  concern classifier (`note_concern.joblib`) driving the Narrative agent.
- **joblib `>=1.3`** — model serialization + lazy load with automatic
  rule fallback if a model file is missing.
- **numpy `>=1.26`** — feature vectors / probability handling.

### Clinical logic (rules, by definition deterministic)
- **Sepsis-3 SOFA/qSOFA** engine in `feature_store.py` — organ-dysfunction
  scoring (uses practical proxies: SpO₂ for PaO₂/FiO₂, MAP without
  vasopressors).
- **Mimic rule-out** engine — Differential agent rules out pancreatitis /
  post-op stress mimics.
- **Shock index**, **delta-SOFA vs baseline**, **EWMA** smoothing, and
  **hysteresis tier assignment** (higher bar to ENTER a tier than to LEAVE
  it) to suppress noise-driven flapping.

### Async / concurrency
- **asyncio** throughout — the simulator runs as a background task; full
  Grok deliberation runs on a throttle (`DELIB_INTERVAL = 4.0` sim minutes)
  in a background task so streaming never blocks on LLM latency.
- **asyncio.Queue per WebSocket subscriber** → pub/sub broadcast to all
  dashboards, with a 25s timeout → `ping` keepalive.

### Data persistence
- **In-memory state** (`PatientState` dataclasses) for live monitoring.
- **Append-only JSONL audit trail** (`audit_trail.jsonl`) — every alert,
  clinician response, and model invocation recorded (Governance / Checkpoint 4–5).
- No database — appropriate for a self-contained demo.

---

## 4. Frontend stack

- **Vanilla HTML / CSS / JavaScript** — no build step, no framework
  (`static/index.html` 136 lines, `app.js` 904 lines, `styles.css` 260 lines).
- **Offline charting** — vendored `chart.umd.min.js` so the dashboard works
  with no internet.
- **Fonts** — Inter + JetBrains Mono via Google Fonts (preconnect).
- **Live transport** — native `WebSocket` to `/ws`; immediate snapshot on
  connect so the dashboard hydrates instantly.
- **REST calls** — `fetch` to `/api/*` for HITL response, device connect/
  disconnect, speed/pause, metrics refresh.
- The only client-side state is a per-patient risk-history buffer for the
  trend/sparkline charts — all clinical values are streamed from the backend.

---

## 5. Multi-agent design (the core contribution)

Four specialist agents + an orchestrator deliberation council:

| Agent | Role | Engine |
|---|---|---|
| **SOFA Tracker** | Organ-dysfunction scoring (Sepsis-3) | rule engine |
| **Differential** | Mimic rule-out (pancreatitis, post-op stress) | rule engine |
| **Predictor** | P(sepsis) risk score | **XGBoost** classifier |
| **Narrative Analyst** | P(nursing note indicates concern) | **TF-IDF + LogReg** |
| **Carbon Budget Controller** | Picks LLM tier by case ambiguity (carbon-aware) | rule thresholds |
| **Orchestrator** | 3-stage deliberation, weighted consensus, tier, alert payload | Grok synthesis (with deterministic consensus/tier) |

### 3-stage deliberation
1. **Stage 1** — each agent's independent, deterministic assessment
   (the "zero-LLM" path that handles ~80% of inferences).
2. **Stage 2** — cross-examination: agents challenge each other's reasoning
   (Grok synthesis call).
3. **Stage 3** — synthesis: weighted consensus → GREEN / YELLOW / RED alert,
   recommended action, uncertainty flag, carbon cost.

**Hallucination-grounding gate:** the *consensus and tier remain
deterministic* — structured data drives the alert; the LLM only explains it.
Alerts fire correctly even if Grok is slow or offline.

### Carbon Budget Controller
Selects model tier by ambiguity:
- ambiguity < 0.30 → `ml_only` (no LLM, ~0.01 g CO₂)
- 0.30–0.70 → `ml+sml` (`grok-4-fast`, ~0.15 g CO₂)
- ≥ 0.70 → `ml+large` (`grok-4`, ~1.20 g CO₂)

---

## 6. REST + WebSocket API

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Self-contained dashboard |
| GET | `/api/health` | Liveness + Grok status |
| GET | `/api/config` | Runtime config (model, speed, autoconnect) |
| GET | `/api/state` | Full patient snapshot |
| GET | `/api/patients` | Patient list |
| POST | `/api/device/{id}/connect` | Connect a device patch |
| POST | `/api/device/{id}/disconnect` | Disconnect a patch |
| POST | `/api/device/connect_all` | Connect all patches |
| POST | `/api/speed` | Set demo speed (0.5×–4×) |
| POST | `/api/pause` | Pause/resume the simulator |
| POST | `/api/alert/{id}/respond` | HITL response (ACCEPT/MODIFY/REJECT) |
| GET | `/api/audit` | Append-only audit entries |
| GET | `/api/metrics` | Batch KPIs (sensitivity, specificity, TTA, equity, drift) |
| POST | `/api/metrics/refresh` | Recompute batch metrics |
| WS | `/ws` | Live state stream (vitals, agents, alerts, outcomes) |

### Batch metrics (`batch.py`)
Runs N synthetic patients through the *real* pipeline and aggregates:
- **KPIs**: sensitivity, specificity, median time-to-alert, override rate,
  mortality reduction vs no-SENTINEL, NPC-LS (net carbon saved / life).
- **Equity**: AUROC disaggregated by sex and age bucket (Checkpoint 2 / SDG 10).
- **Drift**: AUROC over chronological windows + SPC control limits + qSOFA
  auto-fallback flag (Checkpoint 3).

---

## 7. Configuration (`.env`)

| Var | Default | Purpose |
|---|---|---|
| `XAI_API_KEY` | *(blank)* | Grok key — blank = local fallback |
| `XAI_BASE_URL` | `https://api.x.ai/v1` | xAI OpenAI-compatible endpoint |
| `XAI_MODEL` | `grok-4-fast` | Default reasoning model |
| `XAI_MODEL_LARGE` | `grok-4` | Escalation model for hard cases |
| `DEMO_SPEED` | `1.0` | 1 real sec = N sim minutes |
| `DEMO_HOST` | `0.0.0.0` | Bind host |
| `DEMO_PORT` | `8000` | Bind port |
| `DEMO_AUTOCONNECT` | `1` | Auto-stream all patches on startup |

---

## 8. The 5 patient test cases (all stream simultaneously)

| Patient | Ward | Case | Expected behaviour |
|---|---|---|---|
| **Karen Whitfield** | ER | Sepsis (headline) | GREEN → YELLOW (~t=30) → RED (~t=44). Caught before collapse. |
| **Anya Petrova** | ICU | Septic shock | RED early, de-escalates to YELLOW on recovery. |
| **Maria Sanchez** | ER | Pancreatitis **mimic** | Stays GREEN — Differential rules out sepsis. |
| **Robert Hayes** | Surgical | Post-op stress **mimic** | Stays GREEN — distinguished from sepsis. |
| **James Okafor** | General | Healthy control | Stays GREEN — silent monitoring. |

---

## 9. Run / build

### One-shot
```bash
cd /home/devansh/Documents/icam_friday
./sentinel/run.sh
# → http://localhost:8000
```

### Manual
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r sentinel/requirements.txt
cp sentinel/.env.example sentinel/.env      # then add XAI_API_KEY for Grok
python -m sentinel.main
```

### Retrain ML models
```bash
python -m sentinel.ml.train     # deterministic (~10 s), prints holdout metrics
```

### Installed (verified in current env)
FastAPI 0.128 · Uvicorn 0.31 · openai 2.34 · Pydantic 2.12 · xgboost 3.2 ·
scikit-learn 1.7 · joblib 1.5 · numpy 1.26 · httpx 0.28.

---

## 10. Honest limitations

- SOFA uses practical proxies (SpO₂ for PaO₂/FiO₂, MAP without vasopressors)
  from demo data — follows Sepsis-3 structure but is **not** a validated
  clinical device.
- Vitals are scripted + interpolated with reproducible noise (seed 42) so
  every demo run is identical and predictable.
- **No EHR/HL7 integration** (the #1 technical risk in the brief) — this is
  the standalone demo build, not the hospital pilot.
- No database; state is in-memory and the audit log is a local JSONL file.
```