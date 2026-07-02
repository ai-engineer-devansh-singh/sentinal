"use strict";
// SENTINEL dashboard — glanceable multi-patient monitor + live agent-council flow.
// All values are streamed from the backend (device simulator → feature store →
// 4 agents → orchestrator). The only client-side state is a per-patient risk
// history buffer used to draw the trend charts.

const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
const fmt = (n, d = 0) => (n == null || n === "" ? "—" : Number(n).toFixed(d));
const TIER = { GREEN: "green", YELLOW: "yellow", RED: "red" };
const STATUS_WORD = { GREEN: "SAFE", YELLOW: "REVIEW", RED: "ACT NOW" };
const AGENT_LABEL = { sofa_tracker: "SOFA Tracker", differential: "Differential", predictor: "Predictor", narrative: "Narrative Analyst" };
const AGENT_COLOR = { sofa_tracker: "#5b9dff", differential: "#43d8c8", predictor: "#a98bff", narrative: "#ff9d4d" };
const AGENT_ORDER = ["sofa_tracker", "differential", "predictor", "narrative"];

// simulated wall-clock: patch stream starts at 08:00, sim_t is minutes since
function simClock(t) {
  if (t == null) return "—";
  const total = 8 * 60 + Math.round(t);
  const h = Math.floor(total / 60), m = total % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

const state = {
  ws: null,
  patients: {},
  audit: [],
  alertsFired: 0,
  carbon: 0,
  config: { grok_enabled: false, grok_model: "—" },
  drawerId: null,
  chart: null,
  riskChart: null,
  riskHist: {},        // id -> [{t, c, tier}]
  councilId: null,
  firstAlertT: {},
};

// ── websocket ──────────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws.onopen = () => { $("#wsChip").textContent = "WS: live"; $("#wsChip").classList.add("on"); };
  state.ws.onclose = () => { $("#wsChip").textContent = "WS: reconnecting…"; $("#wsChip").classList.remove("on"); setTimeout(connect, 1500); };
  state.ws.onerror = () => state.ws.close();
  state.ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "tick") onTick(msg);
    else if (msg.type === "alert") onAlert(msg);
    else if (msg.type === "hitl") onHitl(msg);
    else if (msg.type === "outcome") onOutcome(msg);
  };
}

// ── tick ───────────────────────────────────────────────────────────────────
function onTick(msg) {
  const list = msg.patients || [];
  list.forEach((p) => {
    state.patients[p.patient_id] = p;
    const c = getConsensus(p);
    const h = state.riskHist[p.patient_id] || (state.riskHist[p.patient_id] = []);
    h.push({ t: p.sim_t, c, tier: p.tier });
    if (h.length > 240) h.shift();
  });
  renderGrid();
  renderHero();
  renderCouncil();
  if (state.drawerId && state.patients[state.drawerId]) renderDrawer(state.patients[state.drawerId]);
}

function onAlert(msg) {
  const p = state.patients[msg.patient_id];
  if (p) p.current_alert = msg.alert;
  state.alertsFired++;
  if (msg.alert && msg.alert.carbon_cost_g) state.carbon += msg.alert.carbon_cost_g;
  if (msg.alert && !(msg.patient_id in state.firstAlertT)) state.firstAlertT[msg.patient_id] = msg.alert.sim_t;
  const tier = msg.alert?.tier || "GREEN";
  auditRow({ event: `ALERT ${tier}`, detail: `${msg.patient_id} · consensus ${msg.alert?.consensus} · ${msg.alert?.engine}`, tier });
  flashCard(msg.patient_id, tier);
}
function onHitl(msg) { auditRow({ event: `HITL ${msg.action}`, detail: `${msg.patient_id} · ${msg.reason || "—"}` }); }
function onOutcome(msg) { auditRow({ event: "OUTCOME", detail: `${msg.patient_id} → ${msg.outcome}` }); }

// ── helpers ────────────────────────────────────────────────────────────────
function getConsensus(p) {
  if (p.current_alert && p.current_alert.consensus != null) return p.current_alert.consensus;
  if (p.consensus_ema != null) return p.consensus_ema;
  const a = p.agent_results || {};
  const vals = Object.values(a).map((x) => x.score).filter((n) => n != null);
  if (!vals.length) return 0;
  return vals.reduce((s, n) => s + n, 0) / vals.length;
}
function tierLabel(t) { return { GREEN: "🟢 GREEN", YELLOW: "🟡 YELLOW", RED: "🔴 RED" }[t] || t; }
function outcomeClass(o) {
  if (!o) return "";
  if (o.startsWith("STABLE") || o.startsWith("DISCHARGED") || o.startsWith("DIGNITY")) return "good";
  if (o.startsWith("DECEASED") || o.startsWith("OVER")) return "bad";
  return "";
}

// ── hero summary ───────────────────────────────────────────────────────────
function renderHero() {
  const ps = Object.values(state.patients);
  const live = ps.filter((p) => p.connected);
  const red = ps.filter((p) => p.tier === "RED");
  const yel = ps.filter((p) => p.tier === "YELLOW");
  $("#mPatients").textContent = live.length;
  $("#mRed").textContent = red.length;
  $("#mYellow").textContent = yel.length;
  const tta = Object.values(state.firstAlertT);
  $("#mAvgTTA").textContent = tta.length ? `${(tta.reduce((a, b) => a + b, 0) / tta.length).toFixed(0)}m` : "—";
  $("#carbonChip").textContent = `CO₂: ${state.carbon.toFixed(2)} g`;
  let summary;
  if (!live.length) summary = "no patches streaming yet — click “Connect all patches”.";
  else if (red.length) summary = `${red.length} patient${red.length > 1 ? "s" : ""} need action NOW, ${yel.length} for review, ${live.length - red.length - yel.length} stable.`;
  else if (yel.length) summary = `All stable. ${yel.length} patient${yel.length > 1 ? "s" : ""} trending up — review soon.`;
  else summary = `All ${live.length} patients stable — silent monitoring.`;
  $("#heroSummary").textContent = summary;
}

