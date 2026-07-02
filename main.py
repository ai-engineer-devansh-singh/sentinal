"""SENTINEL FastAPI backend.

Serves the self-contained dashboard, exposes the HITL + device-control REST
API, and streams live state (vitals, agent results, alerts, outcomes) to the
dashboard over a WebSocket. The device simulator runs in the background.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import simulator as sim_mod
from .audit import audit
from .batch import get_metrics, refresh as refresh_metrics
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("sentinel.main")

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="SENTINEL Protocol", version="1.0")
sim = sim_mod.Simulator(autoconnect=settings.demo_autoconnect, speed=settings.demo_speed)


@app.on_event("startup")
async def _startup() -> None:
    await sim.start()
    log.info("SENTINEL simulator started | grok_enabled=%s model=%s speed=%.2f autoconnect=%s",
             settings.grok_enabled, settings.xai_model, settings.demo_speed, settings.demo_autoconnect)


# ── static dashboard ──────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ── REST API ───────────────────────────────────────────────────────────────
class SpeedCmd(BaseModel):
    speed: float


class PauseCmd(BaseModel):
    paused: bool


class RespondCmd(BaseModel):
    action: str  # ACCEPT | MODIFY | REJECT
    reason: str = ""
    clinician_id: str = "demo-nurse"


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "grok_enabled": settings.grok_enabled,
        "grok_model": settings.xai_model,
        "grok_base_url": settings.xai_base_url,
        "speed": sim.speed,
        "paused": sim.paused,
    }


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return {
        "grok_enabled": settings.grok_enabled,
        "grok_model": settings.xai_model,
        "grok_model_large": settings.xai_model_large,
        "grok_base_url": settings.xai_base_url,
        "speed": sim.speed,
        "paused": sim.paused,
        "autoconnect": settings.demo_autoconnect,
    }


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    return {"patients": sim.snapshot(), "speed": sim.speed, "paused": sim.paused,
            "grok_enabled": settings.grok_enabled}


@app.get("/api/patients")
async def get_patients() -> list[dict[str, Any]]:
    return sim.snapshot()


@app.post("/api/device/{patient_id}/connect")
async def connect_device(patient_id: str) -> dict[str, Any]:
    ok = sim.connect(patient_id)
    return {"ok": ok, "connected": ok}


@app.post("/api/device/{patient_id}/disconnect")
async def disconnect_device(patient_id: str) -> dict[str, Any]:
    sim.disconnect(patient_id)
    return {"ok": True}


@app.post("/api/device/connect_all")
async def connect_all() -> dict[str, Any]:
    results = {pid: sim.connect(pid) for pid in sim.states}
    return {"ok": True, "connected": results}


@app.post("/api/speed")
async def set_speed(cmd: SpeedCmd) -> dict[str, Any]:
    sim.set_speed(cmd.speed)
    return {"ok": True, "speed": sim.speed}


@app.post("/api/pause")
async def set_pause(cmd: PauseCmd) -> dict[str, Any]:
    sim.set_paused(cmd.paused)
    return {"ok": True, "paused": sim.paused}


@app.post("/api/alert/{patient_id}/respond")
async def respond_alert(patient_id: str, cmd: RespondCmd) -> dict[str, Any]:
    return sim.respond(patient_id, cmd.action.upper(), cmd.reason, cmd.clinician_id)


@app.get("/api/audit")
async def get_audit() -> dict[str, Any]:
    return {"entries": audit.all()}


# ── batch metrics (KPIs + equity + drift) ──────────────────────────────────
@app.get("/api/metrics")
async def metrics(n: int = 120) -> dict[str, Any]:
    """KPIs from a synthetic batch cohort (cached). Computed on first call."""
    return get_metrics(n)


@app.post("/api/metrics/refresh")
async def metrics_refresh(n: int = 120) -> dict[str, Any]:
    return refresh_metrics(n)


# ── WebSocket live stream ──────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    q = sim.subscribe()
    # send an immediate snapshot so the dashboard hydrates instantly
    try:
        await ws.send_text(json.dumps({"type": "tick", "patients": sim.snapshot(),
                                       "speed": sim.speed, "paused": sim.paused}))
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25.0)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
                continue
            await ws.send_text(json.dumps(msg, default=str))
    except WebSocketDisconnect:
        log.info("dashboard disconnected")
    except Exception as e:
        log.warning("ws error: %s", e)
    finally:
        sim.unsubscribe(q)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("sentinel.main:app", host=settings.demo_host, port=settings.demo_port, reload=False)