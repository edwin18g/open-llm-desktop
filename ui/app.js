/* EK Light LLM — UI logic */
"use strict";
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
/* Python API over HTTP: api().method(...args) → POST /api/method (see server.py) */
const TOKEN = new URLSearchParams(location.search).get("t") || sessionStorage.getItem("ekToken") || "";
try{ sessionStorage.setItem("ekToken", TOKEN); history.replaceState(null, "", "/") }catch(e){}
const API = new Proxy({}, {get: (_, name) => async (...args) => {
  const r = await fetch("/api/" + name, {method: "POST", headers: {"Content-Type": "application/json", "X-Token": TOKEN}, body: JSON.stringify(args)});
  const j = await r.json().catch(() => ({error: "Bad response from app"}));
  if(!r.ok) throw new Error(j.error || r.statusText);
  return j.result;
}});
const api = () => API;
function downloadText(name, text, type = "text/plain"){
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], {type})); a.download = name;
  document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
function chooseFiles(onFiles){
  const i = document.createElement("input"); i.type = "file"; i.multiple = true;
  i.onchange = () => { if(i.files.length) onFiles([...i.files]) }; i.click();
}
const ic = (n, st = "") => `<svg class="i" style="${st}"><use href="#i-${n}"/></svg>`;
function esc(s){ return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;") }
function toast(t, ms = 3200){ const el = $("#toast"); el.textContent = t; el.classList.add("show"); clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove("show"), ms) }
function fmtBytes(b){ if(!b) return "0 B"; const u=["B","KB","MB","GB"]; let i=0; while(b>=1024&&i<3){b/=1024;i++} return b.toFixed(i?1:0)+" "+u[i] }
function lsGet(k, d){ try{ const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v) }catch(e){ return d } }
function lsSet(k, v){ try{ localStorage.setItem(k, JSON.stringify(v)) }catch(e){} }

/* ============================ state ============================ */
const S = {
  state: null, ready: false, busy: false,
  chatId: null, projectId: null, chatTitle: "", messages: [],
  cur: null,                                   // streaming bot message
  attached: new Set(lsGet("attachedPlugins", [])),
  pending: [],                                 // composer attachments
  catalog: [], servers: [], projects: [],
  style: "normal", thinking: false,
};
const ARTS = {};        // artifact key -> {title, lang, code}
let renderCtx = null;

/* ============================ markdown ============================ */
function artTitle(code, lang){
  const t = code.match(/<title>([^<]{1,80})<\/title>/i) || code.match(/<h1[^>]*>([^<]{1,80})<\/h1>/i);
  if(t) return t[1].trim();
  return lang === "svg" ? "SVG image" : "Web page";
}
const renderer = new marked.Renderer();
renderer.code = function(tok){
  const text = tok.text || "", lang = (tok.lang || "").trim().split(/\s+/)[0].toLowerCase();
  const ctx = renderCtx || {}; const idx = ctx.idx = (ctx.idx ?? -1) + 1;
  const open = ctx.streaming && ctx.openIdx === idx;
  const isArt = lang === "html" || lang === "svg" || (!lang && /^\s*<(!doctype html|html|svg)/i.test(text));
  if(isArt && ctx.msgKey){
    const key = `${ctx.msgKey}#${idx}`, title = artTitle(text, lang === "svg" || /^\s*<svg/i.test(text) ? "svg" : "html");
    ARTS[key] = {title, lang: lang || (/^\s*<svg/i.test(text) ? "svg" : "html"), code: text};
    const lines = text.split("\n").length;
    return `<button class="artcard" data-art="${key}"><span class="ai"><svg class="i" viewBox="0 0 24 24" style="width:20px;height:20px"><path d="m8 7-5 5 5 5M16 7l5 5-5 5"/></svg></span><span><b>${esc(title)}</b><small>${open ? '<span class="spin" style="width:9px;height:9px;vertical-align:middle"></span> Writing… ' : "Click to open · "}${lines} lines</small></span></button>`;
  }
  let body;
  if(!ctx.streaming && window.hljs){
    try{ body = lang && hljs.getLanguage(lang) ? hljs.highlight(text, {language: lang}).value : esc(text) }catch(e){ body = esc(text) }
  } else body = esc(text);
  return `<div class="cb"><div class="cbh"><span>${esc(lang || "text")}</span><button data-copy="1">Copy</button></div><pre><code class="hljs">${body}</code></pre></div>`;
};
renderer.link = function(tok){ return `<a href="${esc(tok.href)}" title="${esc(tok.title||"")}">${this.parser.parseInline(tok.tokens)}</a>` };
marked.use({renderer, gfm: true, breaks: false});

