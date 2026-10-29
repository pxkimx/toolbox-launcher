/* Toolbox Launcher page. The server sends the state of every tool after each scan (server-sent events);
   this file draws the rack, opens tools on keys inside this one page, and sends "open" and "quit" requests back.
   Holding that connection open is also how the app knows its window is still there. */
(() => {
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const TOKEN = $('meta[name="toolbox-token"]').content;
const SAMPLES = 48;                       // processor samples per screen, the same number the server keeps
const S = { state: null, mods: new Map(), open: new Set(), confirm: new Set(), busyKeys: new Set(), pending: new Map(), quit: false, restored: false };
const T = { tabs: [], active: "room", panes: new Map(), keys: new Map() };     // pages open inside this one

/* ---------- small helpers */
const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : (word.endsWith("s") ? "es" : "s")}`;
function dur(s) {
  if (s == null) return "–";
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ${String(m % 60).padStart(2, "0")}`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}
const mem = kb => { const mb = kb / 1024; return mb >= 1000 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`; };
const pct = v => `${v >= 10 ? Math.round(v) : v.toFixed(1)}%`;
const clock = t => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
const tool = key => S.state ? S.state.tools.find(t => t.key === key) : null;

let toastTimer;
function toast(text) {
  const t = $("#toast"); t.textContent = text; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 4200);
}
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Toolbox-Token": TOKEN }, body: JSON.stringify(body || {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || j.text || `request failed (${r.status})`);
  return j;
}

/* ---------- status: a lamp and a word together, never colour alone */
const LABEL = { running: "Running", busy: "Busy", starting: "Warming up", leftover: "Background only", stopped: "Off", stopping: "Shutting down" };
function statusOf(t) {
  if (t.stopping || S.busyKeys.has(t.key)) return t.state === "stopped" ? "stopped" : "stopping";
  if (t.state === "stopped" && S.pending.has(t.key)) return "starting";
  if (t.state === "running") return t.busy ? "busy" : "running";
  return t.state;
}

/* ---------- one module per tool, made once and then updated in place */
const METER = (() => {
  const at = (deg, r) => { const a = deg * Math.PI / 180; return [48 + r * Math.sin(a), 48 - r * Math.cos(a)]; };
  const [x0, y0] = at(-60, 40), [x1, y1] = at(60, 40), [zx0, zy0] = at(36, 37), [zx1, zy1] = at(60, 37);
  const ticks = [-60, -30, 0, 30, 60].map(d => { const [a, b] = at(d, 40), [c, e] = at(d, 34); return `<line class="tick" x1="${a.toFixed(1)}" y1="${b.toFixed(1)}" x2="${c.toFixed(1)}" y2="${e.toFixed(1)}"/>`; }).join("");
  return `<svg viewBox="0 0 96 74" role="img"><path class="zone" d="M${zx0.toFixed(1)} ${zy0.toFixed(1)} A37 37 0 0 1 ${zx1.toFixed(1)} ${zy1.toFixed(1)}"/>
    <path class="arc" d="M${x0.toFixed(1)} ${y0.toFixed(1)} A40 40 0 0 1 ${x1.toFixed(1)} ${y1.toFixed(1)}"/>${ticks}
    <text x="8" y="41">0</text><text x="74" y="41">100</text>
    <line class="needle" x1="48" y1="48" x2="48" y2="12"/><circle class="hub" cx="48" cy="48" r="3.2"/></svg>`;
})();

function makeMod(t) {
  const m = document.createElement("article");
  m.className = "mod"; m.dataset.key = t.key;
  m.innerHTML = `
    <i class="screw a"></i><i class="screw b"></i><i class="screw c"></i><i class="screw d"></i>
    <header>
      ${t.key === "other" ? `<div class="badge blank">…</div>` : `<img class="badge" alt="" src="/icon/${esc(t.key)}.png">`}
      <div class="names"><h2>${esc(t.name)}</h2><p class="what"></p></div>
      <div class="lampbox"><i class="lamp"></i><span class="state"></span></div>
    </header>
    <div class="panel">
      <div class="crt">
        <svg viewBox="0 0 300 70" preserveAspectRatio="none" role="img">
          ${[50, 100, 150, 200, 250].map(x => `<line class="grat" x1="${x}" y1="0" x2="${x}" y2="70"/>`).join("")}
          ${[17.5, 35, 52.5].map(y => `<line class="grat" x1="0" y1="${y}" x2="300" y2="${y}"/>`).join("")}
          <path class="line" d=""/><line class="cross" y1="0" y2="70" visibility="hidden"/>
          <circle class="now" r="3" visibility="hidden"/><circle class="pick" r="3" visibility="hidden"/>
        </svg>
        <span class="crt-cap">Processor · 2 min</span><span class="crt-peak"></span>
      </div>
      <div class="meter" title="Share of one processor core used by this tool, all of its processes">${METER}<span class="meter-val"></span></div>
      <button class="tog" role="switch" aria-checked="false"><i class="lever"></i></button>
    </div>
    <dl class="strip"></dl>
    <p class="tag" hidden></p>
    <footer>
      <button class="btn act"></button>
      <button class="btn pale pop" title="Open in a separate browser tab instead of inside this page">↗ Browser tab</button>
      <button class="link toggle"></button>
    </footer>
    <div class="confirm" hidden><p></p><div class="row"><button class="btn red yes"></button><button class="btn pale no"></button></div></div>
    <div class="svc" hidden></div>`;
  const key = t.key, img = $("img.badge", m);
  if (img) img.onerror = () => { const d = document.createElement("div"); d.className = "badge blank"; d.textContent = t.name[0]; img.replaceWith(d); };
  $(".tog", m).onclick = () => { const x = tool(key); if (!x) return; ["running", "starting"].includes(x.state) ? askQuit(key) : x.state === "leftover" ? askQuit(key) : start(key, x.embed); };
  $(".act", m).onclick = () => act(key);
  $(".pop", m).onclick = () => popOut(key);
  $(".yes", m).onclick = () => quit(key);
  $(".no", m).onclick = () => { S.confirm.delete(key); render(); };
  $(".toggle", m).onclick = () => { S.open.has(key) ? S.open.delete(key) : S.open.add(key); render(); };
  const svg = $(".crt svg", m);
  svg.addEventListener("mousemove", e => hoverTrace(svg, e));
  svg.addEventListener("mouseleave", () => leaveTrace(svg));
  return m;
}

function strip(pairs) {
  return pairs.map(([k, v, title]) => `<div><dt>${k}</dt><dd${title ? ` title="${esc(title)}"` : ""}>${esc(v)}</dd></div>`).join("");
}

function updateMod(m, t) {
  const st = statusOf(t), live = t.state !== "stopped", on = ["running", "busy", "starting"].includes(st);
  m.dataset.state = t.state; m.dataset.s = st;
  $(".state", m).textContent = LABEL[st];
  $(".what", m).textContent = [t.what, t.version ? `v${t.version}` : ""].filter(Boolean).join(" · ") || " ";
  const tog = $(".tog", m);
  tog.setAttribute("aria-checked", on ? "true" : "false"); tog.disabled = st === "stopping" || t.key === "other" && !live || t.installed === false;
  tog.title = on ? `Switch ${t.name} off` : t.state === "leftover" ? "Stop these processes" : `Start ${t.name}`;

  drawTrace($(".crt svg", m), t.history || []);
  const peak = Math.max(0, ...(t.history || []).filter(v => v != null));
  $(".crt-peak", m).textContent = live ? `peak ${pct(peak)}` : "";
  const frac = live ? Math.min(1.05, t.cpu / 100) : 0;
  $(".needle", m).style.transform = `rotate(${-60 + 120 * frac}deg)`;
  $(".meter-val", m).textContent = live ? pct(t.cpu) : "–";

  const inside = T.tabs.includes(t.key);
  const oldest = Math.max(0, ...t.procs.map(p => p.uptime));
  $(".strip", m).innerHTML = t.state === "running" || t.state === "starting"
    ? strip([["Port", t.port], ["Up", dur(t.uptime != null ? t.uptime : oldest)], ["Memory", mem(t.rss)],
             ["Window", t.state === "starting" ? "–" : inside ? "this page" : t.windows > 0 ? "browser" : "none",
              inside ? "Open on a key in this page" : t.windows > 0 ? "A browser window is connected" : "No window is connected"]])
    : t.state === "leftover"
      ? strip([["Processes", t.procs.length], ["Oldest", dur(oldest)], ["Memory", mem(t.rss)], ["Server", "none"]])
      : strip([["Port", t.port || "–"], ["Up", "–"], ["Memory", "–"], ["Opens", t.installed === false ? "not found" : t.embed ? "in this page" : "browser tab", t.embed ? "" : "This tool opens in a browser tab of its own"]]);

  const tag = $(".tag", m);
  let text = "";
  if (t.installed === false && t.key !== "other") text = "Its app is not in the Toolbox folder.";
  else if (t.state === "leftover") text = t.key === "other"
    ? `${plural(t.procs.length, "process")} started from the tools folder that ${t.procs.length === 1 ? "does" : "do"} not belong to a listed tool.`
    : `The tool itself is closed, but ${plural(t.procs.length, "process")} from it ${t.procs.length === 1 ? "is" : "are"} still running.`;
  else if (t.state === "running" && t.windows === 0 && !inside) text = "No window is connected: it is running in the background.";
  else if (t.state === "running" && t.leftovers > 0) text = `${plural(t.leftovers, "process")} left behind by an earlier run.`;
  tag.textContent = text; tag.hidden = !text;

  const act = $(".act", m), pop = $(".pop", m);
  act.textContent = st === "stopping" ? "Shutting down…" : t.state === "leftover" ? "Stop these" : t.state === "stopped" && st !== "starting" ? "Start"
    : !t.embed ? "Show in browser" : inside && T.active === "tool:" + t.key ? "Showing" : "Show";
  act.disabled = st === "stopping" || t.installed === false || (t.key === "other" && !live);
  act.className = "btn act" + (t.state === "leftover" ? " red" : "");
  pop.style.display = t.embed && t.key !== "other" && t.state !== "leftover" ? "" : "none";
  pop.disabled = st === "stopping" || st === "starting" || t.installed === false;
  const link = $(".toggle", m); link.style.display = t.procs.length ? "" : "none";
  link.textContent = `${plural(t.procs.length, "process")} ${S.open.has(t.key) ? "▴" : "▾"}`;

  const conf = $(".confirm", m); conf.hidden = !S.confirm.has(t.key) || st === "stopping" || !live;
  $("p", conf).textContent = t.state === "leftover"
    ? `These processes are using ${pct(t.cpu)} of a processor core. They may still be doing work, and stopping them ends it.`
    : `${t.name} is using ${pct(t.cpu)} of a processor core. An analysis may be running, and switching it off now would lose it.`;
  $(".yes", conf).textContent = t.state === "leftover" ? "Stop anyway" : "Switch off anyway";
  $(".no", conf).textContent = t.state === "leftover" ? "Leave them" : "Keep it running";

  const svc = $(".svc", m); svc.hidden = !S.open.has(t.key) || !t.procs.length;
  if (!svc.hidden) svc.innerHTML = `<table><thead><tr><th>Role</th><th>ID</th><th>CPU</th><th>Memory</th><th>Up</th><th>Command</th></tr></thead><tbody>${
    t.procs.map(p => `<tr><td class="${p.role === "left behind" ? "left" : ""}">${esc(p.role)}${p.ports.length && p.role !== "server" ? ` :${p.ports.join(", :")}` : ""}</td><td>${p.pid}</td><td>${pct(p.cpu)}</td><td>${mem(p.rss)}</td><td>${dur(p.uptime)}</td><td class="cmd" title="${esc(p.from ? "from the " + p.from : "")}">${esc(p.cmd)}</td></tr>`).join("")}</tbody></table>`;
}

/* ---------- the little screen: one series, so no legend; its caption names it */
function tracePoints(hist) {
  const vals = hist.filter(v => v != null);
  const top = Math.max(100, ...vals) * 1.06, step = 300 / (SAMPLES - 1), off = SAMPLES - hist.length;
  return hist.map((v, i) => v == null ? null : { x: (off + i) * step, y: 64 - (v / top) * 54, v, age: hist.length - 1 - i });
}
function drawTrace(svg, hist) {
  const pts = tracePoints(hist); svg._pts = pts;
  let d = "", pen = false;
  for (const p of pts) { if (!p) { pen = false; continue; } d += `${pen ? "L" : "M"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`; pen = true; }
  $(".line", svg).setAttribute("d", d);
  const last = pts[pts.length - 1], now = $(".now", svg);
  if (last) { now.setAttribute("cx", last.x); now.setAttribute("cy", last.y); now.setAttribute("visibility", "visible"); }
  else now.setAttribute("visibility", "hidden");
  if (svg._hover != null) placeHover(svg, svg._hover);
}
function placeHover(svg, clientX) {
  const r = svg.getBoundingClientRect(), x = ((clientX - r.left) / r.width) * 300;
  const pts = (svg._pts || []).filter(Boolean);
  if (!pts.length) return leaveTrace(svg);
  const p = pts.reduce((a, b) => Math.abs(b.x - x) < Math.abs(a.x - x) ? b : a);
  const cross = $(".cross", svg), pick = $(".pick", svg);
  cross.setAttribute("x1", p.x); cross.setAttribute("x2", p.x); cross.setAttribute("visibility", "visible");
  pick.setAttribute("cx", p.x); pick.setAttribute("cy", p.y); pick.setAttribute("visibility", "visible");
  const secs = Math.round(p.age * (S.state ? S.state.interval : 2.5));
  const tip = $("#tip"); tip.innerHTML = `<b>${pct(p.v)}</b> of a core · ${secs === 0 ? "now" : `${secs} s ago`}`; tip.hidden = false;
  const tx = r.left + (p.x / 300) * r.width;
  tip.style.left = `${Math.min(window.innerWidth - tip.offsetWidth - 8, Math.max(8, tx + 12))}px`;
  tip.style.top = `${Math.max(8, r.top - tip.offsetHeight - 6)}px`;
}
function hoverTrace(svg, e) { svg._hover = e.clientX; placeHover(svg, e.clientX); }
function leaveTrace(svg) {
  svg._hover = null; $("#tip").hidden = true;
  $(".cross", svg).setAttribute("visibility", "hidden"); $(".pick", svg).setAttribute("visibility", "hidden");
}

/* ---------- pages inside this page: one key in the rail and one pane per open tool */
let toldTabs = null;
function saveTabs() {
  try { sessionStorage.setItem("toolbox-launcher-tabs", JSON.stringify({ tabs: T.tabs, active: T.active })); } catch (e) { /* storage may be unavailable */ }
  // the app must know which tools live in this window: when the window closes, it switches those off
  const now = T.tabs.join(",");
  if (now !== toldTabs) { toldTabs = now; post("/api/tabs", { keys: T.tabs }).catch(() => { toldTabs = null; }); }
}
function show(view) {
  T.active = view; saveTabs(); render();
  const k = $(`.key[data-view="${view}"]`); if (k && k.scrollIntoView) k.scrollIntoView({ block: "nearest", inline: "nearest" });
}
function openTab(key) { if (!T.tabs.includes(key)) T.tabs.push(key); show("tool:" + key); }
function closeTab(key) {
  T.tabs = T.tabs.filter(k => k !== key);
  const p = T.panes.get(key); if (p) { p.el.remove(); T.panes.delete(key); }
  const k = T.keys.get(key); if (k) { k.remove(); T.keys.delete(key); }
  if (T.active === "tool:" + key) T.active = "room";
  saveTabs();
}
function pane(t) {
  let p = T.panes.get(t.key);
  if (!p) {
    const el = document.createElement("div"); el.className = "pane";
    el.innerHTML = `<div class="warm"><div><div class="tube"></div><h2>Warming up ${esc(t.name)}</h2>
      <p>Its page appears here as soon as it answers. The first start after an update can take a few minutes while it installs its packages.</p><p class="secs"></p></div></div>`;
    $("#panes").appendChild(el);
    p = { el, frame: null, since: Date.now() }; T.panes.set(t.key, p);
  }
  if (!p.frame && t.state === "running") {            // its page answers: put it in
    const f = document.createElement("iframe");
    f.title = t.name; f.allow = "clipboard-read; clipboard-write; fullscreen";
    const chosen = Theme.chosen();
    f.src = chosen ? t.url + (t.url.includes("?") ? "&" : "?") + "appearance=" + chosen : t.url;
    p.el.innerHTML = ""; p.el.appendChild(f); p.frame = f;
  }
  if (!p.frame) $(".secs", p.el).textContent = `${Math.round((Date.now() - p.since) / 1000)} s`;
  return p;
}
function tabKey(t) {
  let b = T.keys.get(t.key);
  if (!b) {
    b = document.createElement("button"); b.className = "key"; b.dataset.view = "tool:" + t.key;
    b.innerHTML = `<i class="pip"></i><img alt="" src="/icon/${esc(t.key)}.png"><span>${esc(t.name)}</span><span class="x" title="Switch ${esc(t.name)} off and close its key">×</span>`;
    $("img", b).onerror = e => e.target.remove();
    b.onclick = e => { if (e.target.classList.contains("x")) { e.stopPropagation(); closeFromKey(t.key); } else show("tool:" + t.key); };
    $("#keys").appendChild(b); T.keys.set(t.key, b);
  }
  $(".pip", b).className = "pip " + (t.state === "running" ? "go" : "warm");
  return b;
}
function closeFromKey(key) {
  const t = tool(key); if (!t) return closeTab(key);
  if (t.busy && t.state === "running") { S.confirm.add(key); return show("room"); }
  quit(key);
}

/* ---------- drawing the whole page */
function render() {
  const st = S.state; if (!st) return;
  const tools = st.tools, listed = tools.filter(t => t.key !== "other");
  const now = Date.now();
  for (const [key, since] of S.pending) { const t = tool(key); if (!t || t.state !== "stopped" || now - since > 30000) S.pending.delete(key); }

  if (!S.restored) {                                   // after a reload: put back the keys that were open
    S.restored = true;
    try {
      const saved = JSON.parse(sessionStorage.getItem("toolbox-launcher-tabs") || "{}");
      T.tabs = (saved.tabs || []).filter(k => { const t = tool(k); return t && t.embed && ["running", "starting"].includes(t.state); });
      if (saved.active && (saved.active === "log" || T.tabs.includes(String(saved.active).slice(5)))) T.active = saved.active;
    } catch (e) { /* nothing saved */ }
    saveTabs();
  }

  const active = tools.filter(t => t.state !== "stopped");
  const running = listed.filter(t => t.state === "running");
  const unseen = active.filter(t => t.state === "leftover" || (t.state === "running" && t.windows === 0 && !T.tabs.includes(t.key)));
  $("#k-running").textContent = `${running.length} of ${listed.length}`;
  $("#k-running-s").textContent = running.length ? running.map(t => t.name).join(", ") : "all switched off";
  $("#k-cpu").textContent = pct(active.reduce((a, t) => a + t.cpu, 0));
  $("#k-mem").textContent = mem(active.reduce((a, t) => a + t.rss, 0));
  $("#k-bg").textContent = unseen.length;
  $("#k-bg-s").textContent = unseen.length ? unseen.map(t => t.name).join(", ") : (active.length ? "every running tool has a window" : "nothing in the background");
  const qa = $("#quit-all"); qa.disabled = !active.length;
  if (!qa.classList.contains("armed")) qa.textContent = "All off";

  // the rack
  const rack = $("#rack"), keep = new Set(tools.map(t => t.key));
  for (const [key, m] of S.mods) if (!keep.has(key)) { m.remove(); S.mods.delete(key); }
  tools.forEach((t, i) => {
    let m = S.mods.get(t.key);
    if (!m) { m = makeMod(t); S.mods.set(t.key, m); rack.appendChild(m); }
    m.style.order = t.key === "other" ? 99 : i;
    updateMod(m, t);
  });

  // a request from outside this window to bring a tool up here (the app forgets it once it hears the key is open)
  for (const key of st.wanted || []) {
    const t = tool(key); if (!t || !t.embed) continue;
    if (!T.tabs.includes(key)) { T.tabs.push(key); S.pending.set(key, now); }
    T.active = "tool:" + key; toldTabs = null; saveTabs();
  }

  // open pages
  for (const key of [...T.tabs]) {
    const t = tool(key);
    if (!t || (t.state === "stopped" && !S.pending.has(key) && !S.busyKeys.has(key)) || t.state === "leftover") {
      const had = T.panes.get(key) && T.panes.get(key).frame;
      closeTab(key);
      if (t && had) toast(`${t.name} has quit.`);
      else if (t) toast(`${t.name} did not start. The logbook has the details.`);
      continue;
    }
    tabKey(t); const p = pane(t);
    p.el.classList.toggle("on", T.active === "tool:" + key);
  }
  if (T.active.startsWith("tool:") && !T.tabs.includes(T.active.slice(5))) T.active = "room";
  $$(".key[data-view]").forEach(b => b.classList.toggle("on", b.dataset.view === T.active));
  $("#room").hidden = T.active !== "room"; $("#log").hidden = T.active !== "log";
  const inTool = T.active.startsWith("tool:");
  document.body.classList.toggle("in-tool", inTool);
  inTool ? Field.stop() : Field.start();

  const act = st.activity || [];
  $("#activity").innerHTML = act.map(a => `<li class="${a.ok ? "" : "bad"}"><time>${clock(a.time)}</time><b>${esc(a.tool)}</b><span>${esc(a.text)}</span></li>`).join("");
  $("#activity-empty").hidden = act.length > 0;
  const nb = $("#n-log"); nb.hidden = !act.length; nb.textContent = act.length;
  Field.load = { n: running.length, cpu: active.reduce((a, t) => a + t.cpu, 0) };
}

/* ---------- opening */
async function start(key, inside) {
  const t = tool(key); if (!t) return;
  S.pending.set(key, Date.now());
  if (inside && t.embed) openTab(key); else render();
  try {
    const r = await post("/api/launch", { key, inside: !!(inside && t.embed) });
    if (!r.ok) throw new Error(r.text);
    if (!inside || !t.embed) toast(`${t.name} is opening in a browser tab.`);
  } catch (e) {
    S.pending.delete(key); closeTab(key); render();
    toast(`${t.name} could not be opened: ${e.message}`);
  }
}
function act(key) {
  const t = tool(key); if (!t) return;
  if (t.state === "leftover") return askQuit(key);
  if (t.state === "stopped") return start(key, true);
  if (!t.embed) return window.open(t.url, "_blank", "noopener");
  openTab(key);
}
function popOut(key) {
  const t = tool(key); if (!t) return;
  if (t.state === "stopped") return start(key, false);
  window.open(t.url, "_blank", "noopener");            // the new tab connects before this page lets go, so the tool stays up
  setTimeout(() => { closeTab(key); render(); }, 1500);
}

/* ---------- quitting */
function askQuit(key) {
  const t = tool(key); if (!t) return;
  if (t.busy && !S.confirm.has(key)) { S.confirm.add(key); return render(); }
  quit(key);
}
async function quit(key) {
  const t = tool(key); if (!t) return;
  S.confirm.delete(key); S.pending.delete(key); S.busyKeys.add(key); closeTab(key); render();
  try { const r = await post("/api/stop", { key }); toast(`${t.name}: ${r.text}`); }
  catch (e) { toast(`${t.name} could not be stopped: ${e.message}`); }
  finally { S.busyKeys.delete(key); render(); }
}
function arm(button, label, restore, go) {
  let timer;
  button.onclick = async () => {
    if (button.disabled) return;
    if (!button.classList.contains("armed")) {
      button.classList.add("armed"); button.textContent = label();
      clearTimeout(timer); timer = setTimeout(() => { button.classList.remove("armed"); button.textContent = restore; }, 4000);
      return;
    }
    clearTimeout(timer); button.classList.remove("armed"); button.textContent = restore;
    await go();
  };
}
const activeTools = () => S.state ? S.state.tools.filter(t => t.state !== "stopped") : [];
async function allOff() {
  const list = activeTools();
  list.forEach(t => { S.busyKeys.add(t.key); closeTab(t.key); }); S.pending.clear(); render();
  try { const r = await post("/api/stop-all"); if (list.length) toast(r.ok ? "Everything is switched off." : "Some processes could not be stopped; see the logbook."); }
  catch (err) { toast(`Could not switch off: ${err.message}`); }
  finally { S.busyKeys.clear(); render(); }
}
arm($("#quit-all"), () => `Switch off ${plural(activeTools().length, "tool")}?`, "All off", allOff);
arm($("#power"), () => activeTools().length ? `Off, with ${plural(activeTools().length, "tool")}?` : "Quit launcher?", "Power", async () => {
  S.quit = true;
  await allOff();
  try { await post("/api/quit"); } catch (e) { /* it may already be gone */ }
  gone("Everything is off", "Toolbox Launcher has quit. You can close this tab.");
});
function gone(title, text) { $("#gone-title").textContent = title; $("#gone-text").textContent = text; $("#gone").hidden = false; Field.stop(); }

$$(".key[data-view]").forEach(b => { b.onclick = () => show(b.dataset.view === T.active && b.dataset.view === "log" ? "room" : b.dataset.view); });

/* ---------- the live connection */
let lost = 0;
function connect() {
  const es = new EventSource(`/api/stream?token=${TOKEN}`);
  es.onmessage = e => {
    lost = 0; S.state = JSON.parse(e.data);
    $("#live").className = "live on"; $("#live-text").textContent = "live";
    if (!S.quit) $("#gone").hidden = true;
    render();
  };
  es.onerror = () => {
    if (S.quit) return es.close();
    $("#live").className = "live off"; $("#live-text").textContent = "reconnecting";
    if (++lost >= 4) { es.close(); gone("Toolbox Launcher is not running", "Open it again from the Toolbox, then reload this page."); }
  };
}

/* ---------- the wall behind the rack: a Lissajous figure, the classic picture on an oscilloscope.
   It gets more intricate with every tool that is on, and turns faster when they work harder.
   Drawn at half size, 20 frames a second, and rested while the window is hidden or a tool's page is showing. */
const Field = (() => {
  const cv = $("#bg"), ctx = cv.getContext("2d");
  const RATIOS = [[1, 1], [1, 2], [2, 3], [3, 4], [3, 5], [4, 5], [5, 6], [5, 7], [7, 8]];
  let timer = null, w = 0, h = 0, t = 0, drift = Math.PI / 2;      // starts as a circle, not a line
  const api = { load: { n: 0, cpu: 0 }, start, stop, repaint: () => size() };
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  function size() { w = cv.width = Math.ceil(window.innerWidth / 2); h = cv.height = Math.ceil(window.innerHeight / 2); ctx.fillStyle = css("--wall"); ctx.fillRect(0, 0, w, h); }
  function frame() {
    ctx.globalAlpha = .09; ctx.fillStyle = css("--wall"); ctx.fillRect(0, 0, w, h); ctx.globalAlpha = 1;    // the phosphor fades slowly
    const [a, b] = RATIOS[Math.min(api.load.n, RATIOS.length - 1)];
    const speed = .012 * (1 + Math.min(3, Math.log1p(api.load.cpu) / 2)), r = Math.min(w, h) * .44, cx = w / 2, cy = h * .54;
    drift += .0025;
    ctx.beginPath();
    for (let i = 0; i <= 14; i++) {
      const x = cx + r * 1.25 * Math.sin(a * t + drift), y = cy + r * Math.sin(b * t);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); t += speed;
    }
    t -= speed;
    ctx.strokeStyle = css("--trace"); ctx.lineWidth = 1; ctx.stroke();
  }
  function start() { if (timer || document.hidden || !$("#anim").checked || matchMedia("(prefers-reduced-motion: reduce)").matches) return; if (!w) size(); timer = setInterval(frame, 50); }
  function stop(clear) { clearInterval(timer); timer = null; if (clear) size(); }
  window.addEventListener("resize", () => size());
  document.addEventListener("visibilitychange", () => document.hidden ? stop() : (T.active.startsWith("tool:") ? null : start()));
  return api;
})();
/* ---------- Light | Dark. Dark is the original look and the default. The choice is remembered, applied in <head> before the
   first paint, and handed to the tools shown inside this page: in their address when they open, and as a message when
   it changes while they are open (the convention is on the team board). */
const Theme = (() => {
  const ok = v => v === "light" || v === "dark";
  const chosen = () => { try { const v = localStorage.getItem("theme"); return ok(v) ? v : null; } catch (e) { return null; } };
  const current = () => chosen() || "dark";
  function apply(v) {
    document.documentElement.dataset.theme = v;
    try { localStorage.setItem("theme", v); } catch (e) { /* storage may be unavailable */ }
    $("#pick").dataset.v = v;
    Field.repaint();
    for (const p of T.panes.values()) if (p.frame && p.frame.contentWindow) p.frame.contentWindow.postMessage({ type: "toolbox-appearance", appearance: v }, "*");
  }
  $("#pick").dataset.v = current();
  $$("#pick button").forEach(b => { b.onclick = () => apply(b.dataset.set); });
  return { chosen, current };
})();

const anim = $("#anim");
try { anim.checked = localStorage.getItem("toolbox-launcher-scope") !== "off"; } catch (e) { /* storage may be unavailable */ }
anim.onchange = () => {
  try { localStorage.setItem("toolbox-launcher-scope", anim.checked ? "on" : "off"); } catch (e) { /* ignore */ }
  anim.checked ? Field.start() : Field.stop(true);
};
Field.stop(true); Field.start();
setInterval(() => { if (T.tabs.some(k => { const p = T.panes.get(k); return p && !p.frame; })) render(); }, 1000);   // the warm-up seconds
connect();
})();
