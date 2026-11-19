/* EK Light LLM — live voice.
   Mic → speech detection → the clip goes to Gemma as audio (it hears it directly) → the reply is
   spoken with Piper sentence by sentence while it streams. Talk over a reply to interrupt it. */
"use strict";

const VOICE = {
  END_SILENCE_MS: 900,   // this much quiet ends your turn
  MIN_SPEECH_MS: 350,    // shorter sounds are ignored (clicks, coughs)
  MAX_SPEECH_MS: 30000,
  PRE_ROLL_MS: 500,      // audio kept from just before speech was detected
  START_MS: 130,         // loud this long = speech started
  BARGE_MS: 260,         // loud this long while Gemma replies = interruption
};
const V = {
  on: false, state: "off", ctx: null, stream: null, node: null, rate: 16000, frameMs: 64,
  noise: 0.008, loudMs: 0, quietMs: 0, pre: [], rec: [], recMs: 0,
  botId: null, raw: "", spokenTo: 0, queue: [], playing: null, genDone: false, turn: 0, ttsReady: false,
};

/* ---------- UI ---------- */
const VOICE_LABELS = {listening: "Listening…", recording: "Hearing you…", sending: "Thinking…",
                      replying: "Speaking · talk to interrupt"};
function voiceUi(state){
  V.state = state;
  $("#micBtn").classList.toggle("on", V.on); $("#micBtn").classList.toggle("live", state === "recording");
  const el = $("#voiceState"); el.classList.toggle("hidden", !V.on); el.textContent = VOICE_LABELS[state] || "";
  $("#micBtn").title = V.on ? "Stop voice mode" : "Talk to Gemma (voice mode)";
}

/* ---------- start / stop ---------- */
async function voiceStart(){
  if(!S.ready) return toast("Wait for the model to load");
  const st = await api().voice_status();
  if(!st.audio_input) return toast("Turn on “Image & audio input” in Settings to talk to Gemma", 6000);
  V.ttsReady = st.ready; if(st.ready) api().voice_warmup();
  if(!st.ready && !st.installing && !lsGet("ttsDeclined:" + st.voice, false)){
    const v = await askForm({title: "Spoken replies", ok: "Download voice",
      body: `Voice mode needs an offline voice for the language chosen in Settings (${st.size}, one time). Without it, voice mode still works and replies stay as text.`});
    if(v){ api().voice_install(); toast("Downloading voice… you can start talking now", 5000) } else lsSet("ttsDeclined:" + st.voice, true);
  }
  try{
    V.stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
  }catch(e){ return toast("Microphone unavailable: " + e.message, 6000) }
  try{ V.ctx = new AudioContext({sampleRate: 16000}) }catch(e){ V.ctx = new AudioContext() }
  V.rate = V.ctx.sampleRate;
  const src = V.ctx.createMediaStreamSource(V.stream);
  V.node = V.ctx.createScriptProcessor(1024, 1, 1);
  V.frameMs = 1024 / V.rate * 1000;
  V.node.onaudioprocess = e => onFrame(e.inputBuffer.getChannelData(0));
  src.connect(V.node); V.node.connect(V.ctx.destination);  // must be connected to run; it outputs silence
  V.on = true; V.pre = []; V.rec = []; V.loudMs = V.quietMs = 0;
  voiceUi("listening");
}
function voiceStop(){
  V.on = false; V.turn++; stopPlayback();
  if(V.node) V.node.disconnect();
  if(V.stream) V.stream.getTracks().forEach(t => t.stop());
  if(V.ctx) V.ctx.close().catch(() => {});
  V.ctx = V.stream = V.node = null; V.botId = null;
  voiceUi("off");
}