// ── council flow diagram ───────────────────────────────────────────────────
// Static SVG scaffold built once; updateCouncil() fills live values.
function buildCouncilSVG() {
  const svg = $("#councilSvg");
  // layout coordinates
  const DEV = [
    { id: "dev_patch", x: 24, y: 56, w: 156, h: 64, title: "Wearable patch", sub: "HR · RR · Temp · SpO₂" },
    { id: "dev_poct", x: 24, y: 158, w: 156, h: 64, title: "POCT i-STAT", sub: "Lactate · WBC · Cr · Bil" },
    { id: "dev_notes", x: 24, y: 260, w: 156, h: 64, title: "EHR notes", sub: "nursing text (NER)" },
  ];
  const AG = [
    { key: "sofa_tracker", y: 30 },
    { key: "differential", y: 134 },
    { key: "predictor", y: 238 },
    { key: "narrative", y: 342 },
  ];
  const AG_X = 470, AG_W = 196, AG_H = 92;
  const FS = { x: 270, y: 170, w: 160, h: 120, cx: 350, cy: 230 };
  const GOV = { x: 196, y: 190, w: 62, h: 80, cx: 227, cy: 230 };
  const ORC = { x: 738, y: 160, w: 152, h: 140, cx: 814, cy: 230 };
  const ALT = { x: 928, y: 170, w: 100, h: 120, cx: 978, cy: 230 };

  const path = (x1, y1, x2, y2, bend = 0) =>
    `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`;

  let html = `
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="#33425e"/></marker>
      <marker id="arrowx" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="#5a4a8a"/></marker>
    </defs>
    <!-- column captions -->
    <text x="102" y="36" class="col-cap">DEVICE SOURCES</text>
    <text x="227" y="150" class="col-cap" text-anchor="middle">GOVERNANCE</text>
    <text x="350" y="150" class="col-cap" text-anchor="middle">FEATURE STORE</text>
    <text x="568" y="18" class="col-cap" text-anchor="middle">SPECIALIST AGENTS</text>
    <text x="814" y="150" class="col-cap" text-anchor="middle">ORCHESTRATOR</text>
    <text x="978" y="158" class="col-cap" text-anchor="middle">ALERT</text>
  `;

  // device nodes
  DEV.forEach((d) => {
    html += `<g class="cnode in" id="${d.id}">
      <rect class="nbox" x="${d.x}" y="${d.y}" width="${d.w}" height="${d.h}" rx="12"/>
      <text class="ntitle" x="${d.x + 12}" y="${d.y + 25}">${d.title}</text>
      <text class="nsub" x="${d.x + 12}" y="${d.y + 43}">${d.sub}</text>
      <text class="nval" id="${d.id}_v" x="${d.x + 12}" y="${d.y + 58}" style="font-size:10px;fill:#8a98ad">—</text>
    </g>`;
  });

  // governance node (trust gate)
  html += `<g class="cnode gov" id="node_gov">
    <rect class="nbox" x="${GOV.x}" y="${GOV.y}" width="${GOV.w}" height="${GOV.h}" rx="10"/>
    <text class="ntitle" x="${GOV.x + 8}" y="${GOV.y + 20}" style="font-size:11px">TRUST GATE</text>
    <text class="nsub" id="gov_grade" x="${GOV.x + 8}" y="${GOV.y + 36}" style="font-size:9.5px">—</text>
    <text class="nval" id="gov_conf" x="${GOV.x + 8}" y="${GOV.y + 58}" style="font-size:14px">—</text>
    <rect class="gauge-bg" x="${GOV.x + 8}" y="${GOV.y + 66}" width="${GOV.w - 16}" height="5" rx="2.5"/>
    <rect class="gauge-fg" id="gov_gauge" x="${GOV.x + 8}" y="${GOV.y + 66}" width="0" height="5" rx="2.5" fill="var(--green)"/>
  </g>`;

  // feature store node
  html += `<g class="cnode" id="node_fs">
    <rect class="nbox" x="${FS.x}" y="${FS.y}" width="${FS.w}" height="${FS.h}" rx="12"/>
    <text class="ntitle" x="${FS.x + 12}" y="${FS.y + 26}">Rolling window</text>
    <text class="nsub" x="${FS.x + 12}" y="${FS.y + 42}">slopes · EWMA · shock idx</text>
    <text class="nval" id="fs_sofa" x="${FS.x + 12}" y="${FS.y + 68}" style="font-size:12px">SOFA —</text>
    <text class="nval" id="fs_shock" x="${FS.x + 12}" y="${FS.y + 86}" style="font-size:12px">shock —</text>
    <text class="nval" id="fs_slope" x="${FS.x + 12}" y="${FS.y + 104}" style="font-size:11px;fill:#8a98ad">rr/hr slope —</text>
  </g>`;

  // agent nodes
  AG.forEach((a) => {
    const y = a.y;
    html += `<g class="cnode agent" id="node_${a.key}" style="--ac:${AGENT_COLOR[a.key]}">
      <rect class="nbox" x="${AG_X}" y="${y}" width="${AG_W}" height="${AG_H}" rx="12"/>
      <text class="ntitle" x="${AG_X + 12}" y="${y + 22}">${AGENT_LABEL[a.key]}</text>
      <text class="nsub" id="${a.key}_model" x="${AG_X + 12}" y="${y + 37}">—</text>
      <rect class="engine-badge" id="${a.key}_badge" x="${AG_X + AG_W - 52}" y="${y + 10}" width="40" height="16" rx="8"/>
      <text id="${a.key}_badgetxt" x="${AG_X + AG_W - 32}" y="${y + 21}" text-anchor="middle" style="font-size:9px;font-weight:700">—</text>
      <text class="nval" id="${a.key}_score" x="${AG_X + 12}" y="${y + 72}" style="font-size:22px">—</text>
      <rect class="gauge-bg" x="${AG_X + 12}" y="${y + 78}" width="${AG_W - 24}" height="8" rx="4"/>
      <rect class="gauge-fg" id="${a.key}_gauge" x="${AG_X + 12}" y="${y + 78}" width="0" height="8" rx="4" fill="${AGENT_COLOR[a.key]}"/>
    </g>`;
  });

  // orchestrator node
  html += `<g class="cnode orch" id="node_orch">
    <rect class="nbox" x="${ORC.x}" y="${ORC.y}" width="${ORC.w}" height="${ORC.h}" rx="12"/>
    <text class="ntitle" x="${ORC.x + 12}" y="${ORC.y + 26}">Orchestrator</text>
    <text class="nsub" x="${ORC.x + 12}" y="${ORC.y + 42}">3-stage deliberation</text>
    <text class="nsub" x="${ORC.x + 12}" y="${ORC.y + 64}">consensus</text>
    <text class="nval" id="orc_consensus" x="${ORC.x + 12}" y="${ORC.y + 92}" style="font-size:28px">—</text>
    <text class="nsub" id="orc_carbon" x="${ORC.x + 12}" y="${ORC.y + 116}">CO₂ — g</text>
  </g>`;

  // alert node
  html += `<g class="cnode alert" id="node_alert">
    <rect class="nbox" x="${ALT.x}" y="${ALT.y}" width="${ALT.w}" height="${ALT.h}" rx="12"/>
    <circle id="alert_ring" class="ring live" cx="${ALT.cx}" cy="${ALT.cy - 6}" r="26" stroke="#3ddc84"/>
    <circle id="alert_circle" class="tier-circle badge-g" cx="${ALT.cx}" cy="${ALT.cy - 6}" r="26"/>
    <text id="alert_word" class="tier-txt" x="${ALT.cx}" y="${ALT.cy - 1}" fill="#3ddc84">SAFE</text>
    <text class="nsub" x="${ALT.cx}" y="${ALT.cy + 28}" text-anchor="middle" id="alert_cons">consensus —</text>
  </g>`;

  // edges: devices -> governance -> feature store
  DEV.forEach((d, i) => {
    const ymid = [200, 230, 260][i];
    html += `<path class="edge flow" d="${path(d.x + d.w, d.y + d.h / 2, GOV.x, ymid, 10)}" marker-end="url(#arrow)"/>`;
  });
  // edge: governance -> feature store
  html += `<path class="edge flow" d="${path(GOV.x + GOV.w, GOV.cy, FS.x, FS.cy, 10)}" marker-end="url(#arrow)"/>`;
  html += `<circle r="3" class="packet gov-pkt"><animateMotion dur="1.6s" repeatCount="indefinite" begin="0.1s" path="${path(GOV.x + GOV.w, GOV.cy, FS.x, FS.cy, 10)}"/></circle>`;
  // edges: feature store -> agents (fan-out) + packets
  AG.forEach((a, i) => {
    const cy = a.y + AG_H / 2;
    const pid = `p_fs_${a.key}`;
    html += `<path id="${pid}" class="edge flow" d="${path(FS.x + FS.w, FS.cy, AG_X, cy, 60)}" marker-end="url(#arrow)"/>`;
    html += `<circle r="3.5" class="packet"><animateMotion dur="1.5s" repeatCount="indefinite" begin="${(i * 0.35).toFixed(2)}s" path="${path(FS.x + FS.w, FS.cy, AG_X, cy, 60)}"/></circle>`;
  });
  // edges: agents -> orchestrator (merge)
  AG.forEach((a) => {
    const cy = a.y + AG_H / 2;
    html += `<path class="edge flow" d="${path(AG_X + AG_W, cy, ORC.x, ORC.cy, 60)}" marker-end="url(#arrow)"/>`;
  });
  // edge: orchestrator -> alert + packet
  html += `<path id="p_orch_alert" class="edge flow" d="${path(ORC.x + ORC.w, ORC.cy, ALT.x, ALT.cy, 20)}" marker-end="url(#arrow)"/>`;
  html += `<circle r="4.5" class="packet" id="alert_pkt"><animateMotion dur="1.4s" repeatCount="indefinite" path="${path(ORC.x + ORC.w, ORC.cy, ALT.x, ALT.cy, 20)}"/></circle>`;
  // cross-examination arrows between agent pairs
  html += `<path class="edge cross" d="M${AG_X + AG_W},${134 + AG_H / 2} C${AG_X + AG_W + 46},${134 + AG_H / 2} ${AG_X + AG_W + 46},${238 + AG_H / 2} ${AG_X + AG_W},${238 + AG_H / 2}" marker-end="url(#arrowx)" marker-start="url(#arrowx)"/>`;
  html += `<path class="edge cross" d="M${AG_X + AG_W},${30 + AG_H / 2} C${AG_X + AG_W + 70},${30 + AG_H / 2} ${AG_X + AG_W + 70},${342 + AG_H / 2} ${AG_X + AG_W},${342 + AG_H / 2}" marker-end="url(#arrowx)" marker-start="url(#arrowx)"/>`;
  html += `<text x="${AG_X + AG_W + 30}" y="${192}" class="xlabel">cross-exam</text>`;

  svg.innerHTML = html;
}