let mdLastIdx = -1;  // index of the last code block md() rendered (streaming keeps counting across calls)
function md(text, msgKey, streaming = false, startIdx = -1){
  const fences = (text.match(/^\s*```/gm) || []).length;
  renderCtx = {msgKey, streaming, idx: startIdx, openIdx: streaming && fences % 2 === 1 ? startIdx + 1 + Math.floor(fences / 2) : -1};
  let html;
  try{ html = marked.parse(text || "") }catch(e){ html = `<p>${esc(text)}</p>` }
  mdLastIdx = renderCtx.idx; renderCtx = null;
  return DOMPurify.sanitize(html, {ADD_ATTR: ["data-art", "data-copy"]});
}

/* ============================ helpers ============================ */
function hashColor(s){ let h=0; for(const c of s) h=(h*31+c.charCodeAt(0))>>>0; return `hsl(${h%360} 42% 46%)` }
function plugMeta(nameOrId){
  const srv = S.servers.find(s => s.name === nameOrId);
  const cat = S.catalog.find(c => c.id === ((srv && srv.plugin) || nameOrId));
  const title = (srv && srv.title) || (cat && cat.title) || nameOrId;
  return {title, color: (cat && cat.color) || hashColor(title)};
}
function tile(nameOrId, cls = ""){
  const m = plugMeta(nameOrId); const w = m.title.replace(/[^A-Za-z0-9 ]/g," ").split(/\s+/).filter(Boolean);
  const ab = w.length > 1 ? (w[0][0]+w[1][0]).toUpperCase() : ((w[0]||"?")[0].toUpperCase() + ((w[0]||"")[1]||""));
  return `<span class="tile ${cls}" style="background:${m.color}">${esc(ab)}</span>`;
}
function extOf(name){ const m = /\.([a-z0-9]{1,5})$/i.exec(name||""); return m ? m[1] : "file" }
function fileCard(a, removable){
  if(a.error) return "";
  const rm = removable ? `<button class="rm" data-rm="${esc(a.tmp||a.id)}" title="Remove">×</button>` : "";
  if(a.kind === "image" && a.thumb) return `<div class="fcard img${a.uploading?" up":""}" data-att="${esc(a.id||"")}" title="${esc(a.name)}"><img src="${esc(attSrc(a.thumb))}" alt="" loading="lazy" decoding="async">${rm}</div>`;
  const sub = a.uploading ? "Uploading…" : (a.kind === "image" ? "Image" : a.kind === "audio" ? "Audio" : `${(a.kind||"").toUpperCase()} · ${fmtBytes(a.size)}`);
  return `<div class="fcard${a.uploading?" up":""}" data-att="${esc(a.id||"")}"><span class="fi">${a.uploading?'<span class="spin"></span>':esc(extOf(a.name))}</span><span class="fn"><b>${esc(a.name)}</b><small>${esc(sub)}</small></span>${rm}</div>`;
}
function openModal(id){ $("#"+id).classList.remove("hidden") }
function closeModals(){ $$(".modal").forEach(m => m.classList.add("hidden")) }
$$("[data-close]").forEach(b => b.onclick = closeModals);
$$(".modal").forEach(m => m.addEventListener("mousedown", e => { if(e.target === m) closeModals() }));

/* generic menu */
function showMenu(anchor, items){
  closeMenu();
  const m = document.createElement("div"); m.className = "menu"; m.id = "ctxMenu";
  m.innerHTML = items.map((it, i) => it === "-" ? `<div class="sep"></div>` : it.label && !it.fn && !it.sub ? `<div class="lbl">${esc(it.label)}</div>`
    : `<button data-i="${i}" class="${it.danger?"danger":""}">${it.icon?ic(it.icon,"width:16px;height:16px"):""}<span style="flex:1">${esc(it.label)}</span>${it.check?ic("check","width:15px;height:15px;color:var(--accent)"):""}${it.sub?ic("chev","width:14px;height:14px"):""}</button>`).join("");
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect(), mw = m.offsetWidth, mh = m.offsetHeight;
  let x = r.left, y = r.bottom + 4;
  if(x + mw > innerWidth - 8) x = innerWidth - mw - 8;
  if(y + mh > innerHeight - 8) y = r.top - mh - 4;
  m.style.left = Math.max(8, x) + "px"; m.style.top = Math.max(8, y) + "px";
  m.querySelectorAll("button[data-i]").forEach(b => b.onclick = e => {
    e.stopPropagation(); const it = items[+b.dataset.i];
    if(it.sub){ showMenu(b, it.sub); return }
    closeMenu(); it.fn && it.fn();
  });
  setTimeout(() => document.addEventListener("mousedown", menuAway), 0);
}
function menuAway(e){ if(!e.target.closest("#ctxMenu")) closeMenu() }
function closeMenu(){ const m = $("#ctxMenu"); if(m) m.remove(); document.removeEventListener("mousedown", menuAway) }

/* prompt / confirm */
function askForm({title, fields = [], body = "", ok = "OK", danger = false}){
  return new Promise(res => {
    $("#mpTitle").textContent = title; $("#mpErr").textContent = "";
    $("#mpBody").innerHTML = (body ? `<p class="sub" style="font-size:14px">${body}</p>` : "") + fields.map(f =>
      `<label class="f">${esc(f.label)}</label>` + (f.type === "textarea"
        ? `<textarea class="inp" data-k="${f.key}" placeholder="${esc(f.placeholder||"")}" style="min-height:${f.h||140}px">${esc(f.value||"")}</textarea>`
        : `<input class="inp" data-k="${f.key}" value="${esc(f.value||"")}" placeholder="${esc(f.placeholder||"")}">`)).join("");
    const okb = $("#mpOk"); okb.textContent = ok; okb.className = "btn " + (danger ? "danger" : "primary");
    openModal("mPrompt");
    const first = $("#mpBody [data-k]"); if(first){ first.focus(); first.select && first.select() }
    const done = v => { closeModals(); okb.onclick = null; $("#mPrompt").onkeydown = null; res(v) };
    okb.onclick = () => { const v = {}; $$("#mpBody [data-k]").forEach(i => v[i.dataset.k] = i.value); done(v) };
    $("#mPrompt").onkeydown = e => { if(e.key === "Enter" && e.target.tagName === "INPUT"){ e.preventDefault(); okb.click() } };
    $$("#mPrompt [data-close]").forEach(b => b.onclick = () => done(null));
  });
}

/* ============================ views ============================ */
let currentView = "chat";
function show(view){
  currentView = view;
  $$(".view").forEach(v => v.classList.toggle("hidden", v.id !== "view-" + view));
  $$(".nav[data-view]").forEach(n => n.classList.toggle("on", n.dataset.view === view));
  if(view !== "chat") closeArtifact();
  if(view === "plugins") renderPlugins();
  if(view === "projects") loadProjects();
  if(view === "settings") fillSettings();
  if(view === "chat") setTimeout(() => $("#input").focus(), 0);
  $("#sideOpen").classList.toggle("hidden", !document.body.classList.contains("side-closed"));
}
$$("[data-view]").forEach(n => n.addEventListener("click", () => show(n.dataset.view)));
function toggleSidebar(){ document.body.classList.toggle("side-closed"); lsSet("sideClosed", document.body.classList.contains("side-closed")); $("#sideOpen").classList.toggle("hidden", !document.body.classList.contains("side-closed")) }
$("#sideToggle").onclick = toggleSidebar; $("#sideOpen").onclick = toggleSidebar;
if(lsGet("sideClosed", false)) document.body.classList.add("side-closed");

/* ============================ sidebar ============================ */
async function loadChats(){
  const chats = await api().list_chats("", null);
  const now = new Date(), d0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  const groups = [["Starred", c => c.starred], ["Today", c => c.updated >= d0], ["Yesterday", c => c.updated >= d0 - 86400],
    ["Previous 7 days", c => c.updated >= d0 - 7*86400], ["Previous 30 days", c => c.updated >= d0 - 30*86400], ["Older", () => true]];
  const used = new Set(); let html = "";
  for(const [name, test] of groups){
    const items = chats.filter(c => !used.has(c.id) && test(c));
    if(name !== "Starred") items.forEach(c => used.add(c.id)); else items.forEach(c => used.add(c.id));
    if(!items.length) continue;
    html += `<div class="grp">${name}</div>` + items.map(chatItem).join("");
  }
  $("#sideList").innerHTML = html || `<div class="side-empty">Your chats will appear here.</div>`;
  bindChatItems($("#sideList"));
}
function chatItem(c){
  return `<div class="ci${c.id === S.chatId ? " on" : ""}" data-id="${c.id}"><span class="t">${esc(c.title)}</span>${c.project_name ? `<span class="pj">${esc(c.project_name)}</span>` : ""}<button class="ib more" data-more="1">${ic("more","width:15px;height:15px")}</button></div>`;
}
function bindChatItems(root){
  root.querySelectorAll(".ci").forEach(el => {
    el.onclick = e => { if(e.target.closest("[data-more]")) return; closeModals(); openChat(el.dataset.id) };
    const mb = el.querySelector("[data-more]");
    if(mb) mb.onclick = e => { e.stopPropagation(); chatMenu(mb, el.dataset.id) };
  });
}
async function chatMenu(anchor, id){
  const all = await api().list_chats("", null); const c = all.find(x => x.id === id) || {};
  const projs = await api().list_projects();
  showMenu(anchor, [
    {label: c.starred ? "Unstar" : "Star", icon: "star", fn: async () => { await api().star_chat(id, !c.starred); loadChats() }},
    {label: "Rename", icon: "pen", fn: () => renameChat(id, c.title)},
    {label: "Add to project", icon: "folder", sub: [
      ...projs.map(p => ({label: p.name, check: p.id === c.project_id, fn: async () => { await api().move_chat(id, p.id); if(id === S.chatId){ S.projectId = p.id; setCrumb(p.name) } loadChats(); toast(`Moved to ${p.name}`) }})),
      ...(c.project_id ? ["-", {label: "Remove from project", fn: async () => { await api().move_chat(id, null); if(id === S.chatId){ S.projectId = null; setCrumb(null) } loadChats() }}] : []),
      ...(projs.length ? [] : [{label: "No projects yet", fn: () => show("projects")}]),
    ]},
    {label: "Export as Markdown", icon: "down", fn: async () => { const f = await api().export_chat(id); if(f) downloadText(f.name, f.text, "text/markdown") }},
    "-",
    {label: "Delete", icon: "trash", danger: true, fn: () => deleteChat(id, c.title)},
  ]);
}
async function renameChat(id, title){
  const v = await askForm({title: "Rename chat", fields: [{key: "t", label: "Title", value: title}], ok: "Save"});
  if(!v) return; await api().rename_chat(id, v.t); if(id === S.chatId) setTitle(v.t); loadChats();
}
async function deleteChat(id, title){
  const v = await askForm({title: "Delete chat?", body: `“${esc(title)}” will be permanently deleted.`, ok: "Delete", danger: true});
  if(!v) return; await api().delete_chat(id); if(id === S.chatId) newChat(); loadChats(); toast("Chat deleted");
}

/* ============================ chat ============================ */
function greet(){
  const h = new Date().getHours(), n = (S.state && S.state.config.user_name || "").trim();
  return (h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening") + (n ? `, ${n}` : "");
}
function setTitle(t){ S.chatTitle = t; $("#chatTitleText").textContent = t; $("#chatTitle").classList.toggle("hidden", !S.chatId) }
function setCrumb(name){ $("#crumb").classList.toggle("hidden", !name); $("#crumbName").textContent = name || "" }
function setHome(home){ $("#view-chat").classList.toggle("home", home); if(home){ $("#greet").textContent = greet(); renderSuggestions() } }
function newChat(projectId = null, projectName = ""){
  if(S.busy) api().stop();
  S.chatId = null; S.projectId = projectId; S.messages = []; S.cur = null;
  $("#msgs").innerHTML = ""; setTitle(""); setCrumb(null); setCtx(null); closeArtifact();
  $("#heroProj").classList.toggle("hidden", !projectId); $("#heroProj span").textContent = projectName;
  setHome(true); show("chat"); setBusy(false); loadChats();
}
$("#newChatBtn").onclick = () => newChat();
$("#heroProjX").onclick = () => { S.projectId = null; $("#heroProj").classList.add("hidden") };
$("#crumb").onclick = () => S.projectId && openProject(S.projectId);
$("#chatTitle").onclick = () => S.chatId && chatMenu($("#chatTitle"), S.chatId);

async function openChat(id){
  const data = await api().open_chat(id);
  if(!data){ toast("Chat not found"); return }
  if(S.busy) api().stop();
  S.chatId = id; S.projectId = data.chat.project_id; S.messages = data.messages; S.cur = null;
  S.attached = new Set((data.chat.plugins || []).filter(n => S.servers.some(s => s.name === n)));
  renderPlugChips(); setTitle(data.chat.title); setCrumb(data.project && data.project.name); setCtx(null);
  $("#heroProj").classList.add("hidden");
  setHome(false); show("chat"); closeArtifact();
  $("#msgs").innerHTML = "";
  data.messages.forEach(m => $("#msgs").appendChild(m.role === "user" ? userEl(m) : botEl(m)));
  markLast(); scrollDown(true); loadChats();
}

function userEl(m){
  const d = document.createElement("div"); d.className = "msg user"; d.dataset.id = m.id || "";
  const used = (m.plugins || []).length ? `<div class="used">${m.plugins.map(n => `<span class="chip">${tile(n,"sm")}${esc(plugMeta(n).title)}</span>`).join("")}</div>` : "";
  const atts = (m.attachments || []).length ? `<div class="atts">${m.attachments.map(a => fileCard(a, false)).join("")}</div>` : "";
  d.innerHTML = `${used}${atts}${m.text ? `<div class="bubble"></div>` : ""}<div class="acts"><button class="ib" data-act="edit" title="Edit">${ic("pen")}</button><button class="ib" data-act="copy" title="Copy">${ic("copy")}</button></div>`;
  if(m.text) d.querySelector(".bubble").textContent = m.text;
  d._m = m; return d;
}
// Tool results that came back as a table (see mcp_client._compact) are shown formatted, the rest as text.
function toolResultHtml(text, callId){
  return /^\|.*\|$/m.test(text || "") ? `<div class="md tres">${md(text, "tool-" + callId)}</div>` : `<pre>${esc(text)}</pre>`;
}
function toolCardHtml(t){
  const st = t.status === "done" ? "ok" : t.status === "running" ? "" : "bad";
  const label = t.status === "running" ? '<span class="spin"></span>' : esc(t.status === "error" ? "error" : t.status);
  const table = /^\|.*\|$/m.test(t.result || "");  // a result table stays open: it is the answer
  return `<details class="toolcard" data-call="${esc(t.call_id)}"${table ? " open" : ""}><summary><span>🔧</span><span class="tn">${esc(t.name)}</span><span class="ts ${st}">${label}</span></summary><pre>${esc(fmtArgs(t.args))}</pre>${t.result ? toolResultHtml(t.result, t.call_id) : ""}${/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(t.shot || "") ? `<img class="shot" src="${t.shot}" alt="Browser after this step">` : ""}</details>`;
}
function botEl(m, streaming = false){
  const d = document.createElement("div"); d.className = "msg bot"; d.dataset.id = m.id || "";
  const key = m.id || m.botId;
  d.innerHTML = `<details class="think${m.thinking ? "" : " hidden"}"><summary>${ic("bulb","width:15px;height:15px")}<span class="tl">Thought process</span></summary><div class="tb"></div></details>
    <div class="tools">${(m.tools || []).map(toolCardHtml).join("")}</div>
    <div class="md">${streaming ? "" : md(m.text || "", key)}</div>
    <div class="acts"><button class="ib" data-act="copy" title="Copy">${ic("copy")}</button><button class="ib" data-act="regen" title="Regenerate">${ic("redo")}</button><span class="stats">${esc(m.stats || "")}</span></div>`;
  if(m.thinking) d.querySelector(".tb").textContent = m.thinking;
  d._m = m; return d;
}
function markLast(){
  const all = $$("#msgs .msg"); all.forEach(e => e.classList.remove("last"));
  const lastBot = [...all].reverse().find(e => e.classList.contains("bot"));
  all.forEach(e => { const r = e.querySelector('[data-act="regen"]'); if(r) r.classList.toggle("hidden", e !== lastBot) });
  if(lastBot && lastBot === all[all.length - 1]) lastBot.classList.add("last");
}
function scrollDown(force){ const c = $("#chat"); if(force || c.scrollHeight - c.scrollTop - c.clientHeight < 160) c.scrollTop = c.scrollHeight }
function setBusy(v){
  S.busy = v; $("#sendBtn").classList.toggle("hidden", v); $("#stopBtn").classList.toggle("hidden", !v); updateSend();
}
function updateSend(){
  const has = $("#input").value.trim() || S.pending.some(p => p.id);
  $("#sendBtn").disabled = !S.ready || S.busy || !has || S.pending.some(p => p.uploading);
}
function setCtx(c){
  if(!c){ $("#ctx").classList.add("hidden"); return }
  const pct = Math.min(100, Math.round(c.used / c.max * 100));
  $("#ctx").classList.remove("hidden"); $("#ctxArc").setAttribute("stroke-dasharray", `${pct} 100`);
  $("#ctxArc").setAttribute("stroke", pct > 85 ? "var(--danger)" : pct > 65 ? "var(--warn)" : "var(--accent)");
  $("#ctxText").textContent = `${c.used.toLocaleString()} / ${c.max.toLocaleString()}`;
  $("#ctx").title = `Context window: ${pct}% used${pct > 80 ? " — start a new chat soon or raise the context size in Settings" : ""}`;
}

function opts(extra = {}){
  const live = [...S.attached].filter(n => S.servers.some(s => s.name === n && (s.status === "connected" || s.status === "connecting")));  // Python waits for connecting ones
  return Object.assign({plugins: live, thinking: S.thinking, style: S.style, bot_id: "b" + Date.now() + Math.random().toString(36).slice(2, 6)}, extra);
}
function startBot(botId){
  const el = botEl({botId}, true); el.classList.add("streaming"); $("#msgs").appendChild(el);
  el.querySelector(".md").innerHTML = `<span class="cursor"></span>`;
  S.cur = {id: botId, el, mdEl: el.querySelector(".md"), text: "", think: "", t0: Date.now(), dirty: false};
  setBusy(true); markLast(); scrollDown(true);
}
async function send(voiceAtt = null){  // voiceAtt: a recorded voice clip sent on its own (voice.js)
  const text = voiceAtt ? "" : $("#input").value.trim();
  if(!S.ready || S.busy) return false;
  if(!voiceAtt && ((!text && !S.pending.length) || S.pending.some(p => p.uploading))) return false;
  hidePop();
  const atts = voiceAtt ? [voiceAtt] : S.pending.filter(p => p.id); const o = opts();
  if(!voiceAtt){ $("#input").value = ""; autosize(); S.pending = []; renderPending() }
  setHome(false);
  const uel = userEl({text, attachments: atts, plugins: o.plugins}); $("#msgs").appendChild(uel);
  startBot(o.bot_id);
  const res = await api().send(Object.assign({chat_id: S.chatId, project_id: S.projectId, text, attachments: atts.map(a => a.id)}, o));
  if(!res.ok){ toast(res.error); setBusy(false); return false }
  uel.dataset.id = res.message.id; uel._m = res.message;
  if(!S.chatId){ S.chatId = res.chat_id; setTitle("New chat"); if(S.projectId){ const p = S.projects.find(x => x.id === S.projectId); setCrumb(p ? p.name : ""); } loadChats() }
  return true;
}
async function regenerate(){
  if(S.busy || !S.chatId) return;
  const last = [...$$("#msgs .msg.bot")].pop(); if(last) last.remove();
  const o = opts(); startBot(o.bot_id);
  const r = await api().regenerate(Object.assign({chat_id: S.chatId}, o));
  if(!r.ok){ toast(r.error); S.cur && S.cur.el.remove(); setBusy(false) }
}
function editMessage(el){
  if(S.busy) return toast("Wait for the reply to finish");
  const m = el._m; if(!m || !m.id) return;
  const box = document.createElement("div"); box.className = "editbox";
  box.innerHTML = `<textarea></textarea><div class="row" style="justify-content:flex-end;margin-top:6px"><button class="btn sm" data-x>Cancel</button><button class="btn sm primary" data-s>Send</button></div>`;
  const ta = box.querySelector("textarea"); ta.value = m.text || "";
  const bubble = el.querySelector(".bubble"), acts = el.querySelector(".acts");
  if(bubble) bubble.classList.add("hidden"); acts.classList.add("hidden"); el.appendChild(box);
  ta.focus(); ta.style.height = ta.scrollHeight + "px";
  box.querySelector("[data-x]").onclick = () => { box.remove(); if(bubble) bubble.classList.remove("hidden"); acts.classList.remove("hidden") };
  const go = async () => {
    const text = ta.value.trim(); if(!text && !(m.attachments||[]).length) return;
    let n = el.nextElementSibling; while(n){ const nx = n.nextElementSibling; n.remove(); n = nx }
    const o = opts(); const nel = userEl({text, attachments: m.attachments, plugins: o.plugins});
    el.replaceWith(nel); startBot(o.bot_id);
    const r = await api().edit_message(Object.assign({chat_id: S.chatId, message_id: m.id, text}, o));
    if(!r.ok){ toast(r.error); setBusy(false); return }
    nel.dataset.id = r.message.id; nel._m = r.message;
  };
  box.querySelector("[data-s]").onclick = go;
  ta.onkeydown = e => { if(e.key === "Enter" && !e.shiftKey){ e.preventDefault(); go() } if(e.key === "Escape") box.querySelector("[data-x]").click() };
}
$("#msgs").addEventListener("click", e => {
  const el = e.target.closest(".msg");
  const act = e.target.closest("[data-act]");
  if(act && el){
    const a = act.dataset.act;
    if(a === "copy"){ const t = el._m ? el._m.text : (S.cur && S.cur.el === el ? S.cur.text : ""); navigator.clipboard.writeText(t || el.innerText).then(() => toast("Copied")) }
    if(a === "edit") editMessage(el);
    if(a === "regen") regenerate();
    return;
  }
  const cp = e.target.closest("[data-copy]");
  if(cp){ const code = cp.closest(".cb").querySelector("pre").textContent; navigator.clipboard.writeText(code).then(() => { cp.textContent = "Copied"; setTimeout(() => cp.textContent = "Copy", 1200) }); return }
  const art = e.target.closest("[data-art]"); if(art){ openArtifact(art.dataset.art); return }
  const att = e.target.closest("[data-att]"); if(att && att.dataset.att){ viewAttachment(att.dataset.att); return }
  const a = e.target.closest("a[href]");
  if(a){ e.preventDefault(); const href = a.getAttribute("href"); if(/^https?:\/\//i.test(href)) openBrowser(href) }
});

/* streaming render (throttled) */
let rafPending = false;
// Voice replies start with <heard>what the user said</heard>; never show it in the reply.
function stripHeard(t){ return t.replace(/<heard>[\s\S]*?(<\/heard>\s*|$)/, "").replace(/\s*<(h(e(a(r(d)?)?)?)?)?$/, "") }
// Finished blocks (up to the last blank line outside a code fence) are rendered once and appended;
// only the unfinished tail is re-parsed each frame, so long replies don't get slower as they grow.
// The "done" event still renders the whole reply in one go, with syntax highlighting.
const RENDER_MIN_MS = 60;
let lastRender = 0;
function streamState(c){
  if(!c.st || !c.text.startsWith(c.st.stableText)){  // first frame, or earlier text changed (voice <heard> cut)
    c.mdEl.innerHTML = `<div class="md-st" style="display:contents"></div><div class="md-tail" style="display:contents"></div>`;
    c.st = {stableEl: c.mdEl.firstChild, tailEl: c.mdEl.lastChild, stableText: "", scan: 0, inFence: false, idx: -1};
  }
  return c.st;
}
function advanceStable(c, st){
  const t = c.text; let pos = st.scan, cut = -1, inFence = st.inFence, fenceAtCut = inFence;
  for(;;){
    const nl = t.indexOf("\n", pos); if(nl < 0) break;  // only complete lines
    const line = t.slice(pos, nl);
    if(/^ {0,3}(```|~~~)/.test(line)) inFence = !inFence;
    else if(!inFence && !line.trim() && nl + 1 > st.stableText.length){ cut = nl + 1; fenceAtCut = inFence }
    pos = nl + 1;
  }
  if(cut < 0){ return }  // keep scanning from the last finished block next time
  const chunk = t.slice(st.stableText.length, cut);
  if(chunk.trim()){
    st.stableEl.insertAdjacentHTML("beforeend", md(chunk, c.id, true, st.idx));
    st.idx = mdLastIdx;
  }
  st.stableText = t.slice(0, cut); st.scan = cut; st.inFence = fenceAtCut;
}
function scheduleRender(){
  if(rafPending) return; rafPending = true;
  requestAnimationFrame(function frame(){
    const c = S.cur; if(!c || !c.dirty){ rafPending = false; return }
    const now = performance.now();
    if(now - lastRender < RENDER_MIN_MS){ requestAnimationFrame(frame); return }
    rafPending = false; c.dirty = false; lastRender = now;
    const st = streamState(c);
    advanceStable(c, st);
    st.tailEl.innerHTML = md(c.text.slice(st.stableText.length), c.id, true, st.idx) + `<span class="cursor"></span>`;
    scrollDown();
  });
}

