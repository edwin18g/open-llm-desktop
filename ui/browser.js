/* EK Light LLM — inline browser panel.
   Shows the app's hidden Chrome live (DevTools screencast) and sends it your clicks, scrolling and typing.
   The Browser plugin drives the same Chrome, so this is also where you watch Gemma browse. */
"use strict";

const WB = {ws: null, seq: 0, cbs: {}, page: null, meta: null, known: new Set(), poll: null, open: false, w: 0, h: 0};

/* ---------- DevTools connection ---------- */
function wbSend(method, params = {}){
  return new Promise((resolve) => {
    if(!WB.ws || WB.ws.readyState !== 1) return resolve(null);
    const id = ++WB.seq; WB.cbs[id] = resolve;
    WB.ws.send(JSON.stringify({id, method, params}));
  });
}
function wbMsg(text){ const m = $("#wbMsg"); m.textContent = text || ""; m.classList.toggle("hidden", !text) }

async function wbAttach(page){
  if(WB.ws){ WB.ws.onclose = null; WB.ws.close() }
  WB.page = page; WB.meta = null; WB.known.add(page.id);
  $("#wbUrl").value = page.url === "about:blank" ? "" : page.url;
  const ws = new WebSocket(page.ws); WB.ws = ws;
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if(m.id){ const cb = WB.cbs[m.id]; delete WB.cbs[m.id]; if(cb) cb(m.result || null); return }
    if(m.method === "Page.screencastFrame"){
      WB.meta = m.params.metadata; $("#wbImg").src = "data:image/jpeg;base64," + m.params.data; wbMsg("");
      ws.send(JSON.stringify({id: ++WB.seq, method: "Page.screencastFrameAck", params: {sessionId: m.params.sessionId}}));
    } else if(m.method === "Page.frameNavigated" && !m.params.frame.parentId){
      if(document.activeElement !== $("#wbUrl")) $("#wbUrl").value = m.params.frame.url === "about:blank" ? "" : m.params.frame.url;
      WB.page.url = m.params.frame.url;
    }
  };
  ws.onclose = () => { if(WB.ws === ws && WB.open) wbMsg("Disconnected — reconnecting…"), setTimeout(wbRefresh, 1000) };
  await new Promise((ok, bad) => { ws.onopen = ok; ws.onerror = bad }).catch(() => wbMsg("Couldn't connect to the browser."));
  await wbSend("Page.enable");
  // A hidden Chrome page isn't "focused", and some sites then ignore typing and focus styles.
  await wbSend("Emulation.setFocusEmulationEnabled", {enabled: true});
  await wbFit();
}
async function wbFit(){
  const v = $("#wbView"); const w = Math.max(320, v.clientWidth), h = Math.max(240, v.clientHeight);
  if(w === WB.w && h === WB.h && WB.meta) return;
  WB.w = w; WB.h = h; const dpr = window.devicePixelRatio || 1;
  await wbSend("Emulation.setDeviceMetricsOverride", {width: w, height: h, deviceScaleFactor: dpr, mobile: false});
  await wbSend("Page.stopScreencast");
  await wbSend("Page.startScreencast", {format: "jpeg", quality: 70, maxWidth: Math.round(w * dpr), maxHeight: Math.round(h * dpr)});
}
new ResizeObserver(() => { if(WB.open){ clearTimeout(WB.rt); WB.rt = setTimeout(() => { WB.w = 0; wbFit() }, 150) } }).observe($("#wbView"));

/* ---------- tabs ---------- */
async function wbRefresh(){
  if(!WB.open) return;
  const r = await api().browser_pages();
  if(r.error){ wbMsg(r.error); return }
  const pages = r.pages;
  // A tab that wasn't there before was opened by a link or by Gemma: show it.
  const fresh = pages.find(p => !WB.known.has(p.id));
  pages.forEach(p => WB.known.add(p.id));
  let cur = pages.find(p => WB.page && p.id === WB.page.id);
  if(fresh || !cur || !WB.ws || WB.ws.readyState > 1) await wbAttach(fresh || cur || pages[0]);
  else cur.url && (WB.page.title = cur.title);
  $("#wbTabs").innerHTML = pages.map(p => `<option value="${esc(p.id)}"${p.id === WB.page.id ? " selected" : ""}>${esc((p.title || p.url || "New tab").slice(0, 40))}</option>`).join("");
  $("#wbTabs").classList.toggle("hidden", pages.length < 2);
  WB.pages = pages;
}
$("#wbTabs").onchange = () => { const p = (WB.pages || []).find(x => x.id === $("#wbTabs").value); if(p) wbAttach(p) };