function setEngineBadge(key, r) {
  const isGrok = r.engine === "grok";
  const badge = document.getElementById(`${key}_badge`);
  const txt = document.getElementById(`${key}_badgetxt`);
  if (!badge) return;
  badge.style.fill = isGrok ? "#0b1f2a" : "#2a2418";
  badge.style.stroke = isGrok ? "#1c3a52" : "#4a3a1f";
  txt.textContent = isGrok ? "GROK" : "LOCAL";
  txt.setAttribute("fill", isGrok ? "#5b9dff" : "#ffcc4d");
}

function renderCouncil() {
  const ids = Object.keys(state.patients);
  if (!ids.length) return;
  populateCouncilSelect();
  // pick spotlight: explicit choice, else most critical, else first
  let id = state.councilId;
  if (!id || !state.patients[id]) {
    const red = ids.find((x) => state.patients[x].tier === "RED");
    const yel = ids.find((x) => state.patients[x].tier === "YELLOW");
    id = red || yel || ids[0];
    state.councilId = id;
  }
  updateCouncil(state.patients[id]);
}

function populateCouncilSelect() {
  const sel = $("#councilSelect");
  const ids = Object.keys(state.patients);
  if (!ids.length) return;
  const current = sel.value;
  const opts = ids.map((id) => {
    const p = state.patients[id];
    return `<option value="${id}" ${id === state.councilId ? "selected" : ""}>${p.name} · ${STATUS_WORD[p.tier] || "—"}</option>`;
  }).join("");
  if (sel.innerHTML !== opts) sel.innerHTML = opts;
  if (current && state.patients[current]) sel.value = current;
  else sel.value = state.councilId;
}

function updateCouncil(p) {
  if (!p) return;
  const v = p.vitals_now || {};
  const labs = p.labs || {};
  const a = p.agent_results || {};
  const alert = p.current_alert;

  // device source live values
  $("#dev_patch_v").textContent = `HR ${v.hr ?? "—"} · RR ${v.rr ?? "—"} · T ${fmt(v.temp, 1)} · SpO₂ ${v.spo2 ?? "—"}`;
  $("#dev_poct_v").textContent = `Lact ${labs.lactate ?? "—"} · WBC ${labs.wbc ?? "—"} · Cr ${labs.creatinine ?? "—"}`;
  $("#dev_notes_v").textContent = p.notes && p.notes.length ? `${p.notes.length} note${p.notes.length > 1 ? "s" : ""} · NER` : "no notes yet";

  // governance (trust gate)
  const t = p.data_trust || {};
  const g = t.data_trust_grade || "—";
  const c = t.data_confidence ?? 0;
  $("#gov_grade").textContent = `grade ${g}`;
  $("#gov_conf").textContent = `${(c * 100).toFixed(0)}%`;
  const govGauge = document.getElementById("gov_gauge");
  if (govGauge) {
    const gw = Math.max(0, Math.min(100, c * 100)) / 100 * 46; // 62 - 16
    govGauge.setAttribute("width", gw);
    const gcol = c >= 0.85 ? "var(--green)" : c >= 0.6 ? "var(--yellow)" : "var(--red)";
    govGauge.setAttribute("fill", gcol);
  }
  const govNode = document.getElementById("node_gov");
  if (govNode) govNode.classList.toggle("active", p.connected && c < 0.85);

  // feature store
  $("#fs_sofa").textContent = `SOFA ${fmt(p.sofa_score)} · Δ ${fmt(p.delta_sofa, 1)}`;
  $("#fs_shock").textContent = `shock idx ${fmt(p.shock_index, 2)}`;
  const f = p.features || {};
  $("#fs_slope").textContent = `rr slope ${fmt(f.rr_slope, 2)} · hr slope ${fmt(f.hr_slope, 2)}`;

  // agents
  AGENT_ORDER.forEach((key) => {
    const r = a[key];
    const node = document.getElementById(`node_${key}`);
    if (!r) return;
    document.getElementById(`${key}_score`).textContent = fmt(r.score, 0);
    document.getElementById(`${key}_model`).textContent = (r.model_used || "").replace(/_/g, " ").slice(0, 26);
    document.getElementById(`${key}_gauge`).setAttribute("width", Math.max(0, Math.min(100, r.score)) / 100 * (196 - 24));
    setEngineBadge(key, r);
    if (node) {
      node.classList.toggle("active", p.connected && (p.tier !== "GREEN" || r.score > 25));
    }
  });

  // orchestrator
  const cons = getConsensus(p);
  $("#orc_consensus").textContent = fmt(cons, 0);
  $("#orc_carbon").textContent = `CO₂ ${fmt(alert?.carbon_cost_g, 2)} g · ${alert?.engine || "local"}`;

  // alert node
  const tier = p.tier || "GREEN";
  const circle = $("#alert_circle");
  const ring = $("#alert_ring");
  const word = $("#alert_word");
  circle.setAttribute("class", `tier-circle badge-${TIER[tier] || "g"}`);
  const color = tier === "RED" ? "#ff5470" : tier === "YELLOW" ? "#ffcc4d" : "#3ddc84";
  ring.setAttribute("stroke", color);
  ring.classList.toggle("live", p.tier !== "GREEN");
  word.textContent = STATUS_WORD[tier] || "SAFE";
  word.setAttribute("fill", color);
  $("#alert_cons").textContent = `consensus ${fmt(cons, 0)}`;
  const pkt = $("#alert_pkt");
  if (pkt) { pkt.classList.remove("pkt-red", "pkt-yellow"); if (tier === "RED") pkt.classList.add("pkt-red"); else if (tier === "YELLOW") pkt.classList.add("pkt-yellow"); }

  // deliberation feeds
  renderCouncilFeeds(p, alert);
}

function renderCouncilFeeds(p, alert) {
  const a = p.agent_results || {};
  // stage 1
  $("#stage1Feed").innerHTML = AGENT_ORDER.map((key) => {
    const r = a[key];
    if (!r) return `<div class="feed-item ${key}"><span class="who">${AGENT_LABEL[key]}</span><div class="resp">awaiting data…</div></div>`;
    return `<div class="feed-item ${key}">
      <span class="who">${AGENT_LABEL[key]} · score ${fmt(r.score, 0)} · conf ${fmt(r.confidence, 2)}</span>
      <div>${r.reasoning}</div></div>`;
  }).join("");

  // stage 2
  const cx = alert?.cross_examination || [];
  $("#stage2Feed").innerHTML = cx.length
    ? cx.map((c) => `<div class="feed-item orch">
        <span class="who">${c.challenger} challenges</span>
        <div>${c.challenge}</div>
        <div class="resp">↳ ${c.response}</div></div>`).join("")
    : `<div class="feed-item orch"><span class="who">awaiting deliberation…</span></div>`;
  const engPill = $("#feedEngine");
  engPill.textContent = alert?.engine === "grok" ? "grok" : "local";
  engPill.classList.toggle("grok", alert?.engine === "grok");

  // stage 3
  $("#stage3Feed").innerHTML = `
    <div class="feed-item orch"><span class="who">Verdict — ${STATUS_WORD[p.tier] || "SAFE"}</span>
      <div>${alert?.consensus_explanation || "—"}</div></div>
    <div class="feed-item"><span class="who" style="color:#5b9dff">Recommended action</span>
      <div>${alert?.recommended_action || "—"}</div></div>
    <div class="feed-item" style="border-color:#5a4a1f"><span class="who" style="color:#ffcc4d">Uncertainty</span>
      <div class="resp">${alert?.uncertainty_flag || "—"}</div></div>`;
}

