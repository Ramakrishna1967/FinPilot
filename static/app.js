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