/* ============================ composer ============================ */
function autosize(){ const i = $("#input"); i.style.height = "auto"; i.style.height = Math.min(i.scrollHeight, 260) + "px"; updateSend() }
$("#input").addEventListener("input", () => {
  autosize();
  if(/(?:^|\s)@[\w-]*$/.test($("#input").value)){ atMode = true; popIdx = 0; showPop(atQuery()) } else if(atMode) hidePop();
});
$("#input").addEventListener("keydown", e => {
  if(atMode && !$("#pop").classList.contains("hidden") && popItems.length){
    if(e.key === "ArrowDown"){ e.preventDefault(); popIdx = (popIdx + 1) % popItems.length; showPop(atQuery()); return }
    if(e.key === "ArrowUp"){ e.preventDefault(); popIdx = (popIdx - 1 + popItems.length) % popItems.length; showPop(atQuery()); return }
    if(e.key === "Enter" || e.key === "Tab"){ e.preventDefault(); pick(popItems[Math.max(0, popIdx)].name); return }
  }
  if(e.key === "Enter" && !e.shiftKey && !e.isComposing){ e.preventDefault(); send() }
  if(e.key === "ArrowUp" && !$("#input").value && !S.busy){ const last = [...$$("#msgs .msg.user")].pop(); if(last){ e.preventDefault(); editMessage(last) } }
});
$("#sendBtn").onclick = () => send();
$("#stopBtn").onclick = () => api().stop();