// ── glanceable patient grid ────────────────────────────────────────────────
function renderGrid() {
  const grid = $("#patientGrid");
  const ids = Object.keys(state.patients);
  // order: RED, YELLOW, GREEN, then by name — critical first
  const order = ids.sort((x, y) => {
    const px = state.patients[x], py = state.patients[y];
    const rank = { RED: 0, YELLOW: 1, GREEN: 2 };
    return (rank[px.tier] ?? 3) - (rank[py.tier] ?? 3) || px.name.localeCompare(py.name);
  });
  order.forEach((id) => {
    const p = state.patients[id];
    let card = $(`#card-${id}`);
    if (!card) {
      card = el("div", `card tier-${p.tier}`);
      card.id = `card-${id}`;
      card.onclick = () => openDrawer(id);
      grid.appendChild(card);
    }
    // keep DOM order matched to criticality
    if (grid.children[order.indexOf(id)] !== card) grid.appendChild(card);
    card.className = `card tier-${p.tier}`;
    const v = p.vitals_now || {};
    const con = p.connected;
    const cons = getConsensus(p);
    const alert = p.current_alert;
    const why = alert?.consensus_explanation || oneLineWhy(p);
    card.innerHTML = `
      <div class="card-rail"></div>
      <div class="card-body">
        <div class="card-head">
          <div><div class="card-name">${p.name}</div><div class="card-meta">${p.age}${p.sex} · ${p.ward}</div></div>
          <div class="device"><span class="dot ${con ? "live" : ""}"></span>${con ? "patch on" : "off"}</div>
        </div>
        ${p.advance_directive && p.advance_directive !== "FULL CODE" ? `<span class="directive-badge" title="Advance directive — dignity preservation overrides the aggressive sepsis bundle">${p.advance_directive}</span>` : ""}
        <span class="status-pill"><span class="sdot"></span>${STATUS_WORD[p.tier] || "SAFE"} · ${tierLabel(p.tier).split(" ")[0]}</span>
        ${trustChip(p)}
        <div class="why"><span class="why-src">why</span>${why}</div>
        <canvas class="risktrend" id="spark-${id}"></canvas>
        <div class="card-foot">
          <div class="mini-vitals">
            <span>HR <b>${v.hr ?? "—"}</b></span><span>SBP <b>${v.sbp ?? "—"}</b></span><span>Lact <b>${p.labs?.lactate ?? "—"}</b></span>
          </div>
          <span class="muted mono">${simClock(p.sim_t)}</span>
        </div>
        <div class="outcome ${outcomeClass(p.outcome)}">${p.outcome || ""}</div>
      </div>
      ${con ? "" : `<button class="btn primary connect-btn" data-id="${id}">Connect patch</button>`}
    `;
    if (!con) card.querySelector(".connect-btn").onclick = (e) => { e.stopPropagation(); connectDevice(id); };
    drawRiskSpark(`spark-${id}`, state.riskHist[id] || [], p.tier);
  });
}

function oneLineWhy(p) {
  const a = p.agent_results || {};
  const diff = a.differential;
  if (diff && diff.extra?.is_sepsis_likely === false) return `Mimic ruled out — ${p.mimic_tag || "non-sepsis"} fits the stable lactate.`;
  if (p.tier === "RED") return `Council agrees: rising SOFA + lactate ${p.labs?.lactate ?? "—"}, shock index ${fmt(p.shock_index, 2)}.`;
  if (p.tier === "YELLOW") return `Trend rising — RR/HR slopes up, SOFA climbing. Review soon.`;
  return `All agents concur — stable. Silent monitoring.`;
}

function drawRiskSpark(canvasId, hist, tier) {
  const c = document.getElementById(canvasId);
  if (!c || hist.length < 2) return;
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  c.width = w * dpr; c.height = h * dpr;
  const ctx = c.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  // threshold bands (35 / 65)
  const yFor = (val) => h - (val / 100) * (h - 4) - 2;
  ctx.fillStyle = "rgba(61,220,132,.07)"; ctx.fillRect(0, yFor(35), w, h - yFor(35));
  ctx.fillStyle = "rgba(255,204,77,.08)"; ctx.fillRect(0, yFor(65), w, yFor(35) - yFor(65));
  ctx.fillStyle = "rgba(255,84,112,.09)"; ctx.fillRect(0, 0, w, yFor(65));
  // line
  const xs = hist.map((_, i) => (i / (hist.length - 1)) * w);
  ctx.strokeStyle = tier === "RED" ? "#ff5470" : tier === "YELLOW" ? "#ffcc4d" : "#3ddc84";
  ctx.lineWidth = 1.8; ctx.beginPath();
  hist.forEach((pt, i) => { const y = yFor(pt.c); i ? ctx.lineTo(xs[i], y) : ctx.moveTo(xs[i], y); });
  ctx.stroke();
  // last point dot
  const last = hist[hist.length - 1];
  ctx.fillStyle = ctx.strokeStyle; ctx.beginPath(); ctx.arc(xs[xs.length - 1], yFor(last.c), 2.6, 0, 7); ctx.fill();
}

function flashCard(id, tier) {
  const c = $(`#card-${id}`);
  if (!c) return;
  c.style.transition = "box-shadow .2s";
  c.style.boxShadow = tier === "RED" ? "0 0 26px rgba(255,84,112,.6)" : tier === "YELLOW" ? "0 0 24px rgba(255,204,77,.5)" : "0 0 18px rgba(61,220,132,.4)";
  setTimeout(() => (c.style.boxShadow = ""), 900);
}

// ── drawer ─────────────────────────────────────────────────────────────────
function openDrawer(id) {
  state.drawerId = id;
  $("#scrim").hidden = false;
  const d = $("#drawer"); d.hidden = false;
  requestAnimationFrame(() => d.classList.add("open"));
  renderDrawer(state.patients[id]);
}
function closeDrawer() {
  state.drawerId = null;
  $("#drawer").classList.remove("open");
  setTimeout(() => { $("#drawer").hidden = true; $("#scrim").hidden = true; }, 250);
  if (state.chart) { state.chart.destroy(); state.chart = null; }
  if (state.riskChart) { state.riskChart.destroy(); state.riskChart = null; }
}
$("#dClose").onclick = closeDrawer;
$("#scrim").onclick = closeDrawer;