/* ---------- speech detection ---------- */
function onFrame(buf){
  if(!V.on) return;
  let sum = 0; for(let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
  const rms = Math.sqrt(sum / buf.length), f = new Float32Array(buf);
  const startThr = Math.max(0.02, V.noise * 3.5);

  if(V.state === "recording"){
    V.rec.push(f); V.recMs += V.frameMs;
    V.quietMs = rms < Math.max(0.012, V.noise * 2.2) ? V.quietMs + V.frameMs : 0;
    if(V.quietMs >= VOICE.END_SILENCE_MS || V.recMs >= VOICE.MAX_SPEECH_MS) finishTurn();
    return;
  }
  V.pre.push(f); while(V.pre.length * V.frameMs > VOICE.PRE_ROLL_MS) V.pre.shift();
  if(V.state === "listening"){
    if(rms > startThr){ V.loudMs += V.frameMs; if(V.loudMs >= VOICE.START_MS) startRecording() }
    else { V.loudMs = 0; V.noise = V.noise * 0.95 + rms * 0.05 }
  } else {
    // Gemma is thinking or speaking: only a clearly louder voice (not its own echo) interrupts.
    const thr = V.playing ? Math.max(0.06, V.noise * 7) : startThr * 1.5;
    if(rms > thr){ V.loudMs += V.frameMs; if(V.loudMs >= VOICE.BARGE_MS) interrupt() } else V.loudMs = 0;
  }
}
function startRecording(){
  V.rec = V.pre.slice(); V.recMs = V.rec.length * V.frameMs; V.quietMs = 0; V.loudMs = 0; V.pre = [];
  voiceUi("recording");
}
function interrupt(){
  V.turn++; stopPlayback(); V.botId = null;
  if(S.busy) api().stop();
  startRecording();
}

async function finishTurn(){
  const chunks = V.rec; V.rec = [];
  if(V.recMs - VOICE.END_SILENCE_MS < VOICE.MIN_SPEECH_MS){ voiceUi("listening"); return }
  voiceUi("sending");
  const turn = ++V.turn;
  try{
    const stamp = new Date().toTimeString().slice(0, 8).replace(/:/g, "-");
    const meta = await api().upload_file(`voice-${stamp}.wav`, wavDataUrl(chunks, V.rate));
    if(meta.error) throw new Error(meta.error);
    for(let i = 0; S.busy && i < 50; i++) await new Promise(r => setTimeout(r, 100));  // let a stopped reply finish
    if(turn !== V.turn || !V.on) return;
    if(currentView !== "chat") show("chat");
    V.raw = ""; V.spokenTo = 0; V.queue = []; V.genDone = false;
    if(!await send(meta)){ voiceUi("listening"); return }
    V.botId = S.cur && S.cur.id;
  }catch(e){ toast("Voice message failed: " + e.message, 5000); if(turn === V.turn) voiceUi("listening") }
}

function wavDataUrl(chunks, rate){
  const n = chunks.reduce((a, c) => a + c.length, 0), buf = new ArrayBuffer(44 + n * 2), d = new DataView(buf);
  const str = (o, s) => { for(let i = 0; i < s.length; i++) d.setUint8(o + i, s.charCodeAt(i)) };
  str(0, "RIFF"); d.setUint32(4, 36 + n * 2, true); str(8, "WAVE"); str(12, "fmt "); d.setUint32(16, 16, true);
  d.setUint16(20, 1, true); d.setUint16(22, 1, true); d.setUint32(24, rate, true); d.setUint32(28, rate * 2, true);
  d.setUint16(32, 2, true); d.setUint16(34, 16, true); str(36, "data"); d.setUint32(40, n * 2, true);
  let o = 44;
  for(const c of chunks) for(let i = 0; i < c.length; i++, o += 2){ const s = Math.max(-1, Math.min(1, c[i])); d.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true) }
  let bin = ""; const bytes = new Uint8Array(buf);
  for(let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return "data:audio/wav;base64," + btoa(bin);
}

/* ---------- spoken replies ---------- */
// Markdown → plain speech. Only complete code blocks are replaced, so earlier text never changes as tokens arrive.
function toSpeech(md){
  const open = md.split("```").length % 2 === 0; if(open) md = md.slice(0, md.lastIndexOf("```"));
  return md.replace(/```[\s\S]*?```/g, " (the code is on screen.) ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s*\|.*$/gm, "").replace(/https?:\/\/\S+/g, "a link")
    .replace(/^\s{0,3}(#{1,6}|[-*+]|\d+[.)])\s+/gm, "").replace(/[*_`~>#]/g, "")
    .replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu, "");
}
function speakFrom(final){
  if(!V.ttsReady) return;
  // The reply ends where Gemma starts writing the transcript (<heard>…), so speak the rest right away.
  const h = V.raw.search(/<(h(e(a(r(d)?)?)?)?)?$|<heard>/);
  if(h >= 0) final = true;
  const text = toSpeech(h >= 0 ? V.raw.slice(0, h) : V.raw);
  if(text.length < V.spokenTo) return;
  const rest = text.slice(V.spokenTo);
  let cut = final ? rest.length : -1;
  if(!final){
    const re = /[.!?](?=\s)|\n/g; let m;
    while((m = re.exec(rest))) if(m.index + 1 >= 12) { cut = m.index + 1; break }
  }
  if(cut <= 0) return;
  const seg = rest.slice(0, cut).replace(/\s+/g, " ").trim(); V.spokenTo += cut;
  if(/[\p{L}\p{N}]/u.test(seg)){  // any language's letters; punctuation alone isn't spoken
    V.queue.push(api().tts(seg).catch(() => "")); pump(V.turn) }
  if(!final) speakFrom(false);
}
async function pump(turn){
  if(V.playing || !V.queue.length) return;
  V.playing = "loading";
  const url = await V.queue.shift();
  if(turn !== V.turn || !V.on){ V.playing = null; return }
  if(!url){ V.playing = null; return next(turn) }
  const a = new Audio(url); V.playing = a; voiceUi("replying");
  a.onended = a.onerror = () => { if(V.playing === a){ V.playing = null; next(turn) } };
  a.play().catch(() => { V.playing = null; next(turn) });
}
function next(turn){
  if(turn !== V.turn || !V.on) return;
  if(V.queue.length) pump(turn); else if(V.genDone) replyFinished();
}
function stopPlayback(){
  if(V.playing && V.playing.pause) V.playing.pause();
  V.playing = null; V.queue = [];
}
function replyFinished(){ V.botId = null; if(V.on && V.state !== "recording") voiceUi("listening") }

/* ---------- events from Python ---------- */
const appEv = window.__ev;
window.__ev = ev => {
  appEv(ev);
  if(ev.type === "voice_setup"){
    if(ev.error) toast("Voice download failed: " + ev.error, 8000);
    else if(ev.done){ V.ttsReady = true; toast("Spoken replies are ready") }
    else if(V.on) $("#voiceState").textContent = `${ev.step} ${ev.pct || 0}%`;
    return;
  }
  if(!V.on) return;
  if(ev.type === "token" && !V.botId && S.cur && S.cur.id === ev.id && V.state === "listening"){
    V.botId = ev.id; V.raw = ""; V.spokenTo = 0; V.queue = []; V.genDone = false; voiceUi("sending");  // typed message in voice mode
  }
  if(ev.id !== V.botId || !V.botId) return;
  if(ev.type === "token"){ V.raw += ev.text; speakFrom(false) }
  else if(ev.type === "done"){
    V.genDone = true; speakFrom(true);
    if(!V.playing && !V.queue.length) replyFinished();
  }
};

$("#micBtn").onclick = () => V.on ? voiceStop() : voiceStart();