/* attachments */
function renderPending(){
  $("#cAtts").innerHTML = S.pending.map(a => fileCard(a, true)).join("");
  $$("#cAtts [data-rm]").forEach(b => b.onclick = e => { e.stopPropagation(); S.pending = S.pending.filter(p => (p.tmp || p.id) !== b.dataset.rm); renderPending() });
  $$("#cAtts [data-att]").forEach(c => c.onclick = () => c.dataset.att && viewAttachment(c.dataset.att));
  updateSend();
}
$("#attachBtn").onclick = () => chooseFiles(files => { uploadFiles(files); $("#input").focus() });
// Large photos are scaled to 1536px here, so the app needs no image library.
async function imageDataURL(file){
  const url = await readAsDataURL(file);
  if(!/^image\/(png|jpeg|webp|bmp)$/.test(file.type)) return {url, name: file.name};
  const img = new Image(); img.src = url; await img.decode().catch(() => {});
  const k = Math.min(1, 1536 / Math.max(img.naturalWidth || 1, img.naturalHeight || 1));
  if(k === 1 && file.size < 2 * 1024 * 1024) return {url, name: file.name};
  const c = document.createElement("canvas"); c.width = Math.round(img.naturalWidth * k); c.height = Math.round(img.naturalHeight * k);
  c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
  return {url: c.toDataURL("image/jpeg", 0.9), name: (file.name || "image").replace(/\.[^.]*$/, "") + ".jpg"};
}
function readAsDataURL(file){ return new Promise((ok, no) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = no; r.readAsDataURL(file) }) }
async function uploadFiles(files){
  for(const f of files){
    if(f.size > 30*1024*1024){ toast(`${f.name} is larger than 30 MB`); continue }
    const tmp = "u" + Math.random().toString(36).slice(2);
    const item = {tmp, name: f.name || "pasted.png", kind: (f.type||"").startsWith("image/") ? "image" : "", uploading: true};
    S.pending.push(item); renderPending();
    try{
      const im = item.kind === "image" ? await imageDataURL(f) : {url: await readAsDataURL(f), name: item.name};
      const meta = await api().upload_file(im.name || item.name, im.url);
      const i = S.pending.findIndex(p => p.tmp === tmp);
      if(meta.error){ toast(meta.error, 5000); if(i >= 0) S.pending.splice(i, 1) } else if(i >= 0) S.pending[i] = meta;
    }catch(e){ toast("Upload failed: " + e); S.pending = S.pending.filter(p => p.tmp !== tmp) }
    renderPending();
  }
}
$("#input").addEventListener("paste", e => {
  const files = [...(e.clipboardData && e.clipboardData.files || [])];
  if(files.length){ e.preventDefault(); uploadFiles(files) }
});
let dragN = 0;
document.addEventListener("dragenter", e => { if(![...e.dataTransfer.types].includes("Files")) return; dragN++; if(!$(".dropzone")){ const z = document.createElement("div"); z.className = "dropzone"; z.textContent = currentView === "project" ? "Drop to add to project knowledge" : "Drop files to attach"; $("main").appendChild(z) } });
document.addEventListener("dragleave", () => { dragN = Math.max(0, dragN - 1); if(!dragN){ const z = $(".dropzone"); z && z.remove() } });
document.addEventListener("dragover", e => e.preventDefault());
document.addEventListener("drop", e => {
  e.preventDefault(); dragN = 0; const z = $(".dropzone"); z && z.remove();
  const files = [...(e.dataTransfer.files || [])]; if(!files.length) return;
  if(currentView === "project" && PD.id) return uploadKnowledge(files);
  if(currentView !== "chat") show("chat");
  uploadFiles(files);
});
// Stored images come as "att/<id>" (served and cached by the app); older data: URLs still work.
function attSrc(u){ return /^att\/[0-9a-f]+$/.test(u || "") ? `${u}?t=${encodeURIComponent(TOKEN)}` : u }
async function viewAttachment(id){
  const a = await api().attachment_text(id); if(!a || !a.name) return;
  $("#vwTitle").textContent = a.name;
  $("#vwBody").innerHTML = a.image ? `<img src="${esc(attSrc(a.image))}">` : `<pre></pre>`;
  if(!a.image) $("#vwBody pre").textContent = a.text || "(empty)";
  openModal("mViewer");
}