function renderDrawer(p) {
  if (!p) return;
  $("#dName").textContent = `${p.name} — ${STATUS_WORD[p.tier]} · ${tierLabel(p.tier)}`;
  $("#dMeta").textContent = `${p.age}${p.sex} · ${p.ward} · ${(p.comorbidities || []).join(", ") || "no comorbidities"} · ${p.advance_directive} · clock ${simClock(p.sim_t)} (t=${fmt(p.sim_t)}m)`;
  const v = p.vitals_now || {};
  const a = p.agent_results || {};
  const alert = p.current_alert;
  const body = $("#drawerBody");
  const labs = p.labs || {};
  const sc = p.features?.sofa_components || {};
  const compNames = { respiratory: "Respiratory", coagulation: "Coagulation", liver: "Liver", cardiovascular: "Cardiovascular", cns: "CNS", renal: "Renal" };

  const cons = getConsensus(p);
  body.innerHTML = `
    <div class="section">
      <h3>Live vitals stream</h3>
      <div class="vitals-grid">
        ${vitalTile("HR", v.hr, "bpm", v.hr > 120 || v.hr < 50)}
        ${vitalTile("RR", v.rr, "/min", v.rr > 24 || v.rr < 12)}
        ${vitalTile("SBP", v.sbp, "mmHg", v.sbp < 90)}
        ${vitalTile("Temp", v.temp, "°C", v.temp > 38.3 || v.temp < 36)}
        ${vitalTile("SpO₂", v.spo2, "%", v.spo2 < 92)}
      </div>
      <div class="chart-wrap"><canvas id="mainChart"></canvas></div>
    </div>
    <div class="section">
      <h3>Risk over time — agent consensus vs alert thresholds</h3>
      <div class="chart-wrap"><canvas id="riskChart"></canvas></div>
      <p class="muted hint" style="margin-top:6px">Shaded bands = YELLOW (≥35) and RED (≥65) thresholds. Marker shows the first alert.</p>
    </div>
    <div class="section">
      <h3>Point-of-care labs</h3>
      <div class="labs">
        ${lab("Lactate", labs.lactate, "mmol/L", labs.lactate > 2)}
        ${lab("WBC", labs.wbc, "×10⁹", labs.wbc > 12)}
        ${lab("Creatinine", labs.creatinine, "mg/dL", labs.creatinine > 1.2)}
        ${lab("Bilirubin", labs.bilirubin, "µmol/L", labs.bilirubin > 33)}
        ${lab("Platelets", labs.platelets, "×10³", labs.platelets < 150)}
        ${lab("GCS", labs.gcs, "/15", labs.gcs < 15)}
      </div>
    </div>
    <div class="section">
      <h3>SOFA ${fmt(p.sofa_score)} · Δ ${fmt(p.delta_sofa, 1)} · qSOFA ${p.qsofa_met ? "MET" : "no"} · shock idx ${fmt(p.shock_index, 2)}</h3>
      <div class="sofa-bars">${Object.keys(compNames).map((k) => {
        const n = sc[k] ?? 0;
        const cls = n >= 3 ? "l3" : n === 2 ? "l2" : n === 1 ? "l1" : "";
        return `<div class="sofa-bar"><span class="sl">${compNames[k]}</span>
          <span class="strack"><span class="sfill ${cls}" style="width:${(n / 4) * 100}%"></span></span>
          <span class="sv">${n}</span></div>`;
      }).join("")}</div>
    </div>
    ${govPanel(p)}
    <div class="section">
      <h3>Agent council — independent assessment</h3>
      ${agentCard("sofa_tracker", "SOFA Tracker", a.sofa_tracker)}
      ${agentCard("differential", "Differential", a.differential)}
      ${agentCard("predictor", "Predictor", a.predictor)}
      ${agentCard("narrative", "Narrative Analyst", a.narrative)}
    </div>
    <div class="section">
      <h3>Agent vote alignment</h3>
      <div class="align-header">
        <div class="align-header-left">
          <div class="muted" style="font-size:11px">COUNCIL CONSENSUS</div>
          <div class="consensus-big" style="font-size:24px">${fmt(cons, 0)}<span style="font-size:14px">/100</span></div>
        </div>
        <div class="align-header-right">
          <span class="status-pill" style="background:var(--${TIER[p.tier]}-d);color:var(--${TIER[p.tier]})">${STATUS_WORD[p.tier]}</span>
          <div class="align-legend">
            <span class="legend-dot" style="background:${AGENT_COLOR.sofa_tracker}"></span><span class="legend-dot" style="background:${AGENT_COLOR.differential}"></span><span class="legend-dot" style="background:${AGENT_COLOR.predictor}"></span><span class="legend-dot" style="background:${AGENT_COLOR.narrative}"></span>
            <span style="font-size:10px;color:var(--muted);margin-left:4px">agents · white line = consensus</span>
          </div>
        </div>
      </div>
      <div class="align-bars">
        ${AGENT_ORDER.map((key) => {
          const r = a[key]; if (!r) return "";
          const score = Math.max(0, Math.min(100, r.score));
          const spread = score - cons;
          const spreadAbs = Math.abs(spread);
          const spreadColor = spreadAbs <= 10 ? "var(--green)" : spreadAbs <= 25 ? "var(--yellow)" : "var(--red)";
          const spreadSign = spread > 0 ? "+" : spread < 0 ? "" : "±";
          return `<div class="align-bar">
            <span style="color:${AGENT_COLOR[key]};font-weight:600">${AGENT_LABEL[key]}</span>
            <span class="atrack">
              <span class="afill" style="width:${score}%;background:${AGENT_COLOR[key]};box-shadow:0 0 10px ${AGENT_COLOR[key]}33, inset 0 1px 0 rgba(255,255,255,.15)"></span>
              <span class="consensus-marker" style="left:${Math.max(0,Math.min(100,cons))}%"></span>
            </span>
            <div class="align-score">
              <span class="av" style="color:${spreadColor}">${fmt(r.score, 0)}</span>
              <span class="aspread" style="color:${spreadColor}">${spreadSign}${spreadAbs}</span>
            </div>
          </div>`;
        }).join("")}
      </div>
    </div>
    <div class="section">
      <h3>Orchestrator — deliberation &amp; synthesis</h3>
      <div class="orch">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div><div class="muted" style="font-size:11px">CONSENSUS</div><div class="consensus-big">${alert ? fmt(alert.consensus, 0) : fmt(getConsensus(p), 0)}<span style="font-size:16px">/100</span></div></div>
          <span class="status-pill" style="background:var(--${TIER[p.tier]}-d);color:var(--${TIER[p.tier]})">${STATUS_WORD[p.tier]}</span>
        </div>
        ${alert?.consensus_explanation ? `<p style="font-size:13px;margin:10px 0 0;opacity:.92">${alert.consensus_explanation}</p>` : ""}
        ${alert?.cross_examination?.length ? `<div style="margin-top:10px">${alert.cross_examination.map((c) => `<div class="cross"><span class="who">${c.challenger}:</span> ${c.challenge}<br><span class="who">↳</span> ${c.response}</div>`).join("")}</div>` : ""}
        <div class="action"><strong>Recommended action:</strong> ${alert?.recommended_action || "—"}</div>
        ${alert?.differential_note ? `<div class="uncertainty"><strong>Differential:</strong> ${alert.differential_note}</div>` : ""}
        <div class="uncertainty"><strong>Uncertainty:</strong> ${alert?.uncertainty_flag || "—"}</div>
        <div class="muted" style="font-size:11px;margin-top:8px">
          engine: <span class="mono">${alert?.engine || "—"}</span> · carbon: <span class="mono">${fmt(alert?.carbon_cost_g, 2)} g</span> · models: <span class="mono">${alert?.models_used?.join(", ") || "—"}</span> · alert_id: <span class="mono">${alert?.alert_id || "—"}</span>
        </div>
      </div>
      <div class="hitl">
        <button class="btn primary" data-act="ACCEPT">Accept</button>
        <button class="btn" data-act="MODIFY">Modify</button>
        <button class="btn ghost" data-act="REJECT">Reject</button>
      </div>
      <select class="reason" id="hitlReason">
        <option value="">Override reason (if rejecting/modifying)…</option>
        <option>Already on antibiotics</option><option>Documented non-sepsis diagnosis</option>
        <option>End-of-life / comfort care</option><option>Artifact / unreliable reading</option>
        <option>Clinical judgment — other</option>
      </select>
      <div class="hitl-log" id="hitlLog">${p.clinician_action ? `Recorded: ${p.clinician_action}` : ""}</div>
    </div>
    ${p.ground_truth ? counterfactual(p) : ""}
  `;

  body.querySelectorAll(".hitl .btn").forEach((b) => b.onclick = () => respond(p.patient_id, b.dataset.act));
  drawMainChart(p);
  drawRiskTrend(p);
}