/* ---------- open / close ---------- */
async function openBrowser(url){
  if(typeof closeArtifact === "function") closeArtifact();
  $("#webPanel").classList.remove("hidden");
  if(!WB.open){ WB.open = true; wbMsg("Starting browser…"); WB.poll = setInterval(wbRefresh, 1500) }
  await wbRefresh();
  if(url) wbGo(url);
}
function closeBrowser(){
  WB.open = false; clearInterval(WB.poll); $("#webPanel").classList.add("hidden");
  if(WB.ws){ wbSend("Page.stopScreencast"); WB.ws.onclose = null; WB.ws.close(); WB.ws = null }
  WB.w = 0;
}
function wbAddress(text){
  text = text.trim(); if(!text) return "";
  if(/^https?:\/\//i.test(text)) return text;
  if(/^[\w-]+(\.[\w-]+)+(:\d+)?(\/\S*)?$/.test(text)) return "https://" + text;
  return "https://www.bing.com/search?q=" + encodeURIComponent(text);
}
async function wbGo(text){
  const url = wbAddress(text); if(!url) return;
  if(!/^https?:\/\//i.test(url)) return toast("Only web addresses (http or https) can be opened here");
  $("#wbUrl").value = url; await wbSend("Page.navigate", {url}); $("#wbView").focus();
}
async function wbHistory(step){
  const h = await wbSend("Page.getNavigationHistory"); if(!h) return;
  const e = h.entries[h.currentIndex + step]; if(e) wbSend("Page.navigateToHistoryEntry", {entryId: e.id});
}
$("#wbUrl").addEventListener("keydown", e => { if(e.key === "Enter"){ e.preventDefault(); wbGo($("#wbUrl").value) } });
$("#wbBack").onclick = () => wbHistory(-1);
$("#wbFwd").onclick = () => wbHistory(1);
$("#wbReload").onclick = () => wbSend("Page.reload");
$("#wbExt").onclick = () => { if(WB.page && /^https?:/.test(WB.page.url)) api().open_url(WB.page.url) };
$("#wbClose").onclick = closeBrowser;
$("#browserBtn").onclick = () => WB.open ? closeBrowser() : openBrowser();

/* ---------- your mouse and keyboard ---------- */
function wbPoint(e){
  const r = $("#wbImg").getBoundingClientRect(), m = WB.meta;
  const scale = m ? m.deviceWidth / r.width : 1;
  return {x: Math.round((e.clientX - r.left) * scale), y: Math.round((e.clientY - r.top) * scale)};
}
const WB_BUTTONS = ["left", "middle", "right"];
function wbMods(e){ return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0) }
const view = $("#wbView");
view.addEventListener("mousedown", e => { e.preventDefault(); view.focus(); wbSend("Input.dispatchMouseEvent", {type: "mousePressed", ...wbPoint(e), button: WB_BUTTONS[e.button] || "left", buttons: e.buttons, clickCount: e.detail || 1, modifiers: wbMods(e)}) });
view.addEventListener("mouseup", e => wbSend("Input.dispatchMouseEvent", {type: "mouseReleased", ...wbPoint(e), button: WB_BUTTONS[e.button] || "left", buttons: e.buttons, clickCount: e.detail || 1, modifiers: wbMods(e)}));
let wbMoveT = 0;
view.addEventListener("mousemove", e => { const t = Date.now(); if(t - wbMoveT < 40) return; wbMoveT = t; wbSend("Input.dispatchMouseEvent", {type: "mouseMoved", ...wbPoint(e), buttons: e.buttons, button: e.buttons ? "left" : "none", modifiers: wbMods(e)}) });
view.addEventListener("wheel", e => { e.preventDefault(); wbSend("Input.dispatchMouseEvent", {type: "mouseWheel", ...wbPoint(e), deltaX: e.deltaX, deltaY: e.deltaY, modifiers: wbMods(e)}) }, {passive: false});
view.addEventListener("contextmenu", e => e.preventDefault());
view.addEventListener("keydown", e => {
  if((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v") return;  // handled by the paste event
  e.preventDefault();
  const base = {key: e.key, code: e.code, windowsVirtualKeyCode: e.keyCode, nativeVirtualKeyCode: e.keyCode, modifiers: wbMods(e)};
  const printable = e.key.length === 1 && !e.ctrlKey && !e.metaKey;
  const text = printable ? e.key : e.key === "Enter" ? "\r" : undefined;
  const commands = (e.ctrlKey || e.metaKey) ? ({a: ["selectAll"], z: ["undo"], y: ["redo"], x: ["cut"], c: ["copy"]})[e.key.toLowerCase()] : undefined;
  wbSend("Input.dispatchKeyEvent", {type: text ? "keyDown" : "rawKeyDown", ...base, text, unmodifiedText: text, commands});
});
view.addEventListener("keyup", e => { e.preventDefault(); wbSend("Input.dispatchKeyEvent", {type: "keyUp", key: e.key, code: e.code, windowsVirtualKeyCode: e.keyCode, modifiers: wbMods(e)}) });
view.addEventListener("paste", e => { e.preventDefault(); const t = e.clipboardData.getData("text"); if(t) wbSend("Input.insertText", {text: t}) });

/* ---------- watching Gemma ---------- */
function wbSnapshot(){
  const img = $("#wbImg"); if(!WB.open || !img.naturalWidth) return "";
  const w = Math.min(560, img.naturalWidth), h = Math.round(img.naturalHeight * w / img.naturalWidth);
  const c = document.createElement("canvas"); c.width = w; c.height = h; c.getContext("2d").drawImage(img, 0, 0, w, h);
  return c.toDataURL("image/jpeg", 0.72);
}
const wbPrevEv = window.__ev;
window.__ev = ev => {
  wbPrevEv(ev);
  if(!/^browser_/.test(ev.name || "") && !(ev.type === "tool_result" && WB.calls && WB.calls.has(ev.call_id))) return;
  if(ev.type === "tool_call"){
    (WB.calls = WB.calls || new Set()).add(ev.call_id);
    if(!WB.open) openBrowser(); else setTimeout(wbRefresh, 300);
  } else if(ev.type === "tool_result"){
    // Give the page a moment to draw, then keep a picture of it with the step.
    setTimeout(async () => {
      await wbRefresh(); await new Promise(r => setTimeout(r, 500));
      const shot = wbSnapshot(); if(!shot) return;
      const card = document.querySelector(`.toolcard[data-call="${CSS.escape(ev.call_id)}"]`);
      if(card && !card.querySelector(".shot")) card.insertAdjacentHTML("beforeend", `<img class="shot" src="${shot}" alt="Browser after this step">`);
      api().tool_shot(ev.call_id, shot);
    }, 400);
  }
};