/* style & thinking */
$("#styleBtn").onclick = () => showMenu($("#styleBtn"), [{label: "Response style"},
  ...((S.state && S.state.styles) || []).map(s => ({label: s.label, check: s.id === S.style, fn: () => setStyle(s.id)}))]);
function setStyle(id){ S.style = id; const s = ((S.state && S.state.styles) || []).find(x => x.id === id); $("#styleName").textContent = s ? s.label : "Normal"; $("#styleBtn").classList.toggle("on", id !== "normal"); api().save_settings({style: id}) }
$("#thinkBtn").onclick = () => { S.thinking = !S.thinking; $("#thinkBtn").classList.toggle("on", S.thinking); api().save_settings({thinking: S.thinking}); toast(S.thinking ? "Extended thinking on — slower, more careful answers" : "Extended thinking off") };

/* suggestions */
const SUGG = [
  ["Write", "Write a short, friendly email to a customer announcing our new course feature"],
  ["Learn", "Explain how vector databases work, simply, with an example"],
  ["Code", "Create a responsive pricing page in HTML with three plans"],
  ["Analyze", "Summarize the attached file and list the key action items"],
  ["Plan", "Plan a 5-day content calendar for an LMS product on Instagram"],
];
function renderSuggestions(){
  $("#suggRow").innerHTML = SUGG.map(([t]) => `<button class="sugg" data-s="${esc(t)}">${esc(t)}</button>`).join("");
  $$("#suggRow .sugg").forEach(b => b.onclick = () => { const s = SUGG.find(x => x[0] === b.dataset.s); $("#input").value = s[1]; autosize(); $("#input").focus() });
}

/* plugin chips & picker */
function renderPlugChips(){
  lsSet("attachedPlugins", [...S.attached]);
  $("#cPlugs").innerHTML = [...S.attached].map(n => { const s = S.servers.find(x => x.name === n); const off = !s || s.status !== "connected";
    return `<span class="chip${off ? " off" : ""}" title="${off ? "Not connected" : ""}">${tile(n,"sm")}${esc(plugMeta(n).title)}<button class="x" data-n="${esc(n)}">×</button></span>` }).join("");
  $$("#cPlugs .x").forEach(b => b.onclick = () => { S.attached.delete(b.dataset.n); renderPlugChips() });
  $("#plusBtn").classList.toggle("on", S.attached.size > 0);
}
let popItems = [], popIdx = 0, atMode = false;
const atQuery = () => (($("#input").value.match(/(?:^|\s)@([\w-]*)$/) || [])[1] || "");
function showPop(filter = ""){
  const f = filter.toLowerCase();
  popItems = S.servers.filter(s => !f || (s.title || s.name).toLowerCase().includes(f) || s.name.includes(f));
  let h = `<div class="ph">Plugins for this chat</div>`;
  if(!S.servers.length) h += `<div class="none">No plugins installed yet.</div>`;
  else if(!popItems.length) h += `<div class="none">No match for “${esc(filter)}”.</div>`;
  h += popItems.map((s, i) => { const on = S.attached.has(s.name); const st = s.status === "connected" ? `${s.tools.length} tools` : s.status;
    return `<button class="it${i === popIdx ? " hl" : ""}" data-n="${esc(s.name)}">${tile(s.name)}<span class="nm">${esc(s.title || s.name)}<small>${esc(st)}</small></span><span class="ck${on ? " on" : ""}">${on ? "✓" : ""}</span></button>` }).join("");
  h += `<div class="sep"></div><button class="it" id="popBrowse"><span class="tile" style="background:var(--panel-2);color:var(--text)">+</span><span class="nm">Browse plugins<small>Install from the catalog</small></span></button>`;
  $("#pop").innerHTML = h; $("#pop").classList.remove("hidden");
  $$("#pop .it[data-n]").forEach(b => b.onmousedown = e => { e.preventDefault(); pick(b.dataset.n) });
  $("#popBrowse").onmousedown = e => { e.preventDefault(); hidePop(); show("plugins") };
}
function hidePop(){ $("#pop").classList.add("hidden"); atMode = false; popIdx = 0 }
function pick(name){
  const s = S.servers.find(x => x.name === name);
  if(S.attached.has(name)) S.attached.delete(name);
  else { if(s && s.status !== "connected") toast(`${s.title || name} isn't connected — check Plugins`); S.attached.add(name) }
  if(atMode){ const i = $("#input"); i.value = i.value.replace(/(^|\s)@[\w-]*$/, "$1"); hidePop() } else showPop();
  renderPlugChips(); $("#input").focus();
}
$("#plusBtn").onclick = () => { if(!$("#pop").classList.contains("hidden") && !atMode) return hidePop(); atMode = false; popIdx = -1; showPop() };
document.addEventListener("mousedown", e => { if(!e.target.closest("#pop") && !e.target.closest("#plusBtn")) hidePop() });

/* tool cards (live) */
function fmtArgs(a){ try{ return JSON.stringify(typeof a === "string" ? JSON.parse(a) : a, null, 2) }catch(e){ return String(a) } }
function addToolCard(ev){
  if(!S.cur || S.cur.id !== ev.id) return;
  const host = S.cur.el.querySelector(".tools");
  const w = document.createElement("div"); w.innerHTML = toolCardHtml({call_id: ev.call_id, name: ev.name, args: ev.args, status: "running"});
  const d = w.firstElementChild; if(ev.ask) d.open = true;
  if(ev.ask){
    const a = document.createElement("div"); a.className = "ask";
    a.innerHTML = `<span>Allow <b>${esc(ev.name)}</b>?</span><button class="btn sm">Deny</button><button class="btn sm primary">Allow</button>`;
    const [deny, allow] = a.querySelectorAll("button"); const decide = v => { api().tool_decision(ev.call_id, v); a.remove() };
    deny.onclick = () => decide(false); allow.onclick = () => decide(true); d.appendChild(a);
  }
  host.appendChild(d); scrollDown();
}
function fillToolCard(ev){
  const d = document.querySelector(`.toolcard[data-call="${CSS.escape(ev.call_id || "")}"]`); if(!d) return;
  const st = d.querySelector(".ts"); st.className = "ts " + (ev.denied || ev.error ? "bad" : "ok"); st.textContent = ev.denied ? "denied" : ev.error ? "error" : "done";
  const a = d.querySelector(".ask"); if(a) a.remove();
  d.insertAdjacentHTML("beforeend", toolResultHtml(ev.result, ev.call_id)); d.open = /^\|.*\|$/m.test(ev.result || ""); scrollDown();
}

/* ============================ artifacts ============================ */
let artKey = null, artTab = "preview";
function openArtifact(key){
  const a = ARTS[key]; if(!a) return; artKey = key;
  if(typeof closeBrowser === "function" && WB.open) closeBrowser();
  $("#artPanel").classList.remove("hidden"); $("#artTitle").textContent = a.title;
  const doc = a.lang === "svg" ? `<!doctype html><html><body style="margin:0;display:grid;place-items:center;min-height:100vh;background:#fff">${a.code}</body></html>` : a.code;
  $("#artFrame").srcdoc = doc;
  const code = $("#artCode code"); try{ code.innerHTML = hljs.highlight(a.code, {language: "xml"}).value }catch(e){ code.textContent = a.code }
  setArtTab(artTab);
}
function setArtTab(t){ artTab = t; $$("#artSeg button").forEach(b => b.classList.toggle("on", b.dataset.t === t)); $("#artFrame").classList.toggle("hidden", t !== "preview"); $("#artCode").classList.toggle("hidden", t !== "code") }
$$("#artSeg button").forEach(b => b.onclick = () => setArtTab(b.dataset.t));
function closeArtifact(){ $("#artPanel").classList.add("hidden"); $("#artFrame").srcdoc = ""; artKey = null }
$("#artClose").onclick = closeArtifact;
$("#artCopy").onclick = () => artKey && navigator.clipboard.writeText(ARTS[artKey].code).then(() => toast("Copied"));
$("#artSave").onclick = async () => { if(!artKey) return; const a = ARTS[artKey]; const name = a.title.replace(/[^\w\- ]+/g, "").trim().replace(/\s+/g, "-").toLowerCase() || "artifact"; downloadText(`${name}.${a.lang === "svg" ? "svg" : "html"}`, a.code, a.lang === "svg" ? "image/svg+xml" : "text/html") };