function lab(k, val, unit, abn) {
  return `<div class="lab ${abn ? "abn" : ""}"><div class="k">${k}</div><div class="v">${val == null ? "—" : val}<small style="color:var(--muted);font-weight:500"> ${unit}</small></div></div>`;
}
function vitalTile(k, val, unit, abn) {
  return `<div class="vital-tile ${abn ? "abn" : ""}"><div class="vk">${k}</div><div class="vv">${val == null ? "—" : val}</div><div class="vu">${unit}</div></div>`;
}

// ── data governance / input trust ─────────────────────────────────────────
const TRUST_COLOR = { HIGH: "var(--green)", MEDIUM: "var(--yellow)", LOW: "var(--red)", "NO DATA": "var(--muted2)" };
function trustChip(p) {
  const t = p.data_trust || {};
  const g = t.data_trust_grade || "—";
  const c = t.data_confidence;
  return `<span class="trust-chip trust-${g.replace(' ', '_')}" title="Data-trust confidence over the rolling 15-min window">
    <span class="tdot" style="background:${TRUST_COLOR[g] || "var(--muted2)"}"></span>DATA · ${g}${c != null ? ` · ${(c * 100).toFixed(0)}%` : ""}
  </span>`;
}
function govPanel(p) {
  const t = p.data_trust || {};
  const g = t.data_trust_grade || "—";
  const c = t.data_confidence ?? 0;
  const flags = t.data_quality_flags || [];
  const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(0)}%`);
  const bar = (v) => `<span class="gtrack"><span class="gfill" style="width:${Math.max(0, Math.min(100, (v ?? 0) * 100))}%;background:${TRUST_COLOR[g] || "var(--muted2)"}"></span></span>`;
  return `
    <div class="section">
      <h3>Data governance — input trust (rolling 15-min window)</h3>
      <div class="gov">
        <div class="gov-head">
          <div><div class="muted" style="font-size:11px">DATA CONFIDENCE</div>
            <div class="consensus-big" style="color:${TRUST_COLOR[g] || "var(--muted2)"}">${(c * 100).toFixed(0)}<span style="font-size:16px">/100</span></div></div>
          <span class="trust-chip trust-${g.replace(' ', '_')}"><span class="tdot" style="background:${TRUST_COLOR[g] || "var(--muted2)"}"></span>${g}</span>
        </div>
        <div class="gov-grid">
          ${govRow("Window coverage", pct(t.coverage), bar(t.coverage))}
          ${govRow("Feed freshness", t.freshness_min == null ? "—" : `${t.freshness_min} min`, bar(t.freshness_min == null ? 1 : Math.max(0, 1 - t.freshness_min / 2)))}
          ${govRow("Plausibility pass-rate", pct(t.plausibility_pass), bar(t.plausibility_pass))}
          ${govRow("Signal stability", t.stability == null ? "—" : (t.stability >= 1 ? "ok" : "flatline?"), bar(t.stability))}
          ${govRow("Lab freshness", t.lab_age_min == null ? "no labs" : `${t.lab_age_min} min`, bar(t.lab_age_min == null ? 1 : Math.max(0, 1 - t.lab_age_min / 60)))}
          ${govRow("Source trust", pct(t.source_trust), bar(t.source_trust))}
        </div>
        <div class="muted" style="font-size:11px;margin-top:8px">Every reading is validated for physiologic plausibility, spike and flatline artefacts before it enters the window; agent confidence is capped by this score and low trust raises review.</div>
        ${flags.length ? `<div class="gov-flags"><strong>Flags:</strong> ${flags.join(" · ")}</div>` : `<div class="gov-flags ok"><strong>Flags:</strong> none — feed attested clean</div>`}
      </div>
    </div>`;
}
function govRow(label, val, bar) {
  return `<div class="gov-row"><span class="gl">${label}</span><span class="gv">${val}</span>${bar}</div>`;
}
function agentCard(key, label, r) {
  if (!r) return `<div class="agent"><div class="agent-name">${label}</div><div class="muted">awaiting data…</div></div>`;
  const eng = r.engine === "grok" ? "grok" : "local";
  return `<div class="agent" style="border-left:3px solid ${AGENT_COLOR[key]}">
    <div class="agent-head">
      <div class="agent-name">${label}</div>
      <span class="engine-tag ${eng}">${eng === "grok" ? "GROK" : "LOCAL"}</span>
    </div>
    <div class="agent-score">
      <span class="mono" style="font-weight:700;font-size:15px">${fmt(r.score, 0)}</span>
      <div class="gauge"><div class="gauge-fill" style="width:${Math.max(0,Math.min(100,r.score))}%"></div></div>
      <span class="agent-conf">conf ${fmt(r.confidence, 2)} · ${r.model_used}${r.llm_used && r.llm_used !== "none" ? " + " + r.llm_used : ""}</span>
    </div>
    <div class="agent-reason">${r.reasoning}</div>
  </div>`;
}
function counterfactual(p) {
  const gt = p.ground_truth;
  if (p.patient_id === "anon-12345") {
    const bad = [
      ["08:00", "Karen arrives with a sprained ankle. Vitals 'fine'. EWS normal."],
      ["08:30", "POCT lactate 2.4 — mild. Nurse notes nothing acute."],
      ["08:45", "RR 26, temp 38.9, SBP 94. Still no sepsis screen ordered."],
      ["09:00", "BP crashes. Shock index 1.5. Sepsis finally suspected."],
      ["09:15", "Karen dies. Early Warning Score was normal at every checkpoint."],
    ];
    const good = [
      ["08:00", "Karen arrives. Wearable patch streaming; agents initialise."],
      ["08:33", "🟡 REVIEW — Predictor detects accelerating trajectory."],
      ["08:45", "Blood cultures drawn. IV fluids started. Lactate 3.8."],
      ["09:10", "Antibiotics administered (TTA 37 min, within golden hour)."],
      ["09:30", "Karen stabilises. Lives — 24 min before the original collapse."],
    ];
    const col = (rows, cls, title, summary) =>
      `<div class="cf-col ${cls}"><h4>${title}</h4>${rows.map((r) =>
        `<div class="cf-row"><span class="cf-t mono">${r[0]}</span><span class="cf-e">${r[1]}</span></div>`).join("")}
       <div class="cf-sum">${summary}</div></div>`;
    return `<div class="section">
      <h3>Counterfactual — what SENTINEL changed</h3>
      <div class="cf">${col(bad, "cf-bad", "Without SENTINEL", gt.without_sentinel)}${col(good, "cf-good", "With SENTINEL", gt.with_sentinel)}</div>
      <p class="muted" style="font-size:11px;margin-top:8px">Category: ${gt.category}.</p>
    </div>`;
  }
  return `<div class="section">
    <h3>Counterfactual — what SENTINEL changed</h3>
    <div class="cf">
      <div class="cf-col cf-bad"><h4>Without SENTINEL</h4><div class="cf-sum">${gt.without_sentinel}</div></div>
      <div class="cf-col cf-good"><h4>With SENTINEL</h4><div class="cf-sum">${gt.with_sentinel}</div></div>
    </div>
    <p class="muted" style="font-size:11px;margin-top:6px">Category: ${gt.category} · expected peak tier: ${gt.expected_peak_tier}</p>
  </div>`;
}

// ── charts ─────────────────────────────────────────────────────────────────
function drawMainChart(p) {
  const ctx = document.getElementById("mainChart");
  if (!ctx) return;
  const hist = p.vitals_history || [];
  const labels = hist.map((h) => simClock(h.t));
  const datasets = [
    ["HR", "hr", "#5b9dff", "y"],
    ["RR", "rr", "#a98bff", "y1"],
    ["SBP", "sbp", "#ff5470", "y"],
    ["Temp", "temp", "#ffcc4d", "y2"],
    ["SpO₂", "spo2", "#3ddc84", "y2"],
  ].map(([label, key, col, axis]) => ({
    label, data: hist.map((h) => h[key]), borderColor: col, backgroundColor: col + "22",
    tension: .3, borderWidth: 1.6, pointRadius: 0, fill: false, yAxisID: axis,
  }));
  if (state.chart) state.chart.destroy();
  state.chart = new Chart(ctx, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: "#8a98ad", font: { size: 10 }, boxWidth: 10 } } },
      scales: {
        x: { ticks: { color: "#5e6b7e", font: { size: 9 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { color: "#222b3a" } },
        y: { position: "left", ticks: { color: "#5e6b7e", font: { size: 9 } }, grid: { color: "#222b3a" } },
        y1: { position: "right", ticks: { color: "#5e6b7e", font: { size: 9 } }, grid: { drawOnChartArea: false } },
        y2: { display: false },
      },
    },
  });
}

const riskBandsPlugin = {
  id: 'riskBands',
  beforeDraw(chart) {
    const { ctx, chartArea: { top, bottom, left, right }, scales: { y } } = chart;
    if (!y) return;
    const y65 = y.getPixelForValue(65);
    const y35 = y.getPixelForValue(35);
    ctx.save();
    ctx.fillStyle = 'rgba(255, 204, 77, .08)';
    ctx.fillRect(left, y65, right - left, y35 - y65);
    ctx.fillStyle = 'rgba(255, 84, 112, .09)';
    ctx.fillRect(left, top, right - left, y65 - top);
    ctx.restore();
  }
};
const alertMarkerPlugin = {
  id: 'alertMarker',
  afterDatasetsDraw(chart) {
    const { ctx, chartArea: { top, bottom }, scales: { x, y } } = chart;
    const meta = chart.config._alertMeta;
    if (!meta || meta.idx < 0 || !x || !y) return;
    const xPos = x.getPixelForValue(meta.idx);
    const yPos = y.getPixelForValue(meta.val);
    ctx.save();
    // vertical dashed line
    ctx.strokeStyle = '#ff5470';
    ctx.lineWidth = 2;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(xPos, top);
    ctx.lineTo(xPos, bottom);
    ctx.stroke();
    // triangle marker at top
    ctx.fillStyle = '#ff5470';
    ctx.beginPath();
    ctx.moveTo(xPos, top + 6);
    ctx.lineTo(xPos - 5, top + 16);
    ctx.lineTo(xPos + 5, top + 16);
    ctx.fill();
    // point on line
    ctx.setLineDash([]);
    ctx.fillStyle = '#ff5470';
    ctx.beginPath();
    ctx.arc(xPos, yPos, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.restore();
  }
};

function drawRiskTrend(p) {
  const ctx = document.getElementById("riskChart");
  if (!ctx) return;
  const hist = state.riskHist[p.patient_id] || [];
  const labels = hist.map((h) => simClock(h.t));
  const data = hist.map((h) => h.c);
  const firstAlert = state.firstAlertT[p.patient_id];
  const firstAlertIdx = (firstAlert != null) ? hist.findIndex((h) => h.t >= firstAlert) : -1;
  if (state.riskChart) state.riskChart.destroy();
  state.riskChart = new Chart(ctx, {
    type: "line",
    plugins: [riskBandsPlugin, alertMarkerPlugin],
    data: {
      labels,
      datasets: [
        { label: "consensus", data, borderColor: "#a98bff", backgroundColor: "rgba(169,139,255,.15)",
          tension: .3, borderWidth: 2, pointRadius: 0, fill: true },
        { label: "RED threshold", data: labels.map(() => 65), borderColor: "rgba(255,84,112,.6)", borderDash: [4, 4], pointRadius: 0, borderWidth: 1 },
        { label: "YELLOW threshold", data: labels.map(() => 35), borderColor: "rgba(255,204,77,.6)", borderDash: [4, 4], pointRadius: 0, borderWidth: 1 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: "#8a98ad", font: { size: 10 }, boxWidth: 10 } },
      },
      scales: {
        x: { ticks: { color: "#5e6b7e", font: { size: 9 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { color: "#222b3a" } },
        y: { min: 0, max: 100, ticks: { color: "#5e6b7e", font: { size: 9 } }, grid: { color: "#222b3a" } },
      },
    },
  });
  state.riskChart.config._alertMeta = (firstAlertIdx >= 0) ? { idx: firstAlertIdx, val: data[firstAlertIdx] } : null;
  state.riskChart.update();
}

// ── REST calls ─────────────────────────────────────────────────────────────
async function post(url, body) {
  try { return await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) }).then((r) => r.json()); }
  catch (e) { return { error: String(e) }; }
}
function connectDevice(id) { post(`/api/device/${id}/connect`); }
async function respond(id, action) {
  const reason = $("#hitlReason")?.value || "";
  const r = await post(`/api/alert/${id}/respond`, { action, reason });
  if (r.ok) { const log = $("#hitlLog"); if (log) log.textContent = `Recorded: ${action}`; }
}

// controls
$("#connectAll").onclick = () => post("/api/device/connect_all");
$("#pauseBtn").onclick = async () => {
  const paused = $("#pauseBtn").textContent === "Pause";
  await post("/api/pause", { paused });
  $("#pauseBtn").textContent = paused ? "Resume" : "Pause";
};
document.querySelectorAll("#speedSeg button").forEach((b) => b.onclick = async () => {
  document.querySelectorAll("#speedSeg button").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  await post("/api/speed", { speed: parseFloat(b.dataset.speed) });
});
$("#councilSelect").onchange = (e) => { state.councilId = e.target.value; updateCouncil(state.patients[e.target.value]); };

// ── audit + model card ─────────────────────────────────────────────────────
function auditRow({ event, detail, tier }) {
  state.audit.unshift({ event, detail, tier, ts: new Date().toLocaleTimeString() });
  state.audit = state.audit.slice(0, 80);
  renderAudit();
}
function renderAudit() {
  const list = $("#auditList");
  list.innerHTML = state.audit.map((a) =>
    `<div class="audit-row ${a.tier || ""}"><span class="e">[${a.ts}] ${a.event}</span> — ${a.detail}</div>`).join("");
}

async function loadConfig() {
  const cfg = await fetch("/api/config").then((r) => r.json());
  state.config = cfg;
  const chip = $("#aiChip");
  if (cfg.grok_enabled) { chip.textContent = `AI: Grok · ${cfg.grok_model}`; chip.classList.add("on"); }
  else { chip.textContent = "AI: local fallback (add XAI_API_KEY)"; chip.classList.add("warn"); chip.classList.remove("on"); }
  const a = await fetch("/api/audit").then((r) => r.json());
  (a.entries || []).reverse().forEach((e) => {
    const tier = e.tier ? e.tier.toLowerCase() : "";
    state.audit.push({ event: e.event.toUpperCase() + (e.tier ? " " + e.tier : ""), detail: `${e.patient_id || e.alert_id || ""} · ${e.action || e.outcome || e.engine || ""}`.trim(), tier, ts: "init" });
  });
  state.audit = state.audit.slice(0, 80);
  renderAudit();
  renderModelCard(cfg);
}

function renderModelCard(cfg) {
  $("#modelCard").innerHTML = `
    <h4>About this Agent — model card</h4>
    <dl>
      <dt>System</dt><dd>SENTINEL multi-agent sepsis council</dd>
      <dt>LLM</dt><dd>${cfg.grok_enabled ? "xAI Grok (" + cfg.grok_model + ")" : "Local deterministic fallback (add XAI_API_KEY)"}</dd>
      <dt>Predictor</dt><dd>XGBoost sepsis-risk classifier (trained on synthetic cohort)</dd>
      <dt>Narrative</dt><dd>TF-IDF + LogisticRegression note-concern classifier</dd>
      <dt>SOFA / Differential</dt><dd>Sepsis-3 rule engines (scoring + mimic rule-out)</dd>
      <dt>Intended use</dt><dd>Clinical decision support, sepsis early warning</dd>
      <dt>Not a</dt><dd>standalone diagnostic device; clinician must review</dd>
      <dt>Governance</dt><dd>Hallucination grounding gate · immutable audit trail · HITL within 15 min</dd>
      <dt>Carbon</dt><dd>Cascading intelligence — LLM only for ambiguous cases</dd>
      <dt>Regulatory</dt><dd>FDA 510(k) SaMD path · EU AI Act Art. 35 AIA</dd>
    </dl>`;
}

// ── batch metrics (KPIs + outcomes + equity + drift) ───────────────────────
const EPIC = { sens: 0.33, tta: 180, override: 0.50 };
const TREWS = { mort: 0.187 };
let driftChart = null, outcomesChart = null;

function flag(ok) { return `<span class="k-flag ${ok ? "pass" : "fail"}">${ok ? "PASS" : "BELOW"}</span>`; }
function pct(x) { return x == null ? "—" : (x <= 1 ? `${(x * 100).toFixed(0)}%` : `${x}`); }

async function fetchMetrics() {
  const hint = $("#metricsHint");
  try {
    const m = await fetch("/api/metrics").then((r) => r.json());
    renderMetrics(m);
    hint.textContent = `Synthetic validation cohort n=${m.n} · ${m.grok_enabled ? "Grok" : "local"} reasoning · re-run for a fresh draw`;
  } catch (e) {
    hint.textContent = "Metrics unavailable: " + e;
  }
}

function renderMetrics(m) {
  const k = m.kpis;
  const tiles = [
    { l: "Sensitivity", v: pct(k.sensitivity), t: "target > 0.85", ok: k.sensitivity >= 0.85, base: `Epic Sepsis Model: ${pct(EPIC.sens)}` },
    { l: "Specificity", v: pct(k.specificity), t: "target > 0.70", ok: k.specificity >= 0.70, base: "no false RED alerts" },
    { l: "Median time-to-alert", v: `${k.median_tta_min ?? "—"} min`, t: "target ≤ 10 min", ok: (k.median_tta_min ?? 99) <= 10, base: "current standard: 2–4 h" },
    { l: "Override rate", v: pct(k.override_rate), t: "target < 0.20", ok: k.override_rate <= 0.20, base: `Epic alert fatigue: ${pct(EPIC.override)}` },
    { l: "Mortality reduction", v: pct(k.mortality_reduction), t: "target ≥ 0.187", ok: k.mortality_reduction >= 0.187, base: `TREWS: ${pct(TREWS.mort)}` },
    { l: "NPC-LS (carbon/life)", v: `${k.npc_ls_kg_per_life} kg`, t: "net-positive", ok: k.npc_ls_kg_per_life > 0, base: "AI carbon: ~0.15 g/alert" },
    { l: "Overall AUROC", v: k.overall_auroc.toFixed(3), t: "cohort discriminability", ok: k.overall_auroc >= 0.85, base: `n=${m.n}` },
  ];
  $("#kpiGrid").innerHTML = tiles.map((x) =>
    `<div class="kpi">${flag(x.ok)}<div class="k-label">${x.l}</div><div class="k-val">${x.v}</div><div class="k-target">${x.t}</div><div class="k-base">${x.base}</div></div>`
  ).join("");

  // outcomes chart
  renderOutcomes(m);
  // equity table
  const eq = m.equity;
  const rows = [];
  const allAucs = [];
  for (const dim of ["by_sex", "by_age"]) {
    rows.push(`<tr><th colspan="4">${dim === "by_sex" ? "By sex" : "By age bucket"}</th></tr>`);
    for (const [g, v] of Object.entries(eq[dim])) {
      const auc = (v.auroc == null || isNaN(v.auroc)) ? "n/a" : v.auroc.toFixed(3);
      if (!isNaN(v.auroc)) allAucs.push(v.auroc);
      rows.push(`<tr><td>${g}</td><td>${v.n}</td><td>${v.pos}</td><td class="auc">${auc}</td></tr>`);
    }
  }
  $("#equityTable").innerHTML = `<table class="eq">${rows.join("")}<tr><th>subgroup</th><th>n</th><th>sepsis</th><th>AUROC</th></tr></table>`;
  const gap = allAucs.length ? Math.max(...allAucs) - Math.min(...allAucs) : 0;
  $("#equityNote").textContent = gap > 0.05 ? `⚠ subgroup AUROC gap ${gap.toFixed(3)} — review calibration` : `max subgroup gap ${gap.toFixed(3)} — equity within bounds (checkpoint 2)`;
  renderDrift(m.drift);
}

function renderOutcomes(m) {
  // derive outcome counts from KPI counts (with vs without)
  const c = m.kpis.counts || {};
  const septic = c.septic || 0, noncase = c.noncase || 0;
  const tp = c.tp || 0, fn = c.fn || 0, fp = c.fp || 0, tn = c.tn || 0;
  const deathsWith = fn; // never alerted → died
  const livesSaved = septic - deathsWith;
  const ctx = $("#outcomesChart");
  if (outcomesChart) outcomesChart.destroy();
  outcomesChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: ["Survived", "Died", "Correctly not flagged", "Over-treated (false alert)"],
      datasets: [
        { label: "With SENTINEL", data: [livesSaved, deathsWith, tn, fp], backgroundColor: "#3ddc84", borderRadius: 4 },
        { label: "Without SENTINEL", data: [0, septic, noncase, 0], backgroundColor: "#ff5470", borderRadius: 4 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { labels: { color: "#8a98ad", font: { size: 10 }, boxWidth: 10 } } },
      scales: {
        x: { ticks: { color: "#8a98ad", font: { size: 9 } }, grid: { color: "#222b3a" } },
        y: { beginAtZero: true, ticks: { color: "#5e6b7e", font: { size: 9 } }, grid: { color: "#222b3a" } },
      },
    },
  });
  $("#outcomesNote").textContent = `${livesSaved} of ${septic} septic patients saved by timely alert vs ${septic} deaths without SENTINEL. ${fp} false alerts → override (training signal).`;
}

function renderDrift(d) {
  const ctx = $("#driftChart");
  const labels = d.windows.map((_, i) => `W${i + 1}`);
  const data = d.windows.map((w) => (w == null ? null : w));
  if (driftChart) driftChart.destroy();
  driftChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "window AUROC", data, borderColor: "#5b9dff", backgroundColor: "#5b9dff22", tension: .3, pointRadius: 5, pointBackgroundColor: data.map((w) => (w != null && w < 0.7 ? "#ff5470" : "#5b9dff")) },
        { label: "mean", data: labels.map(() => d.mean), borderColor: "#3ddc84", borderDash: [4, 4], pointRadius: 0, borderWidth: 1 },
        { label: "LCL (−2σ)", data: labels.map(() => d.lcl), borderColor: "#ffcc4d", borderDash: [2, 2], pointRadius: 0, borderWidth: 1 },
        { label: "threshold 0.70", data: labels.map(() => 0.70), borderColor: "#ff5470", borderDash: [6, 3], pointRadius: 0, borderWidth: 1 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { labels: { color: "#8a98ad", font: { size: 10 }, boxWidth: 10 } } },
      scales: { y: { min: 0.5, max: 1.02, ticks: { color: "#5e6b7e", font: { size: 9 } }, grid: { color: "#222b3a" } },
                x: { ticks: { color: "#5e6b7e", font: { size: 9 } }, grid: { color: "#222b3a" } } },
    },
  });
  $("#driftNote").textContent = d.drift_detected
    ? `⚠ drift detected (window below threshold/LCL) — auto-fallback to qSOFA engaged`
    : `stable — all windows within control limits, no drift detected`;
}

$("#refreshMetrics").onclick = async () => {
  $("#metricsHint").textContent = "Re-running batch…";
  await fetch("/api/metrics/refresh", { method: "POST" }).catch(() => {});
  fetchMetrics();
};

// ── boot ───────────────────────────────────────────────────────────────────
function stamp() { $("#footStamp").textContent = new Date().toLocaleString(); }
buildCouncilSVG();
connect(); loadConfig(); fetchMetrics(); stamp(); setInterval(stamp, 1000);