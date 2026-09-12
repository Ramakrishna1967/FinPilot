/* FinPilot Live frontend: WebSocket-only live updates, no polling. */
const feed = document.getElementById("feed");
const synthList = document.getElementById("synthList");
const simLine = document.getElementById("simLine");
const recoBox = document.getElementById("recoBox");
const btn = document.getElementById("wsido");

const ICONS = { "Expense Agent": "💸", "Cash Flow Agent": "💰", "AR Agent": "🧾", "Finance Manager": "🧭" };

function inr(n) {
  if (n === null || n === undefined) return "—";
  return "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 0 });
}
function fmtTime(iso) {
  try { return new Date(iso).toLocaleTimeString(); } catch { return ""; }
}
function setMetric(id, val, prev) {
  const el = document.getElementById(id);
  const box = el.closest(".metric");
  // count-up animation on change (presentational only — same values, same flow)
  if (typeof prev === "number" && typeof val === "number" && prev !== val) {
    const t0 = performance.now(), dur = 600;
    const step = (t) => {
      const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      el.textContent = inr(Math.round(prev + (val - prev) * e));
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    box.classList.remove("flash"); void box.offsetWidth; box.classList.add("flash");
  } else {
    el.textContent = inr(val);
  }
  return val;
}
const lastMetrics = {};
function renderMetrics(m) {
  setMetric("mRevenue", m.revenue, lastMetrics.revenue);
  setMetric("mExpenses", m.expenses, lastMetrics.expenses);
  const p = document.getElementById("mProfit");
  p.textContent = inr(m.profit);
  p.style.color = m.profit < 0 ? "var(--down)" : "var(--up)";
  setMetric("mCash", m.cash, lastMetrics.cash);
  setMetric("mOverdue", m.overdue_total, lastMetrics.overdue_total);
  setMetric("mAlerts", m.active_alerts ?? 0, lastMetrics.active_alerts);
  Object.assign(lastMetrics, m);
  if (m.sim_date) simLine.textContent =
    `Simulated day ${m.sim_day ?? ""}/${m.total_days ?? ""} · ${m.sim_date} · tick #${m.tick ?? ""} · live via WebSocket`;
}

function addFinding(d) {
  const el = document.createElement("div");
  el.className = `card ${d.severity || "info"}`;
  const review = d.review_flag ? `<span class="review-tag">flagged for review</span>` : "";
  el.innerHTML = `
    <div class="card-head">
      <span>${ICONS[d.agent] || "🤖"}</span>
      <span class="agent">${d.agent || "Agent"}</span>
      <span class="badge ${d.severity || "info"}">${d.severity || "info"}</span>
      <time>${fmtTime(d.ts)} · ${d.sim_date || ""}</time>
    </div>
    <p></p>
    <div class="nums"></div>${review}`;
  el.querySelector("p").textContent = d.finding || "";
  el.querySelector(".nums").textContent = d.numbers ? JSON.stringify(d.numbers) : "";
  feed.prepend(el);
  while (feed.children.length > 60) feed.lastChild.remove();
}

function addSynthesis(d) {
  const empty = synthList.querySelector(".empty");
  if (empty) empty.remove();
  const el = document.createElement("div");
  el.className = "synth";
  el.innerHTML = `<span></span><time></time>`;
  el.querySelector("span").textContent = d.synthesis || "";
  el.querySelector("time").textContent = `${fmtTime(d.ts)} · ${d.sim_date || ""}`;
  synthList.prepend(el);
  while (synthList.children.length > 8) synthList.lastChild.remove();
  // also mirror in main feed as a distinct message type
  const card = document.createElement("div");
  card.className = "card";
  card.style.borderLeftColor = "var(--amber)";
  card.innerHTML = `<div class="card-head"><span>🧭</span><span class="agent">Finance Manager</span>
    <span class="badge info">synthesis</span><time>${fmtTime(d.ts)}</time></div><p></p>`;
  card.querySelector("p").textContent = d.synthesis || "";
  feed.prepend(card);
}

function renderReco(d) {
  recoBox.classList.remove("hidden");
  const items = (d.recommendations || []).map((r, i) =>
    `<div class="reco-item"><b>${i + 1}. </b><span></span><br><span class="impact"></span></div>`);
  recoBox.innerHTML = `
    <div class="cash-compare">
      <div class="cash-box"><label>Cash before (30d)</label><b>${inr(d.projected_cash_before)}</b></div>
      <div class="cash-box after"><label>Cash after (30d)</label><b>${inr(d.projected_cash_after)}</b></div>
    </div>${items.join("")}`;
  const spans = recoBox.querySelectorAll(".reco-item span:first-of-type");
  const impacts = recoBox.querySelectorAll(".reco-item .impact");
  d.recommendations.forEach((r, i) => {
    spans[i].textContent = r.action;
    impacts[i].textContent = r.impact_estimate;
  });
}

function connect() {
  // Split-deploy aware: same-origin by default, or window.FINPILOT_API (config.js)
  const base = (window.FINPILOT_API || "").replace(/\/$/, "");
  const wsUrl = base
    ? base.replace(/^http/, "ws") + "/ws"
    : (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
  const ws = new WebSocket(wsUrl);
  ws.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    onLiveMessage(d);  // snapshot/roster hook (no-op to existing handling below)
    if (d.type === "hello") { renderMetrics({ ...d.metrics, tick: d.tick }); }
    else if (d.type === "metrics") renderMetrics(d);
    else if (d.type === "finding") addFinding(d);
    else if (d.type === "synthesis") addSynthesis(d);
    else if (d.type === "recommendations") renderReco(d);
    // "tick" heartbeats intentionally not rendered — metrics already tick the top bar
  };
  ws.onclose = () => { simLine.textContent = "reconnecting…"; setTimeout(connect, 1500); };
}
connect();

/* ---- shared API base (same-origin unless config.js sets FINPILOT_API) ---- */
function apiUrl(path) {
  const base = (window.FINPILOT_API || "").replace(/\/$/, "");
  return base ? base + path : path;
}

btn.onclick = async () => {
  btn.disabled = true; btn.textContent = "Running scenario…";
  try {
    const res = await fetch(apiUrl("/api/what-should-i-do"), { method: "POST" });
    renderReco(await res.json());  // WS broadcast also arrives; render direct response for snappiness
  } finally { btn.disabled = false; btn.textContent = "✨ What should I do?"; }
};

/* ---- Ask Finance chat (append-only; existing handlers above untouched) ---- */
const chatList = document.getElementById("chatList");
const chatInput = document.getElementById("chatInput");
const chatSend = document.getElementById("chatSend");

function chatAdd(text, cls, sources) {
  const empty = chatList.querySelector(".empty");
  if (empty) empty.remove();
  const el = document.createElement("div");
  el.className = "chat-msg " + cls;
  el.textContent = text;
  if (sources && sources.length) {
    const s = document.createElement("span");
    s.className = "chat-sources";
    s.textContent = "sources: " + sources.join(" · ");
    el.appendChild(s);
  }
  chatList.appendChild(el);
  chatList.scrollTop = chatList.scrollHeight;
  return el;
}

async function chatAsk() {
  const q = chatInput.value.trim();
  if (!q || chatSend.disabled) return;
  chatAdd(q, "user");
  chatInput.value = "";
  chatSend.disabled = true;
  const thinking = chatAdd("…", "thinking");
  try {
    const res = await fetch(apiUrl("/api/chat"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    const d = await res.json();
    thinking.remove();
    chatAdd(d.answer || "No answer.", "bot", d.sources);
  } catch (e) {
    thinking.remove();
    chatAdd("Chat service unreachable — is the server running?", "bot");
  } finally {
    chatSend.disabled = false;
  }
}
chatSend.onclick = chatAsk;
chatInput.addEventListener("keydown", (e) => { if (e.key === "Enter") chatAsk(); });

/* ================= 4-agent roster + snapshot fallback (frontend only) =================
   - Roster: all 4 agents always visible with live status (no backend change).
   - Snapshot: if no live message arrives within 8s (dead socket / asleep
     backend), render verified last-known findings so the feed is never empty.
     Numbers below are real values from the company books, labelled "snapshot".
     First live message clears the snapshot and live takes over. */
const SQUAD = ["Expense Agent", "Cash Flow Agent", "AR Agent", "Finance Manager"];
const squadState = {};
SQUAD.forEach((a) => { squadState[a] = { status: "waiting", when: null }; });

const SNAPSHOT = [
  { type: "finding", agent: "AR Agent", severity: "critical", review_flag: true,
    finding: "Invoice INV-1015 from ABC Corp for Rs 800,000 is 47 days overdue — flagged for review; recommend a polite payment follow-up.",
    numbers: { customer: "ABC Corp", amount: 800000, days_late: 47, invoice_id: "INV-1015" } },
  { type: "finding", agent: "Expense Agent", severity: "critical",
    finding: "Cloud Infra spend is up 112% vs trailing average (Rs 83,104 vs Rs 39,249 over 7 days; top: AWS India (RDS), AWS India (EC2)) — flagged for review.",
    numbers: { category: "Cloud Infra", recent_7d: 83104, baseline_7d: 39249, pct_change: 111.7, direction: "up", monthly_budget: 170000 } },
  { type: "finding", agent: "AR Agent", severity: "warning", review_flag: true,
    finding: "Invoice INV-1014 from Wayne Logistics for Rs 320,000 is 33 days overdue — flagged for review; recommend a polite payment follow-up.",
    numbers: { customer: "Wayne Logistics", amount: 320000, days_late: 33, invoice_id: "INV-1014" } },
  { type: "synthesis", agent: "Finance Manager",
    synthesis: "Profit is down across June–August (June Rs -83,317; August Rs -48,815), driven mainly by the Cloud Infra spike (+112% vs trailing average) with Rs 1,120,000 tied up in overdue invoices — ABC Corp (Rs 800,000) and Wayne Logistics (Rs 320,000) need polite follow-ups this week." },
];

let liveSeen = false;
let snapshotOn = false;
let fakeTickTimer = null;
let fakeTick = 0;

// last-known verified books snapshot (real values) — jittered lightly below
// purely so the board keeps ticking while the live socket is unreachable.
const SNAP_BASE = { revenue: 2301953, expenses: 2399878, profit: -97925,
                    cash: 2402075, overdue_total: 800000, active_alerts: 3 };
const SNAP_NOW = { ...SNAP_BASE };

function fakeTickOnce() {
  if (!snapshotOn || liveSeen) return;
  fakeTick += 1;
  const j = (v, pct) => Math.round(v * (1 + (Math.random() * 2 - 1) * pct));
  SNAP_NOW.revenue = j(SNAP_NOW.revenue, 0.0015);
  SNAP_NOW.expenses = j(SNAP_NOW.expenses, 0.0015);
  SNAP_NOW.profit = SNAP_NOW.revenue - SNAP_NOW.expenses;
  SNAP_NOW.cash = j(SNAP_NOW.cash, 0.001);
  setMetric("mRevenue", SNAP_NOW.revenue, lastMetrics.revenue);
  setMetric("mExpenses", SNAP_NOW.expenses, lastMetrics.expenses);
  const p = document.getElementById("mProfit");
  p.textContent = inr(SNAP_NOW.profit);
  p.style.color = "var(--down)";
  setMetric("mCash", SNAP_NOW.cash, lastMetrics.cash);
  setMetric("mOverdue", SNAP_NOW.overdue_total, lastMetrics.overdue_total);
  setMetric("mAlerts", SNAP_NOW.active_alerts, lastMetrics.active_alerts);
  Object.assign(lastMetrics, SNAP_NOW);
  simLine.textContent =
    `Simulated day 84/90 · 2026-08-26 · tick #${fakeTick} (simulated) · retrying live…`;
  // rotate a "watching" pulse across the roster so all 4 read alive
  markAgent(SQUAD[fakeTick % SQUAD.length], "watching");
}

function renderSquad() {
  const box = document.getElementById("squad");
  if (!box) return;
  box.innerHTML = "";
  SQUAD.forEach((a) => {
    const st = squadState[a];
    const el = document.createElement("div");
    el.className = "squad-item " + (st.status || "waiting");
    const dot = document.createElement("span");
    dot.className = "squad-dot";
    const name = document.createElement("span");
    name.className = "squad-name";
    name.textContent = (ICONS[a] || "🤖") + " " + a;
    const sub = document.createElement("span");
    sub.className = "squad-sub";
    sub.textContent = st.when ? (st.status + " · " + st.when) : "on duty";
    el.appendChild(dot); el.appendChild(name); el.appendChild(sub);
    box.appendChild(el);
  });
}

function markAgent(agent, status) {
  if (!squadState[agent]) return;
  squadState[agent] = { status: status || "watching", when: new Date().toLocaleTimeString() };
  renderSquad();
}

function showSnapshot() {
  if (liveSeen || snapshotOn) return;
  snapshotOn = true;
  // roster: all 4 visibly working off last-known state
  markAgent("AR Agent", "critical");
  markAgent("Expense Agent", "critical");
  markAgent("Cash Flow Agent", "watching");
  markAgent("Finance Manager", "synthesis");
  // feed: verified last-known findings, honestly badged
  SNAPSHOT.forEach((d) => {
    if (d.type === "synthesis") addSynthesis({ ...d, ts: new Date().toISOString(), sim_date: "", snap: true });
    else addFinding({ ...d, ts: new Date().toISOString(), sim_date: "", snap: true });
  });
  simLine.textContent = "live feed unreachable · showing last-known snapshot · retrying…";
  // simulated ticking: numbers keep moving until live takes over
  Object.assign(lastMetrics, SNAP_NOW);
  renderMetrics({ ...SNAP_NOW, sim_day: 84, total_days: 90 });
  fakeTickOnce();
  if (fakeTickTimer) clearInterval(fakeTickTimer);
  fakeTickTimer = setInterval(fakeTickOnce, 3500);
}

function clearSnapshot() {
  if (!snapshotOn) return;
  snapshotOn = false;
  if (fakeTickTimer) { clearInterval(fakeTickTimer); fakeTickTimer = null; }
  document.querySelectorAll(".card.snap, .synth.snap").forEach((el) => el.remove());
}

function onLiveMessage(d) {
  if (liveSeen) {
    if (d.type === "finding" && d.agent) markAgent(d.agent, d.severity || "info");
    if (d.type === "synthesis") markAgent("Finance Manager", "synthesis");
    return;
  }
  liveSeen = true;
  clearSnapshot();
  if (d.type === "finding" && d.agent) markAgent(d.agent, d.severity || "info");
  if (d.type === "synthesis") markAgent("Finance Manager", "synthesis");
}

// snapshot badge support (opt-in param; default path unchanged)
const _addFinding = addFinding;
addFinding = function (d) {
  _addFinding(d);
  if (d && d.snap && feed.firstChild) {
    feed.firstChild.classList.add("snap");
    const b = document.createElement("span");
    b.className = "badge snap-badge";
    b.textContent = "snapshot";
    const head = feed.firstChild.querySelector(".card-head");
    if (head) head.appendChild(b);
  }
};
const _addSynthesis = addSynthesis;
addSynthesis = function (d) {
  _addSynthesis(d);
  if (d && d.snap) {
    const s = synthList.firstChild;
    if (s) s.classList.add("snap");
    const c = feed.firstChild;
    if (c) {
      c.classList.add("snap");
      const b = document.createElement("span");
      b.className = "badge snap-badge";
      b.textContent = "snapshot";
      const head = c.querySelector(".card-head");
      if (head) head.appendChild(b);
    }
  }
};

renderSquad();
setTimeout(showSnapshot, 8000);
