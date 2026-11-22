/* DataLens AI front-end: vanilla JS, no build step. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
let META = null;
const charts = {};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const nice = (k) => k.replace(/_/g, " ");

/* A random id for this browser; the public server keeps uploads and saved items separate per id. */
const SID = (() => {
  let id = null;
  try { id = localStorage.getItem("dl_sid"); } catch (_) { /* storage blocked */ }
  if (!id) {
    const rand = crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "") : Math.random().toString(16).slice(2) + Date.now().toString(16) + Math.random().toString(16).slice(2);
    id = rand.padEnd(24, "0").slice(0, 48);
    try { localStorage.setItem("dl_sid", id); } catch (_) { /* ignore */ }
  }
  return id;
})();

async function api(path, body, method) {
  const opt = { method: method || (body ? "POST" : "GET"), headers: { "X-Session-Id": SID } };
  if (body instanceof FormData) opt.body = body;
  else if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { /* non-json */ }
  if (!r.ok) throw new Error((data && data.detail) || `Request failed (${r.status})`);
  return data;
}

function fmt(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "boolean") return v ? "yes" : "";
  return String(v);
}

function tableHTML(cols, rows) {
  if (!rows || !rows.length) return '<p class="hint">No rows.</p>';
  const numeric = cols.map((c) => rows.some((r) => typeof r[c] === "number"));
  const head = cols.map((c, i) => `<th class="${numeric[i] ? "num" : ""}">${esc(nice(c))}</th>`).join("");
  const body = rows.map((r) => `<tr class="${r.small_sample ? "small" : ""}">` + cols.map((c, i) => `<td class="${numeric[i] ? "num" : ""}">${esc(fmt(r[c]))}</td>`).join("") + "</tr>").join("");
  return `<div class="tbl"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function drawChart(canvas, labels, values, label, horizontal = true) {
  if (!window.Chart || !canvas) return;
  if (charts[canvas.id]) charts[canvas.id].destroy();
  const css = getComputedStyle(document.documentElement);
  const accent = css.getPropertyValue("--accent").trim() || "#0e7c66";
  const ink = css.getPropertyValue("--muted").trim() || "#666";
  charts[canvas.id] = new Chart(canvas, {
    type: "bar",
    data: { labels, datasets: [{ label, data: values, backgroundColor: accent }] },
    options: { indexAxis: horizontal ? "y" : "x", plugins: { legend: { display: false } },
      scales: { x: { ticks: { color: ink } }, y: { ticks: { color: ink } } }, maintainAspectRatio: false },
  });
}

let uid = 0;
/** Render a result block: title, warnings, optional chart, table and export/save actions. */
function renderBlock(el, o) {
  const id = "cv" + ++uid;
  const warn = (o.warnings || []).map((w) => `<div class="warn">${esc(w)}</div>`).join("");
  const chart = o.chart ? `<div style="height:${Math.min(360, 40 + o.rows.length * 22)}px"><canvas id="${id}"></canvas></div>` : "";
  const sql = o.sql ? `<details><summary>Show query</summary><pre>${esc(o.sql)}</pre></details>` : "";
  const actions = o.noActions ? "" : `<div class="actions"><button data-a="csv">Download CSV</button><button data-a="report">Download report</button>${o.spec ? '<button data-a="save">Save</button>' : ""}</div>`;
  const block = document.createElement("div");
  block.className = "card";
  block.innerHTML = `<h2>${esc(o.title)}</h2>${o.sub ? `<p class="hint">${esc(o.sub)}</p>` : ""}${warn}${o.extra || ""}${chart}${tableHTML(o.cols, o.rows)}${sql}${actions}`;
  el.appendChild(block);
  if (o.chart) drawChart($("#" + id, block), o.rows.map((r) => r[o.chart.label]), o.rows.map((r) => r[o.chart.value]), nice(o.chart.value), o.chart.horizontal !== false);
  $$("button[data-a]", block).forEach((b) => (b.onclick = () => exportAction(b.dataset.a, o)));
  return block;
}

async function exportAction(kind, o) {
  try {
    if (kind === "save") {
      const name = prompt("Name this project:", o.title);
      if (!name) return;
      await api("/api/projects", { name, kind: o.spec.kind, spec: o.spec });
      toast("Saved. Find it in the Saved tab.");
      return;
    }
    const body = { title: o.title, columns: o.cols, rows: o.rows, sections: [{ title: o.title, text: o.sub || "", columns: o.cols, rows: o.rows, warnings: o.warnings || [] }] };
    const r = await fetch("/api/export/" + (kind === "csv" ? "csv" : "report"), { method: "POST", headers: { "Content-Type": "application/json", "X-Session-Id": SID }, body: JSON.stringify(body) });
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = kind === "csv" ? "datalens-export.csv" : "datalens-report.html";
    a.click();
  } catch (e) { alert(e.message); }
}

function toast(msg) { const s = $("#status"); const old = s.textContent; s.textContent = msg; setTimeout(() => (s.textContent = old), 2500); }
function showErr(el, e) { el.innerHTML = `<div class="card err">${esc(e.message)}</div>`; }
async function busy(btn, fn) { btn.disabled = true; try { await fn(); } finally { btn.disabled = false; } }

/* ---------------------------------------------------------------- tabs */
$$("#tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
function showTab(t) {
  $$("#tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === t));
  $$(".tab").forEach((x) => x.classList.toggle("active", x.id === "tab-" + t));
  if (t === "saved") loadSaved();
}

/* ---------------------------------------------------------------- player autocomplete */
let acTimer;
document.addEventListener("input", (e) => {
  if (e.target.getAttribute("list") !== "players") return;
  clearTimeout(acTimer);
  const q = e.target.value;
  if (q.length < 2) return;
  acTimer = setTimeout(async () => {
    try {
      const res = await api("/api/players/search?q=" + encodeURIComponent(q));
      $("#players").innerHTML = res.map((r) => `<option value="${esc(r.player)}">`).join("");
    } catch (_) { /* ignore */ }
  }, 150);
});

/* ---------------------------------------------------------------- scope (format / gender / competition) */
function scope() {
  const s = { format: $("#sc-format").value, gender: $("#sc-gender").value };
  const c = $("#sc-comp").value.trim();
  if (c) s.competition = c;
  return s;
}
function restoreScope() {
  try {
    const saved = JSON.parse(localStorage.getItem("dl_scope") || "{}");
    if (saved.format) $("#sc-format").value = saved.format;
    if (saved.gender) $("#sc-gender").value = saved.gender;
    if (saved.competition) $("#sc-comp").value = saved.competition;
  } catch (_) { /* ignore */ }
  ["#sc-format", "#sc-gender", "#sc-comp"].forEach((id) => $(id).addEventListener("change", () => {
    try { localStorage.setItem("dl_scope", JSON.stringify(scope())); } catch (_) { /* ignore */ }
  }));
}

/* ---------------------------------------------------------------- init */
async function init() {
  META = await api("/api/meta");
  if (META.max_upload_mb) $("#max-mb").textContent = META.max_upload_mb;
  if (META.public) $$(".public-only").forEach((el) => (el.hidden = false));
  $("#status").textContent = `${META.counts.matches} matches - ${META.counts.balls.toLocaleString()} balls - chat: ${META.chat_mode}`;
  const fill = (sel, items, blank) => { $(sel).innerHTML = (blank ? `<option value="">${blank}</option>` : "") + items.map((i) => `<option>${esc(i)}</option>`).join(""); };
  const dl = (id, items) => { $(id).innerHTML = items.map((i) => `<option value="${esc(i)}">`).join(""); };
  dl("#venue-list", META.venues); dl("#team-list", META.teams);
  dl("#comp-list", [...new Set(META.competitions.map((c) => c.competition))]);
  restoreScope();
  setRoleOptions();
  setMetricChips();
  const ex = ["best middle order batters by strike rate since 2022", "top 10 death bowlers by economy in the last 3 seasons", "top openers by average at Wankhede since 2023",
    "Kohli vs spin", "Kohli vs Bumrah", "players similar to Hardik Pandya", "best finishers against spin since 2023", "venue profile for Chepauk"];
  $("#examples").innerHTML = ex.map((q) => `<span class="chip">${esc(q)}</span>`).join("");
  $$("#examples .chip").forEach((c) => (c.onclick = () => { $("#chatin").value = c.textContent; $("#chatform").requestSubmit(); }));
  buildSlots();
  refreshComp();
  addChat("assistant", `Ask me about IPL players. I can rank by role (opener, middle order, finisher, powerplay / middle / death bowlers), phase, venue and matchups. ${META.chat_mode === "rules" ? "Running in built-in parser mode - set ANTHROPIC_API_KEY for free-form questions." : ""}`);
}

/* ---------------------------------------------------------------- chat */
const history = [];
function addChat(role, text, extra) {
  const m = document.createElement("div");
  m.className = "msg " + role;
  m.innerHTML = `<div>${esc(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>")}</div>`;
  if (extra) m.appendChild(extra);
  $("#chatlog").appendChild(m);
  $("#chatlog").scrollTop = 1e9;
  return m;
}
$("#chatform").onsubmit = async (e) => {
  e.preventDefault();
  const q = $("#chatin").value.trim();
  if (!q) return;
  $("#chatin").value = "";
  addChat("user", q);
  const wait = addChat("assistant", "Thinking...");
  try {
    const r = await api("/api/chat", { message: q, history: history.slice(-8), scope: scope() });
    wait.remove();
    const box = document.createElement("div");
    (r.warnings || []).forEach((w) => { const d = document.createElement("div"); d.className = "warn"; d.textContent = w; box.appendChild(d); });
    (r.tables || []).forEach((t) => { const h = document.createElement("div"); h.innerHTML = `<p class="hint" style="margin:8px 0 2px">${esc(t.title)}</p>` + tableHTML(t.columns, t.rows); box.appendChild(h); });
    if ((r.steps || []).length) {
      const s = document.createElement("div");
      s.className = "steps";
      s.innerHTML = "Steps: " + r.steps.map((x) => `<span class="badge">${esc(x.tool)}</span>`).join("") + `<details><summary>Show how</summary><pre>${esc(JSON.stringify(r.steps.map((x) => ({ tool: x.tool, args: x.args, sql: x.sql })), null, 2))}</pre></details>`;
      box.appendChild(s);
    }
    addChat("assistant", r.answer, box);
    history.push({ role: "user", content: q }, { role: "assistant", content: r.answer });
  } catch (err) { wait.remove(); addChat("assistant", "Sorry: " + err.message); }
};

/* ---------------------------------------------------------------- rankings */
function setRoleOptions() {
  const side = $("#r-side").value;
  const roles = side === "bat" ? META.bat_roles : META.bowl_roles;
  $("#r-role").innerHTML = '<option value="">any (no role filter)</option>' + Object.entries(roles).map(([k, v]) => `<option value="${k}">${nice(k)} - ${esc(v)}</option>`).join("");
}
function setMetricChips() {
  const side = $("#r-side").value;
  const m = side === "bat" ? META.bat_metrics : META.bowl_metrics;
  $("#metric-list").innerHTML = Object.keys(m).map((k) => `<option value="${k}">`).join("");
  $("#metric-chips").innerHTML = Object.entries(m).map(([k, v]) => `<span class="chip" title="${esc(v)}" data-m="${k}">${k}</span>`).join("");
  $$("#metric-chips .chip").forEach((c) => (c.onclick = () => { const f = $("#r-formula"); f.value = f.value && /[a-z_)]$/i.test(f.value.trim()) ? f.value + " + " + c.dataset.m : c.dataset.m; }));
}
$("#r-side").onchange = () => { setRoleOptions(); setMetricChips(); $("#r-formula").value = $("#r-side").value === "bat" ? "strike_rate" : "economy"; };

function collectFilters(prefix = "f") {
  const g = (id) => $("#" + id).value;
  const f = { ...scope() };
  const set = (k, v, num) => { if (v !== "" && v != null) f[k] = num ? Number(v) : v; };
  set("last_n_seasons", g("f-last"), 1); set("season_from", g("f-from"), 1); set("season_to", g("f-to"), 1);
  set("phase", g("f-phase")); set("venue", g("f-venue")); set("team", g("f-team")); set("opposition", g("f-opp"));
  set("innings", g("f-inn"), 1); set("bowler_kind", g("f-kind")); set("batter_hand", g("f-hand"));
  return f;
}
function rankSpec() {
  return { kind: "rank", side: $("#r-side").value, role: $("#r-role").value || null, formula: $("#r-formula").value, order: $("#r-order").value || null,
    filters: collectFilters(), min_balls: $("#r-min").value ? Number($("#r-min").value) : null, limit: Number($("#r-limit").value || 15) };
}
async function runRank(spec) {
  const out = $("#r-out");
  out.innerHTML = '<div class="card">Ranking...</div>';
  try {
    const r = await api("/api/rank", spec);
    out.innerHTML = "";
    const bat = r.side === "bat";
    const base = bat ? ["rank", "player", "innings", "balls", "runs", "strike_rate", "average", "boundary_pct", "dot_pct"] : ["rank", "player", "innings", "balls", "wickets", "economy", "bowling_average", "dot_pct", "wickets_per_over"];
    const cols = [...new Set([...base, "score", "nationality"])].filter((c) => r.columns.includes(c));
    const role = r.role ? `Role "${nice(r.role.role)}": ${r.role.definition} - ${r.role.qualifying_players} players qualify.` : "";
    renderBlock(out, { title: `${bat ? "Batters" : "Bowlers"} ranked by ${r.formula}`, sub: `${r.filters_text}. Minimum ${r.min_balls} balls. ${role}`,
      cols, rows: r.rows, warnings: r.warnings, sql: r.sql, chart: r.rows.length ? { label: "player", value: "score" } : null, spec });
  } catch (e) { showErr(out, e); }
}
$("#r-run").onclick = (e) => busy(e.target, () => runRank(rankSpec()));

/* ---------------------------------------------------------------- matchups */
$("#m-kind").onchange = () => {
  const k = $("#m-kind").value;
  $("#m-b-l").hidden = k !== "head_to_head";
  $("#m-a-l").firstChild.textContent = k === "head_to_head" ? "Batter" : k.includes("bowl") ? "Bowler" : "Player";
};
$("#m-run").onclick = (e) => busy(e.target, async () => {
  const out = $("#m-out"); out.innerHTML = '<div class="card">Working...</div>';
  const kind = $("#m-kind").value, a = $("#m-a").value, b = $("#m-b").value, last = $("#m-last").value;
  const filters = { ...scope(), ...(last ? { last_n_seasons: Number(last) } : {}) };
  try {
    let r;
    out.innerHTML = "";
    if (kind === "batter_vs_types") {
      r = await api("/api/matchup", { kind, batter: a, filters });
      const rows = r.by_kind.filter((x) => x.bowler_kind !== "unknown");
      renderBlock(out, { title: `${r.player} vs pace and spin`, sub: r.filters_text, cols: ["bowler_kind", "balls", "runs", "dismissals", "strike_rate", "average", "dot_pct", "boundary_pct"], rows: r.by_kind, warnings: r.warnings, chart: rows.length ? { label: "bowler_kind", value: "strike_rate", horizontal: false } : null });
      renderBlock(out, { title: "By bowling style", cols: ["bowler_kind", "bowler_style", "balls", "runs", "dismissals", "strike_rate", "average"], rows: r.by_style, noActions: true });
    } else if (kind === "bowler_vs_hands") {
      r = await api("/api/matchup", { kind, bowler: a, filters });
      renderBlock(out, { title: `${r.player} vs left- and right-handers`, sub: r.filters_text, cols: ["batter_hand", "balls", "runs", "wickets", "economy", "bowling_average", "dot_pct"], rows: r.by_hand, warnings: r.warnings, chart: { label: "batter_hand", value: "economy", horizontal: false } });
    } else if (kind === "head_to_head") {
      r = await api("/api/matchup", { kind, batter: a, bowler: b, filters });
      const t = r.total;
      const extra = `<div class="kv"><div><b>${t.runs}</b><span>runs</span></div><div><b>${t.balls}</b><span>balls</span></div><div><b>${t.dismissals}</b><span>dismissals</span></div><div><b>${fmt(t.strike_rate)}</b><span>strike rate</span></div><div><b>${fmt(t.average) || "-"}</b><span>average</span></div></div>`;
      renderBlock(out, { title: `${r.batter} vs ${r.bowler}`, sub: r.filters_text, extra, cols: ["season", "balls", "runs", "dismissals", "boundaries", "dots"], rows: r.by_season, warnings: r.warnings });
    } else {
      const side = kind === "opponents_bat" ? "bat" : "bowl";
      r = await api("/api/matchup", { kind: "opponents", player: a, side, filters });
      const cols = side === "bat" ? ["opponent", "balls", "runs", "dismissals", "strike_rate", "average"] : ["opponent", "balls", "runs", "wickets", "economy"];
      renderBlock(out, { title: side === "bat" ? `${r.player}: best against (by strike rate)` : `${r.player}: most wickets / best economy against`, sub: "Minimum 12 balls", cols, rows: r.best });
      renderBlock(out, { title: side === "bat" ? `${r.player}: toughest bowlers (most dismissals)` : `${r.player}: most expensive against`, cols, rows: r.worst });
    }
  } catch (err) { showErr(out, err); }
});

/* ---------------------------------------------------------------- venues */
$("#v-run").onclick = (e) => busy(e.target, async () => {
  const out = $("#v-out"); out.innerHTML = "";
  const v = $("#v-venue").value, from = $("#v-from").value;
  try {
    const r = await api("/api/venues?" + new URLSearchParams({ ...scope(), ...(v ? { venue: v } : {}), ...(from ? { season_from: from } : {}) }));
    const cols = ["venue", "matches", "avg_first_innings", "avg_second_innings", "bat_first_win_pct", "toss_winner_win_pct", "chose_to_field_pct", "powerplay_rpo", "middle_rpo", "death_rpo", "boundary_pct", "wickets_per_match"];
    renderBlock(out, { title: v ? v : "All venues (3+ matches)", sub: r.filters_text, cols, rows: r.venues, warnings: r.warnings, chart: r.venues.length > 1 ? { label: "venue", value: "avg_first_innings" } : null });
  } catch (err) { showErr(out, err); }
});

/* ---------------------------------------------------------------- replacement */
$("#s-run").onclick = (e) => busy(e.target, async () => {
  const out = $("#s-out"); out.innerHTML = "";
  const body = { player: $("#s-player").value, side: $("#s-side").value, n: Number($("#s-n").value || 8), filters: { ...scope(), last_n_seasons: Number($("#s-last").value || 3) } };
  if ($("#s-price").value) body.max_price = Number($("#s-price").value);
  if ($("#s-nat").value) body.nationality = $("#s-nat").value;
  try {
    const r = await api("/api/similar", body);
    const tgt = Object.entries(r.target_stats).map(([k, v]) => `<div><b>${fmt(v)}</b><span>${nice(k)}</span></div>`).join("");
    const rows = r.rows.map((x) => ({ ...x, ...x.stats }));
    const cols = ["player", "similarity", "price_cr", "nationality", ...r.features.slice(0, 5), "closest_on"];
    renderBlock(out, { title: `Players like ${r.player}`, sub: `${r.filters_text}. Prices: ${r.price_source}.`, extra: `<p class="hint">Target profile:</p><div class="kv">${tgt}</div>`, cols, rows, warnings: r.warnings, chart: rows.length ? { label: "player", value: "similarity" } : null });
  } catch (err) { showErr(out, err); }
});

/* ---------------------------------------------------------------- auction */
function buildSlots() {
  const roles = { ...META.bat_roles, ...META.bowl_roles };
  delete roles.pacer; delete roles.spinner;
  $("#a-slots").innerHTML = Object.keys(roles).map((r) => `<label>${nice(r)}<input type="number" min="0" max="6" data-slot="${r}" placeholder="auto"></label>`).join("");
}
const lines = (id) => $(id).value.split("\n").map((s) => s.trim()).filter(Boolean);
const squadList = () => lines("#a-squad");

/* tournament -> team -> squad */
async function loadTeams() {
  const sel = $("#a-team"), c = scope().competition;
  if (!c || c === "International") { sel.innerHTML = '<option value="">Choose a tournament first (e.g. IPL)</option>'; return; }
  try {
    const r = await api("/api/rosters/teams?" + new URLSearchParams(scope()));
    sel.innerHTML = '<option value="">(choose your team)</option>' + r.teams.map((t) => `<option value="${esc(t.team)}">${esc(t.team)} (${t.players})</option>`).join("");
    if (!$("#a-season").value) $("#a-season").value = r.file_season || r.latest_season + 1;
    if (r.free_players.length && !$("#a-free").value) $("#a-free").value = r.free_players.join("\n");
  } catch (e) { sel.innerHTML = `<option value="">${esc(e.message)}</option>`; }
}
function fillTournaments() {
  const f = $("#sc-format").value, g = $("#sc-gender").value, cur = scope().competition || "";
  const items = META.competitions.filter((c) => c.fmt === f && c.gender === g && c.competition !== "International").map((c) => c.competition);
  $("#a-comp").innerHTML = '<option value="">Choose a tournament</option>' + items.map((i) => `<option value="${esc(i)}">${esc(i)}</option>`).join("");
  $("#a-comp").value = items.includes(cur) ? cur : "";
}
function refreshComp() { fillTournaments(); loadTeams(); }
$("#a-comp").onchange = () => {  // choosing a tournament here sets the sidebar scope too
  $("#sc-comp").value = $("#a-comp").value;
  try { localStorage.setItem("dl_scope", JSON.stringify(scope())); } catch (_) { /* ignore */ }
  $("#a-squad").value = ""; $("#a-roster-note").textContent = "";
  refreshComp();
};
["#sc-format", "#sc-gender", "#sc-comp"].forEach((id) => $(id).addEventListener("change", refreshComp));
$("#a-load").onclick = (e) => busy(e.target, async () => {
  const note = $("#a-roster-note");
  try {
    if (!$("#a-team").value) throw new Error("Choose your team first (and set Competition in the sidebar, e.g. IPL).");
    const r = await api("/api/rosters/squad?" + new URLSearchParams({ ...scope(), team: $("#a-team").value }));
    $("#a-squad").value = r.players.join("\n");
    if (r.free_players.length) $("#a-free").value = r.free_players.join("\n");
    note.textContent = `${r.team}: ${r.players.length} players - ${r.source}. Remove anyone you are releasing, then click Find gaps.`;
  } catch (err) { note.textContent = err.message; }
});
$("#a-save").onclick = (e) => busy(e.target, async () => {
  const note = $("#a-save-note");
  try {
    if (!$("#a-team").value) throw new Error("Choose your team first.");
    const r = await api("/api/rosters/save", { filters: scope(), season: Number($("#a-season").value), team: $("#a-team").value,
      retained: squadList(), free: lines("#a-free"), taken: lines("#a-taken") });
    note.textContent = `Saved for ${r.team} ${r.season}: ${r.saved.retained} retained, ${r.saved.free} free, ${r.saved.taken} unavailable.` + (r.unresolved.length ? ` Not recognised: ${r.unresolved.join(", ")}.` : "");
    loadTeams();
  } catch (err) { note.textContent = err.message; }
});
$("#a-rfile").onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  const fd = new FormData(); fd.append("file", f);
  try { const r = await api("/api/rosters/upload", fd); $("#a-save-note").textContent = `Roster file loaded: ${r.rows} rows for ${r.competitions.join(", ")}.` + (r.unmatched.length ? ` Not matched: ${r.unmatched.slice(0, 6).join(", ")}.` : ""); loadTeams(); }
  catch (err) { $("#a-save-note").textContent = err.message; }
};
const aFilters = () => ({ ...scope(), last_n_seasons: Number($("#a-last").value || 3) });
$("#a-gaps").onclick = (e) => busy(e.target, async () => {
  const out = $("#a-out"); out.innerHTML = "";
  try {
    if (!squadList().length) throw new Error("Enter your current squad first (one player per line).");
    const g = await api("/api/squad/gaps", { squad: squadList(), filters: aFilters() });
    renderGaps(out, g);
    $$("#a-slots input").forEach((i) => (i.value = g.gaps[i.dataset.slot] || ""));
  } catch (err) { showErr(out, err); }
});
function renderGaps(out, g) {
  const rows = Object.entries(g.template).map(([role, need]) => ({ role: nice(role), have: g.role_counts[role], need, gap: g.gaps[role] || 0 }));
  renderBlock(out, { title: "Squad role coverage", sub: g.filters_text + (g.unresolved.length ? ` - not recognised: ${g.unresolved.join(", ")}` : ""), cols: ["role", "have", "need", "gap"], rows, warnings: g.warnings, noActions: true });
}
$("#a-run").onclick = (e) => busy(e.target, async () => {
  const out = $("#a-out"); out.innerHTML = '<div class="card">Optimising...</div>';
  const slots = {};
  $$("#a-slots input").forEach((i) => { if (i.value && Number(i.value) > 0) slots[i.dataset.slot] = Number(i.value); });
  const useRoster = $("#a-avail").checked;
  if (useRoster && !$("#a-team").value) { showErr(out, new Error("Pick your team (and set Competition in the sidebar) so other teams' players can be excluded - or untick 'Only suggest players who are available'.")); return; }
  const body = { purse: Number($("#a-purse").value), max_overseas: Number($("#a-os").value), filters: aFilters(), squad: squadList(),
    bat_formula: $("#a-bf").value || null, bowl_formula: $("#a-wf").value || null,
    team: useRoster ? $("#a-team").value : null, use_roster: useRoster, free: lines("#a-free"), taken: lines("#a-taken") };
  if (Object.keys(slots).length) body.slots = slots;
  try {
    const r = await api("/api/budget", body);
    out.innerHTML = "";
    if (r.derived_gaps) renderGaps(out, r.derived_gaps);
    const extra = `<div class="kv"><div><b>${fmt(r.spent)} cr</b><span>spent of ${r.purse}</span></div><div><b>${fmt(r.remaining)} cr</b><span>remaining</span></div><div><b>${r.overseas_used}/${r.max_overseas}</b><span>overseas</span></div><div><b>${fmt(r.total_score)}</b><span>total fit score</span></div></div>` +
      (r.roster ? `<p class="hint">${esc(r.roster.note)} Excluded ${r.roster.excluded_players} players from ${r.roster.other_teams} other teams.${r.roster.unresolved.length ? " Not recognised: " + esc(r.roster.unresolved.join(", ")) + "." : ""}</p>` : "");
    renderBlock(out, { title: r.optimal ? "Best squad within budget" : "Best partial squad within budget", sub: `${r.filters_text}. Prices: ${r.price_source}. Fit score = percentile within role pool.`, extra,
      cols: ["role", "player", "score", "price", "nationality", "last_season"], rows: r.selected, warnings: r.warnings, chart: { label: "player", value: "price" } });
    renderBlock(out, { title: "Next-best value alternatives", cols: ["player", "role", "score", "price", "nationality", "last_season"], rows: r.alternatives, noActions: true });
  } catch (err) { showErr(out, err); }
});
$("#a-file").onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  const fd = new FormData(); fd.append("file", f);
  try { const r = await api("/api/prices/upload", fd); $("#a-src").textContent = `${r.count} prices loaded${r.unmatched.length ? `, ${r.unmatched.length} names not matched (${r.unmatched.slice(0, 5).join(", ")}...)` : ""}`; }
  catch (err) { $("#a-src").textContent = err.message; }
};
api("/api/prices?limit=1").then((r) => ($("#a-src").textContent = `Price list: ${r.source}`)).catch(() => {});