/* ============================ projects ============================ */
async function loadProjects(){
  S.projects = await api().list_projects();
  $("#pgrid").innerHTML = S.projects.length ? S.projects.map(p => `<div class="pcard" data-id="${p.id}"><b>${esc(p.name)}</b><p>${esc(p.description || "No description")}</p><small>${p.chat_count} chats · ${p.file_count} files · updated ${new Date(p.updated*1000).toLocaleDateString()}</small></div>`).join("")
    : `<div class="card" style="text-align:center;padding:40px"><b>No projects yet</b><p class="sub">Create one to give a group of chats shared instructions and documents.</p></div>`;
  $$("#pgrid .pcard").forEach(c => c.onclick = () => openProject(c.dataset.id));
}
$("#newProjBtn").onclick = async () => {
  const v = await askForm({title: "New project", fields: [{key: "name", label: "Name", placeholder: "e.g. ekhool Latin America launch"}, {key: "desc", label: "What is it about? (optional)", type: "textarea", h: 80}], ok: "Create"});
  if(!v || !v.name.trim()) return;
  const p = await api().create_project(v.name, v.desc); openProject(p.id);
};
const PD = {id: null, data: null};
async function openProject(id){
  const p = await api().get_project(id); if(!p) return;
  PD.id = id; PD.data = p; show("project");
  $("#pdName").textContent = p.name; $("#pdDesc").textContent = p.description || "Add a description…";
  $("#pdInstr").value = p.instructions || "";
  $("#pdChatCount").textContent = p.chats.length ? `· ${p.chats.length}` : "";
  $("#pdChats").innerHTML = p.chats.length ? p.chats.map(chatItem).join("") : `<div class="side-empty" style="padding:16px">No chats yet.</div>`;
  bindChatItems($("#pdChats"));
  const total = p.files.reduce((s, f) => s + (f.chars || 0), 0), budget = ((S.state && S.state.config.max_tokens) || 8192) * 0.35 * 3.5;
  const pct = Math.min(100, Math.round(total / budget * 100));
  $("#pdMeter").style.width = pct + "%"; $("#pdMeter").style.background = pct >= 100 ? "var(--warn)" : "var(--accent)";
  $("#pdMeterTxt").textContent = p.files.length ? `${pct}% of knowledge capacity${pct >= 100 ? " — some text will be left out; raise the context window in Settings" : ""}` : "Add documents Gemma should know in every chat of this project.";
  $("#pdFiles").innerHTML = p.files.map(f => `<div class="kf"><span class="fi">${esc(extOf(f.name))}</span><span class="n"><b>${esc(f.name)}</b><small>${fmtBytes(f.size)} · ~${Math.round((f.chars||0)/3.5).toLocaleString()} tokens</small></span><button class="ib" data-rmf="${f.id}" title="Remove">${ic("trash","width:15px;height:15px")}</button></div>`).join("");
  $$("#pdFiles [data-rmf]").forEach(b => b.onclick = async () => { await api().remove_project_file(b.dataset.rmf); openProject(id) });
}
$("#backProjects").onclick = () => show("projects");
$("#pdName").onblur = async () => { const n = $("#pdName").textContent.trim(); if(n && PD.data && n !== PD.data.name){ await api().update_project(PD.id, {name: n}); PD.data.name = n; loadChats() } };
$("#pdDesc").onblur = async () => { const d = $("#pdDesc").textContent.trim(); if(PD.data && d !== (PD.data.description || "") && d !== "Add a description…") await api().update_project(PD.id, {description: d}) };
$("#pdName").onkeydown = e => { if(e.key === "Enter"){ e.preventDefault(); e.target.blur() } };
$("#pdInstrSave").onclick = async () => { await api().update_project(PD.id, {instructions: $("#pdInstr").value}); toast("Instructions saved") };
$("#pdNewChat").onclick = () => newChat(PD.id, PD.data.name);
$("#pdAddFile").onclick = () => showMenu($("#pdAddFile"), [
  {label: "Upload files…", icon: "file", fn: () => chooseFiles(uploadKnowledge)},
  {label: "Add text content", icon: "pen", fn: async () => { const v = await askForm({title: "Add text to knowledge", fields: [{key: "name", label: "Title", placeholder: "e.g. Pricing notes"}, {key: "text", label: "Content", type: "textarea", h: 220}], ok: "Add"}); if(v && v.text.trim()){ await api().add_project_text(PD.id, v.name || "notes.txt", v.text); openProject(PD.id) } }},
]);
async function uploadKnowledge(files){
  for(const f of files){ const r = await api().upload_project_file(PD.id, f.name, await readAsDataURL(f)); if(!r.ok) toast(r.error, 5000) }
  openProject(PD.id);
}
$("#pdMenu").onclick = () => showMenu($("#pdMenu"), [{label: "Delete project", icon: "trash", danger: true, fn: async () => {
  const v = await askForm({title: "Delete project?", body: "The project, its instructions and knowledge are deleted. Its chats are kept.", ok: "Delete", danger: true});
  if(v){ await api().delete_project(PD.id); show("projects"); loadChats() } }}]);