/* ---------------------------------------------------------------- my data */
$("#u-go").onclick = (e) => busy(e.target, async () => {
  const f = $("#u-file").files[0]; const out = $("#u-out");
  if (!f) { showErr(out, new Error("Choose a file first")); return; }
  const fd = new FormData(); fd.append("file", f);
  try { renderUpload(await api("/api/upload", fd)); } catch (err) { showErr(out, err); }
});
function renderUpload(p) {
  const out = $("#u-out"); out.innerHTML = "";
  const issues = p.issues.length ? `<ul>${p.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : "<p>No issues found.</p>";
  const act = p.actions ? `<div class="warn">Cleaning applied: ${esc(p.actions.join("; "))} (${p.rows_before} &rarr; ${p.rows_after} rows)</div>` : "";
  const extra = `${act}<div class="kv"><div><b>${p.quality_score}</b><span>quality score /100</span></div><div><b>${p.rows}</b><span>rows</span></div><div><b>${p.columns.length}</b><span>columns</span></div><div><b>${p.duplicates}</b><span>duplicates</span></div><div><b>${p.missing_cells}</b><span>missing cells</span></div></div>${issues}
    <div class="actions"><button id="u-clean">Auto-clean</button><button id="u-clean2">Auto-clean + fill missing numbers (median)</button></div>`;
  const blk = renderBlock(out, { title: `Table ${p.table}`, extra, cols: ["name", "dtype", "missing_pct", "unique", "min", "max", "mean", "outliers"], rows: p.columns, noActions: true });
  $("#u-clean", blk).onclick = () => api(`/api/upload/${p.table}/clean`, {}, "POST").then((r) => renderUpload({ ...r, table: p.table })).catch((e) => alert(e.message));
  $("#u-clean2", blk).onclick = () => api(`/api/upload/${p.table}/clean?fill=median`, {}, "POST").then((r) => renderUpload({ ...r, table: p.table })).catch((e) => alert(e.message));
  renderBlock(out, { title: "Preview", cols: p.columns.map((c) => c.name), rows: p.preview, noActions: true });
  const names = p.columns.map((c) => c.name);
  const sqlCard = document.createElement("div");
  sqlCard.className = "card";
  sqlCard.innerHTML = `<h2>Explore</h2><div class="row"><input id="u-sql" value="SELECT * FROM ${p.table} LIMIT 20"><button class="primary" id="u-sqlgo">Run SQL</button></div>
    <div class="grid"><label>Chart: group by<select id="u-x">${names.map((n) => `<option>${esc(n)}</option>`).join("")}</select></label>
    <label>Value<select id="u-y"><option value="">(count rows)</option>${names.map((n) => `<option>${esc(n)}</option>`).join("")}</select></label>
    <label>Aggregate<select id="u-agg"><option>count</option><option>sum</option><option>avg</option><option>min</option><option>max</option><option>median</option></select></label></div>
    <button id="u-chartgo">Draw chart</button><div id="u-res"></div>`;
  out.appendChild(sqlCard);
  $("#u-sqlgo", sqlCard).onclick = async () => {
    const res = $("#u-res", sqlCard);
    try { const r = await api("/api/upload/sql", { query: $("#u-sql", sqlCard).value }); res.innerHTML = tableHTML(r.columns, r.rows); } catch (err) { showErr(res, err); }
  };
  $("#u-chartgo", sqlCard).onclick = async () => {
    const res = $("#u-res", sqlCard);
    try {
      const q = new URLSearchParams({ x: $("#u-x", sqlCard).value, agg: $("#u-agg", sqlCard).value, ...($("#u-y", sqlCard).value ? { y: $("#u-y", sqlCard).value } : {}) });
      const r = await api(`/api/upload/${p.table}/chart?` + q);
      res.innerHTML = '<div style="height:320px"><canvas id="u-canvas"></canvas></div>';
      drawChart($("#u-canvas"), r.labels, r.values, "value", false);
    } catch (err) { showErr(res, err); }
  };
}

/* ---------------------------------------------------------------- saved */
async function loadSaved() {
  const box = $("#saved-list");
  try {
    const items = await api("/api/projects");
    if (!items.length) { box.innerHTML = '<p class="hint">Nothing saved yet. Use the Save button on a ranking.</p>'; return; }
    box.innerHTML = items.map((p) => `<div class="row" data-id="${p.id}"><b>${esc(p.name)}</b><span class="badge">${esc(p.kind)}</span><span class="hint">${esc(p.created)}</span><button data-open>Open</button><button data-del>Delete</button></div>`).join("");
    $$("[data-open]", box).forEach((b) => (b.onclick = async () => {
      const p = await api("/api/projects/" + b.closest("[data-id]").dataset.id);
      if (p.spec.kind === "rank") {
        const s = p.spec;
        $("#r-side").value = s.side; setRoleOptions(); setMetricChips();
        $("#r-role").value = s.role || ""; $("#r-formula").value = s.formula; $("#r-order").value = s.order || "";
        const f = s.filters || {};
        $("#f-last").value = f.last_n_seasons || ""; $("#f-from").value = f.season_from || ""; $("#f-to").value = f.season_to || "";
        $("#f-phase").value = f.phase || ""; $("#f-venue").value = f.venue || ""; $("#f-team").value = f.team || ""; $("#f-opp").value = f.opposition || "";
        $("#f-inn").value = f.innings || ""; $("#f-kind").value = f.bowler_kind || ""; $("#f-hand").value = f.batter_hand || "";
        $("#r-min").value = s.min_balls || ""; $("#r-limit").value = s.limit || 15;
        showTab("rank"); runRank(s);
      } else alert(JSON.stringify(p.spec, null, 2));
    }));
    $$("[data-del]", box).forEach((b) => (b.onclick = async () => { if (confirm("Delete this project?")) { await api("/api/projects/" + b.closest("[data-id]").dataset.id, null, "DELETE"); loadSaved(); } }));
  } catch (e) { showErr(box, e); }
}

init().catch((e) => { $("#status").textContent = "Failed to load: " + e.message; });