/* ============================ plugins page ============================ */
function renderPlugins(){
  const q = $("#plugSearch").value.trim().toLowerCase();
  const match = (t, d) => !q || t.toLowerCase().includes(q) || (d || "").toLowerCase().includes(q);
  const inst = S.servers.filter(s => match(s.title || s.name, ""));
  $("#instCount").textContent = S.servers.length ? `· ${S.servers.filter(s => s.status === "connected").length}/${S.servers.length} connected` : "";
  $("#installed").innerHTML = inst.length ? inst.map(s => { const dot = s.status === "connected" ? "ready" : s.status === "connecting" ? "loading" : s.status === "error" ? "err" : "";
      return `<button class="inst" data-n="${esc(s.name)}" title="${esc(s.title || s.name)} · ${esc(s.status)}">${tile(s.name,"lg")}<span class="dot ${dot}"></span><span>${esc(s.title || s.name)}</span></button>` }).join("")
    : `<div class="empty-inst">${S.servers.length ? "No installed plugin matches." : "Nothing installed yet — pick one below."}</div>`;
  $$("#installed .inst").forEach(b => b.onclick = () => openDetail(b.dataset.n));
  const installedIds = new Set(S.servers.map(s => s.plugin || s.name));
  const cats = [...new Set(S.catalog.map(c => c.category))];
  $("#catalog").innerHTML = cats.map(cat => {
    const items = S.catalog.filter(c => c.category === cat && match(c.title, c.description)); if(!items.length) return "";
    return `<div class="sech">${esc(cat)}</div><div class="cat">${items.map(c => { const done = installedIds.has(c.id);
      return `<div class="pl" data-id="${c.id}">${tile(c.id)}<div class="nm"><b>${esc(c.title)}</b><small>${esc(c.description)}</small></div><button class="act${done ? " ok" : ""}">${done ? "✓" : "+"}</button></div>` }).join("")}</div>`;
  }).join("");
  $$("#catalog .pl").forEach(p => p.onclick = () => { const id = p.dataset.id; if(installedIds.has(id)) openDetail(S.servers.find(s => (s.plugin || s.name) === id).name); else openInstall(id) });
  $("#plugBadge").textContent = S.servers.length || "";
}
$("#plugSearch").addEventListener("input", renderPlugins);
let installing = null;
function openInstall(id){
  const c = S.catalog.find(x => x.id === id); installing = id;
  $("#miTile").innerHTML = tile(id, "lg"); $("#miTitle").textContent = c.title; $("#miDesc").textContent = c.description;
  $("#miFields").innerHTML = c.fields.map(f => `<label class="f" for="mi_${f.key}">${esc(f.label)}${f.optional ? " (optional)" : ""}</label>` + (f.type === "select"
    ? `<select class="inp" id="mi_${f.key}">${f.options.map(([v, l]) => `<option value="${esc(v)}" ${v === f.default ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`
    : `<input class="inp" id="mi_${f.key}" type="${f.type === "secret" ? "password" : "text"}" value="${esc(f.default || "")}" autocomplete="off">`)).join("") || `<div class="sub" style="margin-top:12px">No setup needed.</div>`;
  $("#miNeeds").textContent = `Runs on your computer · needs ${c.needs}. Settings are saved in ~/.config/ek-light-llm/mcp_servers.json.`;
  $("#miErr").textContent = ""; $("#miGo").disabled = false; $("#miGo").textContent = "Install";
  openModal("mInstall"); const first = $("#miFields input"); if(first) first.focus();
}
$("#miGo").onclick = async () => {
  const c = S.catalog.find(x => x.id === installing); const inputs = {};
  c.fields.forEach(f => inputs[f.key] = $("#mi_" + f.key).value);
  const r = await api().install_plugin(installing, inputs);
  if(!r.ok){ $("#miErr").textContent = r.error; return }
  $("#miErr").textContent = ""; $("#miGo").disabled = true; $("#miGo").innerHTML = `<span class="spin"></span> Connecting…`;
  waitFor(r.name, "#miErr", "#miGo", () => { closeModals(); S.attached.add(r.name); renderPlugChips(); toast(`${c.title} installed and attached`) });
};
const waiters = {};
function waitFor(name, errSel, btnSel, onOk){ waiters[name] = {errSel, btnSel, onOk, seen: false} }
function checkWaiters(){
  for(const [n, w] of Object.entries(waiters)){ const s = S.servers.find(x => x.name === n); if(!s) continue;
    if(s.status === "connecting"){ w.seen = true; $(w.errSel).textContent = s.error || ""; continue }
    if(s.status === "connected"){ delete waiters[n]; w.onOk() }
    else if(s.status === "error" && w.seen){ delete waiters[n]; $(w.errSel).textContent = "Couldn't connect: " + s.error; const b = $(w.btnSel); b.disabled = false; b.textContent = "Retry" }
  }
}
let detailName = null;
function openDetail(name){
  detailName = name; const s = S.servers.find(x => x.name === name); if(!s) return;
  const dis = new Set((S.state && S.state.config.disabled_tools) || []);
  $("#mdTile").innerHTML = tile(name, "lg"); $("#mdTitle").textContent = s.title || name;
  const dot = s.status === "connected" ? "ready" : s.status === "connecting" ? "loading" : "err";
  $("#mdStatus").innerHTML = `<span class="dot ${dot}"></span>${esc(s.status)}${s.remote ? " · remote" : " · local"}`;
  $("#mdErr").textContent = s.error || "";
  $("#mdTools").innerHTML = s.tools.length ? s.tools.map(t => { const k = `${name}/${t.name}`; return `<label><input type="checkbox" data-key="${esc(k)}" ${dis.has(k) ? "" : "checked"}><span>${esc(t.name)}<small>${esc(t.description || "")}</small></span></label>` }).join("") : `<div class="sub" style="padding:12px">No tools (not connected).</div>`;
  $$("#mdTools input").forEach(i => i.onchange = async () => { await api().set_tool_enabled(i.dataset.key, i.checked); S.state = await api().get_state() });
  $("#mdUse").textContent = S.attached.has(name) ? "Attached ✓" : "Use in chat";
  $("#mdNotes").value = ((S.state && S.state.config.plugin_notes) || {})[name] || "";
  openModal("mDetail");
}
$("#mdNotes").onchange = async () => {
  const notes = Object.assign({}, (S.state && S.state.config.plugin_notes) || {});
  const v = $("#mdNotes").value.trim(); if(v) notes[detailName] = v; else delete notes[detailName];
  S.state = await api().save_settings({plugin_notes: notes}); toast("Notes saved");
};
$("#mdUse").onclick = () => { S.attached.add(detailName); renderPlugChips(); closeModals(); show("chat") };
$("#mdReconnect").onclick = () => { api().reconnect_plugin(detailName); $("#mdStatus").innerHTML = `<span class="spin"></span> reconnecting…`; $("#mdErr").textContent = "" };
$("#mdRemove").onclick = async () => { const n = detailName; await api().remove_plugin(n); S.attached.delete(n); renderPlugChips(); closeModals(); toast("Plugin removed") };
let customKind = "url";
$("#addCustomBtn").onclick = () => { $("#mcErr").textContent = ""; $("#mcGo").disabled = false; $("#mcGo").textContent = "Add plugin"; openModal("mCustom"); $("#mcName").focus() };
$$("#mcKind button").forEach(b => b.onclick = () => { customKind = b.dataset.k; $$("#mcKind button").forEach(x => x.classList.toggle("on", x === b));
  const u = customKind === "url";
  $("#mcValueLbl").textContent = u ? "Server URL" : "Command"; $("#mcValue").placeholder = u ? "https://example.com/mcp" : "npx -y @scope/some-mcp-server --flag";
  $("#mcExtraLbl").textContent = u ? "Headers (optional, one per line)" : "Environment variables (optional, KEY=VALUE per line)";
  $("#mcExtra").placeholder = u ? "Authorization: Bearer YOUR_TOKEN" : "API_KEY=xxxx";
});
$("#mcGo").onclick = async () => {
  const r = await api().install_custom($("#mcName").value, customKind, $("#mcValue").value, $("#mcExtra").value);
  if(!r.ok){ $("#mcErr").textContent = r.error; return }
  $("#mcErr").textContent = ""; $("#mcGo").disabled = true; $("#mcGo").innerHTML = `<span class="spin"></span> Connecting…`;
  waitFor(r.name, "#mcErr", "#mcGo", () => { closeModals(); S.attached.add(r.name); renderPlugChips(); toast("Plugin added and attached") });
};
$("#mcpEditBtn").onclick = async () => { $("#mcpText").value = await api().mcp_get_config(); $("#mcpErr").textContent = ""; openModal("mJson") };
$("#mcpSave").onclick = async () => {
  try{ JSON.parse($("#mcpText").value) }catch(e){ $("#mcpErr").textContent = "Invalid JSON: " + e.message; return }
  const r = await api().mcp_save_config($("#mcpText").value);
  if(!r.ok){ $("#mcpErr").textContent = r.error; return }
  closeModals(); toast("Saved · reconnecting plugins…");
};

/* ============================ search ============================ */
let searchT = null;
function openSearch(){ openModal("mSearch"); $("#searchInput").value = ""; runSearch(); setTimeout(() => $("#searchInput").focus(), 0) }
async function runSearch(){
  const q = $("#searchInput").value.trim(); const res = await api().list_chats(q, null);
  $("#searchRes").innerHTML = res.length ? res.slice(0, 60).map(chatItem).join("") : `<div class="side-empty" style="padding:18px">No chats found.</div>`;
  bindChatItems($("#searchRes"));
}
$("#navSearch").onclick = openSearch;
$("#searchInput").addEventListener("input", () => { clearTimeout(searchT); searchT = setTimeout(runSearch, 150) });
$("#searchInput").addEventListener("keydown", e => { if(e.key === "Enter"){ const f = $("#searchRes .ci"); if(f){ closeModals(); openChat(f.dataset.id) } } });

/* ============================ settings ============================ */
const FIELDS = ["user_name","voice_language","system_prompt","backend","threads","max_tokens","vision","temperature","top_p","top_k","tool_approval","confirm_destructive"];
const RELOAD = ["backend","threads","max_tokens","vision"];
const SHORTCUTS = [["New chat","Ctrl+N"],["Search chats","Ctrl+K"],["Toggle sidebar","Ctrl+B"],["Settings","Ctrl+,"],["Stop generating","Esc"],["Edit last message","↑ (empty input)"],["Attach plugin","@"],["New line","Shift+Enter"]];
$("#scList").innerHTML = SHORTCUTS.map(([a, k]) => `<div class="sc-row"><span>${a}</span><span class="kbd">${k}</span></div>`).join("");
async function refreshState(){
  S.state = await api().get_state(); const c = S.state.config, st = S.state;
  $("#modelPath").textContent = st.model_exists ? `${c.model_path} · ${fmtBytes(st.model_size)}` : c.model_path;
  $("#dlBox").classList.toggle("hidden", st.model_exists);
  $("#loadBtn").disabled = !st.model_exists;
  if(!st.model_exists && !st.loaded && !st.downloading) setStatus("err", "Model not downloaded");
  if(st.downloading){ $("#dlBtn").disabled = true; $("#dlBtn").textContent = "Downloading…" }
  return S.state;
}
function fillSettings(){
  if(!S.state) return; const c = S.state.config;
  FIELDS.forEach(k => { const el = $("#" + k); if(!el) return; if(el.type === "checkbox") el.checked = c[k] !== false; else el.value = (k === "threads" && !c[k]) ? "" : (c[k] ?? "") });
}
function collect(){
  const o = {}; FIELDS.forEach(k => { const el = $("#" + k); if(el.type === "checkbox"){ o[k] = el.checked; return } let v = el.value;
    if(["threads","max_tokens","top_k"].includes(k)) v = parseInt(v || "0", 10); else if(["temperature","top_p"].includes(k)) v = parseFloat(v); o[k] = v }); return o;
}
async function applySettings(){
  const before = S.state.config, next = collect();
  const reload = RELOAD.some(k => String(before[k] ?? "") !== String(next[k] ?? "")) || !S.state.loaded;
  await api().save_settings(next); await refreshState();
  if(reload) api().load_model();
  toast(reload ? "Saved · loading model" : "Settings saved");
}
$("#applyBtn").onclick = applySettings;
$("#loadBtn").onclick = applySettings;
$("#pickBtn").onclick = async () => {
  const v = await askForm({title: "Use a model file", fields: [{key: "path", label: "Full path to a .litertlm file", value: S.state && S.state.config.model_path || "", placeholder: "/home/me/models/gemma-4-E2B-it.litertlm"}], ok: "Use this file"});
  if(!v) return;
  const r = await api().set_model_path(v.path); if(!r.ok){ toast(r.error, 5000); return }
  await refreshState(); fillSettings(); api().load_model();
};
$("#dlBtn").onclick = () => { $("#dlBtn").disabled = true; $("#dlBtn").textContent = "Downloading…"; $("#dlBar").classList.remove("hidden"); api().download_model() };
$("#mstat").onclick = () => show("settings");

/* theme */
function effTheme(){ return document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark") }
function syncHl(){ const l = effTheme() === "light"; $("#hlLight").disabled = !l; $("#hlDark").disabled = l }
$("#themeBtn").onclick = () => { document.documentElement.dataset.theme = effTheme() === "light" ? "dark" : "light"; lsSet("theme", document.documentElement.dataset.theme); syncHl() };
{ const t = lsGet("theme", null); if(t) document.documentElement.dataset.theme = t; syncHl() }

/* ============================ status ============================ */
function setStatus(kind, text){ $("#dot").className = "dot " + (kind || ""); $("#statusText").textContent = text }

/* ============================ events from python ============================ */
window.__ev = ev => {
  switch(ev.type){
    case "status":
      if(ev.state === "loading"){ setStatus("loading", ev.message || "Loading…"); S.ready = false; updateSend() }
      else if(ev.state === "ready"){ S.ready = true; setStatus("ready", ev.sleeping ? "Gemma 4 E2B · resting, wakes on send" : `Gemma 4 E2B · ${(ev.backend || "").toUpperCase()}${ev.vision ? " · vision" : ""}`); if(ev.message && !/^Loaded in/.test(ev.message)) toast(ev.message); updateSend() }
      else if(ev.state === "no_model"){ setStatus("err", "Model file missing"); refreshState() }
      break;
    case "notice": toast(ev.message, 5000); break;
    case "error": setStatus("err", "Error — see message"); toast(ev.message, 8000); break;
    case "mcp":
      S.servers = ev.servers || []; renderPlugChips(); checkWaiters();
      if(currentView === "plugins") renderPlugins(); else $("#plugBadge").textContent = S.servers.length || "";
      if(!$("#pop").classList.contains("hidden")) showPop(atMode ? atQuery() : "");
      if(detailName && !$("#mDetail").classList.contains("hidden")) openDetail(detailName);
      break;
    case "tool_call": addToolCard(ev); break;
    case "tool_result": fillToolCard(ev); break;
    case "thinking":
      if(S.cur && S.cur.id === ev.id){ S.cur.think += ev.text; const t = S.cur.el.querySelector(".think"); t.classList.remove("hidden");
        t.querySelector(".tl").innerHTML = `<span class="spin"></span> Thinking…`; t.querySelector(".tb").textContent = S.cur.think; scrollDown() }
      break;
    case "token":
      if(S.cur && S.cur.id === ev.id){ S.cur.raw = (S.cur.raw || "") + ev.text; S.cur.text = stripHeard(S.cur.raw); S.cur.dirty = true; scheduleRender() }
      break;
    case "done": {
      const c = S.cur;
      if(c && c.id === ev.id){
        const m = ev.message || {text: c.text, thinking: c.think, stats: ev.stats};
        if(ev.error) m.text = (c.text ? c.text + "\n\n" : "") + "⚠️ " + ev.error;
        const t = c.el.querySelector(".think");
        if(c.think){ t.querySelector(".tl").textContent = `Thought for ${Math.max(1, Math.round((Date.now() - c.t0) / 1000))}s` }
        c.el.dataset.id = m.id || ""; c.el._m = m; c.el.classList.remove("streaming");
        c.mdEl.innerHTML = md(m.text || "", m.id || c.id) || (ev.error ? "" : `<p class="sub">(no response)</p>`);
        if(ev.error) c.el.classList.add("err");
        c.el.querySelector(".stats").textContent = (ev.stopped ? "stopped · " : "") + (ev.stats || "");
        const newArts = Object.keys(ARTS).filter(k => k.startsWith((m.id || c.id) + "#"));
        if(newArts.length && !ev.stopped && currentView === "chat" && S.chatId === ev.chat_id) openArtifact(newArts[newArts.length - 1]);
        S.cur = null;
      }
      if(ev.context) setCtx(ev.context);
      setBusy(false); markLast(); loadChats(); $("#input").focus();
      break;
    }
    case "heard": {  // a voice message's transcript becomes the user's bubble text
      const u = [...$$("#msgs .msg.user")].pop();
      if(u){ const n = userEl(Object.assign({}, u._m || {}, {id: ev.message_id, text: ev.text})); u.replaceWith(n) }
      break;
    }
    case "chat_updated": if(ev.chat_id === S.chatId) setTitle(ev.title); loadChats(); break;
    case "download":
      if(ev.error){ toast("Download failed: " + ev.error, 8000); $("#dlBtn").disabled = false; $("#dlBtn").textContent = "Retry download (resumes)"; return }
      { const pct = ev.total ? (ev.done / ev.total * 100) : 0;
        $("#dlBar").classList.remove("hidden"); $("#dlBar i").style.width = pct.toFixed(1) + "%";
        $("#dlText").textContent = ev.finished ? "Download complete" : `${fmtBytes(ev.done)} / ${fmtBytes(ev.total)} · ${fmtBytes(ev.speed || 0)}/s`;
        setStatus("loading", `Downloading model ${pct.toFixed(0)}%`);
        if(ev.finished) refreshState().then(() => api().load_model()) }
      break;
  }
};

/* ============================ shortcuts ============================ */
document.addEventListener("keydown", e => {
  const mod = e.ctrlKey || e.metaKey;
  if(e.key === "Escape"){
    if(!$("#mSearch").classList.contains("hidden") || $$(".modal:not(.hidden)").length){ closeModals(); return }
    if($("#ctxMenu")){ closeMenu(); return }
    if(!$("#pop").classList.contains("hidden")){ hidePop(); return }
    if(S.busy){ api().stop(); return }
    if(artKey){ closeArtifact(); return }
  }
  if(!mod) return;
  const k = e.key.toLowerCase();
  if(k === "k"){ e.preventDefault(); openSearch() }
  else if(k === "n" || (e.shiftKey && k === "o")){ e.preventDefault(); newChat() }
  else if(k === "b"){ e.preventDefault(); toggleSidebar() }
  else if(k === ","){ e.preventDefault(); show("settings") }
});

/* ============================ init ============================ */
/* events: Python → page over Server-Sent Events */
let evSrc = null, evDown = false;
function connectEvents(){
  evSrc = new EventSource("/events?t=" + encodeURIComponent(TOKEN));
  evSrc.onmessage = m => { try{ window.__ev(JSON.parse(m.data)) }catch(e){ console.error(e) } };
  evSrc.onopen = () => { if(evDown){ evDown = false; setStatus("loading", "Reconnected"); refreshState().then(st => st.loaded && setStatus("ready", `Gemma 4 E2B · ${(st.backend_used || "").toUpperCase()}`)) } };
  evSrc.onerror = () => { if(evSrc.readyState === EventSource.CLOSED){ evDown = true; setStatus("err", "App stopped — restart EK Light"); setTimeout(connectEvents, 3000) } else evDown = true };
}
connectEvents();
(async () => {
  S.catalog = await api().plugins_catalog();
  const st = await refreshState();
  S.style = st.config.style || "normal"; S.thinking = !!st.config.thinking;
  setStyle(S.style); $("#thinkBtn").classList.toggle("on", S.thinking);
  const m = await api().mcp_state(); S.servers = m.servers || [];
  S.attached = new Set([...S.attached].filter(n => S.servers.some(s => s.name === n)));
  renderPlugChips(); $("#plugBadge").textContent = S.servers.length || "";
  setHome(true); loadChats(); renderPending();
  if(st.loaded){ S.ready = true; setStatus("ready", `Gemma 4 E2B · ${(st.backend_used || "").toUpperCase()}`) }
  else if(st.model_exists) api().load_model();
  else { show("settings"); toast("Download the model to get started", 6000) }
  $("#input").focus();
})();
