/* Scrum Simu - front end (plain JavaScript, no build step) */
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let V = null;            // current view from the server
let META = null;
let view = "meeting";    // meeting | work
let tab = "board";
let demoSprint = null;
let busy = false;
const played = {};       // ceremony index -> number of messages already shown
const queue = [];
let playing = false;
let skipCurrent = null;
let meetingStart = {};   // ceremony index -> timestamp
let handUp = false;      // you raised your hand
let floorGiven = false;  // the facilitator already said "go ahead"
let userTyping = false;  // you're talking/typing: the team stays quiet
let lastMsgCount = -1;
let soundOn = true;
try { soundOn = localStorage.getItem("sg-sound") !== "off"; } catch (e) { /* storage blocked */ }

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  let r;
  try {
    r = await fetch(path, opts);
  } catch (e) {
    serverLost();
    throw new Error("Can't reach the Scrum Simu server. Waiting for it to come back...");
  }
  if (lostTimer) serverBack();
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || "Something went wrong. Try again.");
  return data;
}

/* ======================= server lost / resume ======================= */
// If the server stops (Terminal closed) or the laptop sleeps, show it clearly and carry on when it's back.
let lostTimer = null;
function serverLost() {
  if (lostTimer) return;
  let bar = $("#connLost");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "connLost";
    bar.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:999;background:#b91c1c;color:#fff;padding:10px 16px;font:14px system-ui;text-align:center";
    document.body.appendChild(bar);
  }
  bar.innerHTML = "Lost connection to the Scrum Simu server. If you closed Terminal, run <code>python app.py</code> again. The meeting will continue by itself.";
  bar.hidden = false;
  lostTimer = setInterval(async () => {
    try { const r = await fetch("api/meta"); if (r.ok) serverBack(true); } catch (e) { /* still down */ }
  }, 3000);
}
async function serverBack(resume) {
  clearInterval(lostTimer);
  lostTimer = null;
  const bar = $("#connLost");
  if (bar) bar.hidden = true;
  busy = false;
  if (!resume) return;
  try { const v = await api("api/state"); showApp(v, true); } catch (e) { /* next retry */ }
}

/* ======================= keep the meeting moving ======================= */
// Nobody talks for a while? The Scrum Master reminds the next person (or you) by name.
let lastActivity = Date.now();
let nudgeKey = "";
let nudges = 0;
const touch = () => { lastActivity = Date.now(); };
["keydown", "click"].forEach((ev) => document.addEventListener(ev, touch, true));
// Browsers only allow speech after a click: the first click anywhere unlocks the voices.
document.addEventListener("click", () => {
  if ("speechSynthesis" in window) speechSynthesis.speak(new SpeechSynthesisUtterance(""));
}, { once: true });

function leadLine(text) {
  const lead = V.project.role === "sm" ? "po" : "sm";
  const p = V.team.find((t) => t.role === lead);
  return { who: lead, name: p.name, title: p.title, text };
}

setInterval(() => {
  // A new turn (new message or new meeting) means you may be asked again, mic on or off.
  if (V && V.meeting.live) {
    const key = `${V.ceremony.index}:${V.meeting.messages.length}`;
    if (key !== nudgeKey) { nudgeKey = key; nudges = 0; micNudges = 0; }
  }
  if (V && V.meeting.live && listening && !holdMic && !playing && !queue.length) {
    // Your mic is on but we hear nothing: the team asks you, by name, instead of waiting in silence.
    const heard = ($("#msgInput").value || "").trim();
    // Asked at most once per turn: if they were already asked, just keep listening quietly.
    if (!heard && Date.now() - lastHeard > 10000 && nudges === 0) {
      micNudges += 1;
      nudges = 1;
      lastHeard = Date.now();
      nudgeWhileListening(micNudges);
    }
    return;
  }
  if (!V || !V.meeting.live || lostTimer || busy || playing || queue.length || handUp || userTyping || listening) return;
  const idle = Date.now() - lastActivity;
  if (V.meeting.awaiting) {
    // Your turn and you're quiet: they ask you once, then wait silently. Your turn is never skipped for you.
    if (idle > 10000 && nudges === 0) {
      nudges = 1;
      touch();
      const ask = V.meeting.awaiting.prompt.replace(/^Your turn:\s*/i, "");
      queue.push(leadLine(V.meeting.awaiting.kind === "summary" ? `${myName()}, over to you to wrap up and close, whenever you're ready.`
        : V.project.role === "sm" ? `${myName()}, over to you.` : `${myName()}, over to you: ${ask}`));
      playQueue();
    }
    return;
  }
  if (idle > 7000) { touch(); pump(true); }   // a teammate should be talking: remind them
}, 2000);

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 4500);
}

async function act(fn, waitingText) {
  if (busy) return;
  busy = true;
  if (waitingText) idleCaption(waitingText, true);
  renderControls();
  try {
    const v = await fn();
    if (v && v.ceremony) render(v);
    return true;
  } catch (e) {
    toast(e.message);
    return false;
  } finally {
    busy = false;
    // Keep your own words on screen until the next person speaks.
    if (V) { renderControls(); if (!playing && (V.meeting.messages.at(-1)?.who !== "user" || V.meeting.awaiting || !V.meeting.live)) idleCaption(); }
  }
}

/* ======================= voices ======================= */
/* Voices: use the most natural voices the browser has, normal pitch, calm pace.
   Novelty voices (the "cartoon" ones) are never used. */
const NOVELTY = /albert|bad news|bahh|bells|boing|bubbles|cellos|good news|jester|organ|superstar|trinoids|whisper|wobble|zarvox|fred|junior|ralph|kathy|princess|grandma|grandpa|eddy|flo|reed|rocko|sandy|shelley/i;
const FEMALE = /ava|zoe|samantha|allison|susan|karen|moira|tessa|serena|fiona|victoria|joelle|nicky|kate|jenny|aria|zira|female|libby|sonia|emma|veena|isha|neerja|catherine|natasha|leah|luciana/i;
const MALE = /evan|nathan|tom|daniel|aaron|alex|oliver|arthur|rishi|lee|gordon|david|mark|guy|ryan|male|james|noah|prabhat|william|thomas|liam|connor/i;
const SPEEDS = [0.82, 0.92, 1.02];
let speedIdx = 1;
try { speedIdx = +(localStorage.getItem("sg-speed") ?? 1); } catch (e) { /* ignore */ }
let voiceMap = {};

function voiceScore(v) {
  let s = 0;
  if (/premium/i.test(v.name)) s += 50;
  if (/enhanced|neural|natural/i.test(v.name)) s += 40;
  if (/google/i.test(v.name)) s += 25;          // Chrome's online voices are decent
  if (/^en[-_]US/i.test(v.lang)) s += 10;
  else if (/^en[-_](GB|AU|IE|CA)/i.test(v.lang)) s += 6;
  if (v.localService === false) s += 3;
  return s;
}

// Where a teammate's name usually comes from -> the English accent we try first for their voice.
const ACCENTS = {
  IN: ["priya", "aisha", "arjun", "ravi", "lakshmi"],
  GB: ["hannah", "laura", "daniel", "tom", "ben", "chloe", "ingrid", "jonas", "lena", "felix", "leo"],
  AU: ["grace", "sam", "zara"],
  ZA: ["kwame", "amara", "fatima", "nadia", "omar"],
  IE: ["julia", "noah"],
};
const FEMALE_ROLES = { po: true, tester: true };   // the name lists for these roles are women's names
let voiceKey = "";

function accentOf(name) {
  const n = (name || "").toLowerCase();
  return Object.keys(ACCENTS).find((k) => ACCENTS[k].includes(n)) || "US";
}

// Realistic neural voices (served by /api/tts), by accent and gender. First unused one wins.
const NEURAL = {
  IN: { f: ["en-IN-NeerjaNeural", "en-IN-NeerjaExpressiveNeural"], m: ["en-IN-PrabhatNeural"] },
  GB: { f: ["en-GB-SoniaNeural", "en-GB-LibbyNeural", "en-GB-MaisieNeural"], m: ["en-GB-RyanNeural", "en-GB-ThomasNeural"] },
  AU: { f: ["en-AU-NatashaNeural"], m: ["en-AU-WilliamMultilingualNeural"] },
  ZA: { f: ["en-ZA-LeahNeural"], m: ["en-ZA-LukeNeural"] },
  IE: { f: ["en-IE-EmilyNeural"], m: ["en-IE-ConnorNeural"] },
  US: { f: ["en-US-AvaNeural", "en-US-EmmaNeural", "en-US-JennyNeural", "en-US-AriaNeural"],
        m: ["en-US-AndrewNeural", "en-US-BrianNeural", "en-US-ChristopherNeural", "en-US-GuyNeural"] },
};
let neuralMap = {};
let neuralOk = true;      // turned off after repeated failures; the browser voices take over
let neuralFails = 0;

function pickNeural(team) {
  const used = new Set();
  neuralMap = {};
  for (const t of team.filter((x) => !x.is_user)) {
    const g = FEMALE_ROLES[t.role] ? "f" : "m";
    const list = [...NEURAL[accentOf(t.name)][g], ...NEURAL.US[g], ...NEURAL.GB[g]];
    const v = list.find((x) => !used.has(x)) || list[0];
    used.add(v);
    neuralMap[t.role] = v;
  }
}

function pickVoices() {
  pickNeural((V && V.team) || [{ role: "po" }, { role: "sm" }, { role: "dev" }, { role: "tester" }]);
  if (!("speechSynthesis" in window)) { voiceKey = ((V && V.team) || []).map((t) => t.name).join("|"); return; }
  const all = speechSynthesis.getVoices()
    .filter((v) => /^en/i.test(v.lang) && !NOVELTY.test(v.name))
    .sort((a, b) => voiceScore(b) - voiceScore(a));
  if (!all.length) return;
  const isF = (v) => FEMALE.test(v.name) || /female/i.test(v.name);
  const isM = (v) => !isF(v) && (MALE.test(v.name) || /\bmale/i.test(v.name));
  const team = (V && V.team) || [{ role: "po" }, { role: "sm" }, { role: "dev" }, { role: "tester" }];
  const used = new Set();
  voiceMap = {};
  for (const t of team.filter((x) => !x.is_user)) {
    const female = !!FEMALE_ROLES[t.role];
    const acc = accentOf(t.name);
    const fits = (v) => (female ? isF(v) : isM(v));
    const inAccent = (v) => new RegExp(`^en[-_]${acc}`, "i").test(v.lang);
    const v = all.find((x) => !used.has(x.name) && inAccent(x) && fits(x))   // right accent, right voice
      || all.find((x) => !used.has(x.name) && fits(x))                       // right voice, any English
      || all.find((x) => !used.has(x.name))                                  // anything not used yet
      || all[0];
    used.add(v.name);
    voiceMap[t.role] = v;
  }
  voiceKey = team.map((t) => t.name).join("|");
}
if ("speechSynthesis" in window) {
  pickVoices();
  speechSynthesis.onvoiceschanged = pickVoices;
}

function sayOne(text, role, onWord) {
  return new Promise((resolve) => {
    if (V && V.team && V.team.map((t) => t.name).join("|") !== voiceKey) pickVoices();   // new team: new voices
    const u = new SpeechSynthesisUtterance(text);
    if (voiceMap[role]) u.voice = voiceMap[role];
    u.pitch = 1;
    u.rate = SPEEDS[speedIdx] ?? 0.92;
    u.onend = resolve;
    u.onerror = resolve;
    if (onWord) u.onboundary = (e) => onWord(e.charIndex + (e.charLength || 0));
    speechSynthesis.speak(u);
    setTimeout(resolve, text.length * 120 + 3000); // safety net
  });
}

// One audio element, unlocked by the first click (Safari needs that).
const audioEl = new Audio();
audioEl.preload = "auto";
const SILENT = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";
document.addEventListener("click", () => { audioEl.src = SILENT; audioEl.play().catch(() => {}); }, { once: true, capture: true });
const ttsCache = new Map();

function neuralUrl(text, role) {
  if (!neuralOk || !soundOn || role === "system" || role === "user") return null;
  if (V && V.team && V.team.map((t) => t.name).join("|") !== voiceKey) pickVoices();
  const voice = neuralMap[role];
  if (!voice) return null;
  const rate = Math.round(((SPEEDS[speedIdx] ?? 0.92) - 0.96) * 100);
  const key = `${voice}|${rate}|${text}`;
  if (!ttsCache.has(key)) {
    ttsCache.set(key, fetch("api/tts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice, rate }),
    }).then((r) => (r.ok ? r.blob() : null))
      .then((b) => { if (b) { neuralFails = 0; return URL.createObjectURL(b); } throw new Error("tts"); })
      .catch(() => { ttsCache.delete(key); if (++neuralFails >= 3) neuralOk = false; return null; }));
  }
  return ttsCache.get(key);
}
// Fetch the next lines' audio while the current one plays, so there is no gap.
function prefetchQueue() { queue.slice(0, 2).forEach((m) => neuralUrl(m.text, m.who)); }

function playAudio(url, isDone, onProgress) {
  return new Promise((resolve) => {
    if (isDone()) return resolve(true);
    let raf = 0;
    const tick = () => {
      const d = audioEl.duration;
      if (onProgress && d && isFinite(d)) onProgress(audioEl.currentTime / d);
      if (!audioEl.paused && !audioEl.ended) raf = requestAnimationFrame(tick);
    };
    audioEl.onplaying = () => { cancelAnimationFrame(raf); tick(); };
    audioEl.onended = () => { cancelAnimationFrame(raf); resolve(true); };
    audioEl.onerror = () => { cancelAnimationFrame(raf); resolve(false); };
    audioEl.src = url;
    audioEl.playbackRate = 1;
    audioEl.play().catch(() => resolve(false));   // blocked (no click yet) -> browser voice
  });
}

// The caption shows only the words the voice has reached, so text and speech stay together.
function speak(text, role, reveal) {
  reveal = reveal || (() => {});
  return new Promise(async (resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; reveal(text.length); resolve(); } };
    skipCurrent = () => { audioEl.pause(); if ("speechSynthesis" in window) speechSynthesis.cancel(); finish(); };
    const readTime = Math.max(2200, text.length * 65);
    const neural = soundOn && role !== "system" ? neuralUrl(text, role) : null;
    prefetchQueue();
    if (neural) {
      const url = await neural;
      if (done) return;
      // Audio has a little lead-in silence; trail the words slightly so they never run ahead of the voice.
      if (url && await playAudio(url, () => done, (f) => reveal(Math.floor(text.length * Math.max(0, f - 0.04) / 0.92)))) { finish(); return; }
    }
    if (!soundOn || !("speechSynthesis" in window) || role === "system") {
      if (role === "system") { reveal(text.length); setTimeout(finish, 2400); return; }
      // Sound off: words appear at a calm reading pace.
      const start = Date.now();
      const t = setInterval(() => {
        if (done) return clearInterval(t);
        const f = (Date.now() - start) / readTime;
        reveal(Math.floor(text.length * Math.min(1, f / 0.85)));
        if (f >= 1) { clearInterval(t); finish(); }
      }, 80);
      return;
    }
    // Speak sentence by sentence with a short breath in between: sounds more human.
    const parts = text.match(/[^.!?]+[.!?]*\s*/g) || [text];
    let offset = 0;
    for (const p of parts) {
      if (done) return;
      const lead = p.length - p.trimStart().length;
      const base = offset + lead;
      let boundaries = false;
      // Voices that report word boundaries move the caption word by word;
      // others show the sentence as it starts being spoken.
      const sentenceTimer = setTimeout(() => { if (!boundaries) reveal(base + p.trim().length); }, 250);
      await sayOne(p.trim(), role, (n) => { boundaries = true; reveal(base + n); });
      clearTimeout(sentenceTimer);
      offset += p.length;
      reveal(offset);
      await new Promise((r) => setTimeout(r, 180));
    }
    finish();
  });
}

async function playQueue() {
  if (playing) return;
  playing = true;
  while (queue.length) {
    const m = queue.shift();
    setSpeaking(m.who);
    showCaption(m, 0);
    touch();
    let shown = 0;
    await speak(m.text, m.who, (n) => { if (n > shown) { shown = n; showCaption(m, n); } });
    touch();
    showLastSaid(m);
    setSpeaking(null);
    await new Promise((r) => setTimeout(r, 250));
  }
  playing = false;
  skipCurrent = null;
  idleCaption();
  afterTurn();
}

/* ======================= turn-taking ======================= */
// One person talks at a time. After each turn the browser asks the server for the next one,
// unless it's your turn, you raised your hand, or you're typing/talking.
function afterTurn() {
  if (!V || !V.meeting.live) return;
  if (handUp) { if (!floorGiven && !V.meeting.awaiting) giveFloor(); return; }
  setTimeout(pump, 600);
}

async function pump(nudge) {
  if (!V || !V.meeting.live) return;
  if (busy || playing || queue.length || handUp || userTyping || V.meeting.awaiting) return;
  const before = V.meeting.messages.length;
  if (V.meeting.messages.at(-1)?.who !== "user") idleCaption("", true, "Waiting for the next speaker");
  touch();
  await act(() => api("api/meeting/next", nudge === true ? { nudge: true } : {}), null);
  touch();
  // Nothing new and not your turn: stop instead of looping.
  if (V && V.meeting.live && !V.meeting.awaiting && V.meeting.messages.length === before && !queue.length) idleCaption();
}

function giveFloor() {
  floorGiven = true;
  // The facilitator hands you the floor out loud, then waits.
  const lead = V.project.role === "sm" ? "po" : "sm";
  const p = V.team.find((t) => t.role === lead);
  const line = { who: lead, name: p.name, title: p.title, text: "Yes? Go ahead, you have the floor." };
  queue.push(line);
  playQueue().then(() => {
    idleCaption("You have the floor. Click Talk (or type), and click 'Done talking' when finished. Everyone is listening.");
    $("#msgInput").focus();
  });
}

let lastSaid = "";

function silence() {
  queue.length = 0;
  stopTeam();
  setSpeaking(null);
}

function stopTeam() {
  // You started talking: everyone goes quiet right away.
  audioEl.pause();
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  if (skipCurrent) skipCurrent();
}

function myName() {
  return (V && V.player_name) || "You";
}

function setSpeaking(who) {
  document.querySelectorAll(".tile").forEach((t) => t.classList.toggle("speaking", t.dataset.role === who));
}

function showCaption(m, spoken) {
  const c = $("#caption");
  // spoken = how many characters the voice has reached; the rest stays hidden (but keeps its space).
  let body = esc(m.text);
  if (spoken != null && spoken < m.text.length) {
    let cut = Math.max(0, spoken);
    const sp = m.text.indexOf(" ", cut);           // finish the current word, never half a word
    cut = cut === 0 ? 0 : (sp === -1 ? m.text.length : sp);
    body = `${esc(m.text.slice(0, cut))}<span class="unsaid">${esc(m.text.slice(cut))}</span>`;
  }
  if (m.who === "system") {
    c.innerHTML = `<span class="who">Note</span>${body}`;
  } else {
    const who = m.who === "user" ? `${esc(myName())} (you, ${esc(V.user_role_title)}) ${m.live ? "is speaking..." : "said"}` : `${esc(m.name)}, ${esc(m.title)}, is speaking`;
    c.innerHTML = `<span class="who">${who}</span>${body}`;
  }
}

// The box under the caption keeps the latest finished line, whoever said it.
function showLastSaid(m) {
  if (!m || m.who === "system" || m.kind === "summary") return;
  const who = m.who === "user" ? "You said" : `${esc(m.name)} (${esc(m.title)}) said`;
  $("#youSaid").hidden = false;
  $("#youSaid").innerHTML = `<b>${who}:</b> ${esc(m.text)}`;
}

function idleCaption(text, waiting, waitLabel) {
  const c = $("#caption");
  if (!V) return;
  if (waiting && !text) { c.innerHTML = `<span class="idle">&#8943; ${esc(waitLabel || "")}</span>`; return; }
  let msg = text;
  if (!msg && V.meeting.awaiting) {
    c.innerHTML = `<span class="who">Your turn, ${esc(myName())}</span>${esc(V.meeting.awaiting.prompt)}
      <span class="idle" style="display:block;margin-top:4px">Everyone is waiting for you. Click Talk and then 'Done talking', or type and press Enter.</span>`;
    return;
  }
  if (!msg) {
    if (V.project.complete) msg = "The project is complete. Look at the Charts and Demo in the Team workspace.";
    else if (V.meeting.live && handUp) msg = "Your hand is up. You'll get the floor when the speaker finishes.";
    else if (V.meeting.live) msg = "Listening to the team. Raise your hand or just start talking to jump in.";
    else if (V.meeting.summary) msg = "The meeting is closed. Read the summary on the right.";
    else if (V.meeting.count) {
      const sug = V.menu.find((m) => m.suggested);
      msg = `Meeting ended. Do your to-dos, then pick the next meeting at the top${sug ? ` (suggested: ${sug.label})` : ""}.`;
    } else msg = "The team is joining...";
  }
  c.innerHTML = `<span class="idle">${waiting ? "&#8943; " : ""}${esc(msg)}</span>`;
}

$("#caption").addEventListener("click", () => skipCurrent && skipCurrent());

$("#speedBtn").addEventListener("click", () => {
  speedIdx = (speedIdx + 1) % SPEEDS.length;
  try { localStorage.setItem("sg-speed", String(speedIdx)); } catch (e) { /* ignore */ }
  renderControls();
});

$("#soundBtn").addEventListener("click", () => {
  soundOn = !soundOn;
  try { localStorage.setItem("sg-sound", soundOn ? "on" : "off"); } catch (e) { /* ignore */ }
  if (!soundOn) { audioEl.pause(); if (skipCurrent) skipCurrent(); }
  if (!soundOn && "speechSynthesis" in window) speechSynthesis.cancel();
  renderControls();
});

/* ======================= microphone ======================= */
const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null;
let listening = false;
if (!Rec) $("#micBtn").hidden = true;

// Click the mic to start talking; click "Done talking" when you're finished.
// It keeps listening through pauses, and the team waits until you're done.
let micWanted = false;
let micText = "";
let lastHeard = 0;      // when your mic last picked up words
let micNudges = 0;      // how often the team asked you while your mic was on and silent
let holdMic = false;    // mic paused while a teammate speaks, so their voice isn't taken as yours

async function nudgeWhileListening(n) {
  const aw = V.meeting.awaiting;
  const ask = aw ? aw.prompt.replace(/^Your turn:\s*/i, "") : "";
  const text = ask ? `${myName()}, we're listening. ${ask}` : `${myName()}, go ahead, we're listening.`;
  holdMic = true;
  try { rec.stop(); } catch (e) { /* already stopped */ }
  queue.push(leadLine(text));
  await playQueue();
  holdMic = false;
  lastHeard = Date.now();
  if (listening && !micWanted && rec) { rec.onend(); return; }   // you clicked "Done talking" meanwhile: send it
  if (micWanted && listening) {
    try { startRec(); } catch (e) { setTimeout(() => { try { startRec(); } catch (e2) { /* gave up */ } }, 600); }
    idleCaption("Listening... take your time. Click 'Done talking' when you're finished.");
  }
}
function startRec() {
  rec = new Rec();
  rec.lang = "en-US";
  rec.interimResults = true;
  rec.continuous = true;
  const base = micText;
  let interimNow = "";
  rec.onresult = (e) => {
    let finals = "", interim = "";
    for (const r of e.results) { if (r.isFinal) finals += r[0].transcript + " "; else interim += r[0].transcript; }
    micText = (base + " " + finals).replace(/\s+/g, " ").trim();
    interimNow = interim;
    if ((finals + interim).trim()) lastHeard = Date.now();
    const live = (micText + " " + interim).trim();
    $("#msgInput").value = live;
    if (live) showCaption({ who: "user", name: myName(), title: V.user_role_title, text: live, live: true });
  };
  rec.onend = () => {
    // The browser stops listening now and then (pauses, long talks). Keep what was heard and carry on.
    if (interimNow) { micText = (micText + " " + interimNow).trim(); interimNow = ""; }
    if (holdMic) return;          // paused while a teammate asks you something; it restarts after
    if (micWanted) {
      setTimeout(() => { if (micWanted) { try { startRec(); } catch (e) { setTimeout(startRec, 600); } } }, 150);
      return;
    }
    listening = false;
    renderControls();
    const text = (micText || $("#msgInput").value).trim();
    micText = "";
    $("#msgInput").value = text;
    if (text) $("#composer").requestSubmit();
    else { userTyping = false; setTimeout(pump, 600); }
  };
  rec.onerror = (e) => {
    if (e.error === "not-allowed" || e.error === "service-not-allowed") {
      micWanted = false;
      toast("Microphone is blocked. Allow it in Chrome (address bar, mic icon), or type instead.");
    }
  };
  rec.start();
}

$("#micBtn").addEventListener("click", () => {
  if (!Rec) return;
  if (listening) { micWanted = false; rec.stop(); return; }   // "Done talking"
  stopTeam();
  userTyping = true;
  micWanted = true;
  micText = "";
  lastHeard = Date.now();
  micNudges = 0;
  $("#msgInput").value = "";
  listening = true;
  renderControls();
  idleCaption("Listening... take your time. Click 'Done talking' when you're finished.");
  startRec();
});

/* ======================= start screen ======================= */
async function init() {
  try {
    META = await api("api/meta");
  } catch (e) {
    $("#offlineMsg").innerHTML = `Scrum Simu can't reach its server. It doesn't work as a plain file:
      start it with <code>python app.py</code> in the aiscrum folder, then open
      <code>http://localhost:5050</code> in your browser.`;
    return;
  }
  $("#offline").hidden = true;
  if (META.has_project) {
    const v = await api("api/state");
    showApp(v, true);
    return;
  }
  showStart();
}

function showStart() {
  $("#offline").hidden = true;
  $("#app").hidden = true;
  $("#resetBtn").hidden = true;
  $("#progress").hidden = true;
  $("#productName").textContent = "";
  $("#start").hidden = false;
  $("#roleChoices").innerHTML = Object.entries(META.roles).filter(([k]) => k === "po" || k === "sm").map(([k, r]) => `
    <label class="role-tile">
      <input type="radio" name="role" value="${k}">
      <strong>${esc(r.title)}</strong>
      <span>${esc(roleBlurb(k))}</span>
    </label>`).join("");

  $("#roleChoices").addEventListener("change", () => {
    $("#startBtn").disabled = false;
  });
}

function roleBlurb(k) {
  return {
    po: "Decide what gets built: you name the product in the kickoff. Order the backlog, accept the work.",
    sm: "Run the ceremonies, protect the sprint, remove blockers.",
    dev: "Estimate, build (in pseudo-code), move cards, raise blockers.",
    tester: "Write test cases, pass or fail stories, log bugs.",
  }[k];
}

$("#startForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const role = document.querySelector("input[name=role]:checked")?.value;
  if (!role) return;
  // A click is needed before browsers allow speech: warm it up now.
  if ("speechSynthesis" in window) speechSynthesis.speak(new SpeechSynthesisUtterance(""));
  $("#startBtn").disabled = true;
  api("api/project", {
    role,
    player_name: $("#nameInput").value,
  }).then(async (v) => {
    showApp(v, false);
    await act(() => api("api/meeting/open", { type: "kickoff" }), "The team is joining the kickoff...");
    setTimeout(pump, 300);
  }).catch((err) => { toast(err.message); $("#startBtn").disabled = false; });
});

$("#resetBtn").addEventListener("click", async () => {
  const b = $("#resetBtn");
  if (b.dataset.armed !== "1") {
    b.dataset.armed = "1";
    b.textContent = "Click again to start a new project";
    setTimeout(() => { b.dataset.armed = ""; b.textContent = "Start over"; }, 3500);
    return;
  }
  await api("api/reset", {});
  b.dataset.armed = "";
  b.textContent = "Start over";
  audioEl.pause();
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  queue.length = 0;
  V = null;
  showStart();
});

function showApp(v, reload) {
  $("#start").hidden = true;
  $("#app").hidden = false;
  $("#resetBtn").hidden = false;
  if (reload) played[v.ceremony.index] = v.meeting.messages.length; // don't replay old talk
  render(v);
  if (reload && V.meeting.live) showLastSaid(V.meeting.messages.at(-1));
  // Reopened mid-meeting: the facilitator says where we are, then the meeting carries on.
  queue.length = 0; playing = false; handUp = false; floorGiven = false; userTyping = false; touch();
  if (reload && V.meeting.live) {
    const a = V.meeting.awaiting;
    const where = `Welcome back, ${myName()}. We're still in the ${V.ceremony.label}.`;
    queue.push(leadLine(a ? `${where} Over to you: ${a.prompt.replace(/^Your turn:\s*/i, "")}` : `${where} Let's carry on.`));
    playQueue();
  } else if (V.meeting.live) setTimeout(pump, 400);
}

/* ======================= render ======================= */
function render(v) {
  V = v;
  const idx = v.ceremony.index;
  if (v.meeting.live && !(idx in meetingStart)) meetingStart[idx] = Date.now();
  const seen = played[idx] ?? 0;
  const fresh = v.meeting.messages.slice(seen).filter((m) => m.who !== "user");
  played[idx] = v.meeting.messages.length;
  if (!v.meeting.live) $("#youSaid").hidden = true;
  if (v.meeting.live) queue.push(...fresh);
  else {
    // The meeting is over: nobody keeps talking. Only the short closing line is heard.
    silence();
    const close = fresh.filter((m) => m.kind === "summary").at(-1);
    if (close) queue.push(close);
  }

  $("#productName").textContent = `${v.project.product.name} · you are the ${v.user_role_title}`;
  renderProgress();
  renderMeeting();
  renderControls();
  renderNotice();
  renderGuide();
  renderPanels();
  if (queue.length) playQueue(); else if (!playing && !busy) idleCaption();
}

function renderProgress() {
  const p = $("#progress");
  p.hidden = false;
  const sp = V.sprint;
  let html;
  if (V.project.complete) html = '<span class="where">Project complete</span>';
  else if (!V.project.kickoff_done) html = '<span class="where">Kickoff</span>';
  else {
    const st = { unplanned: "not planned yet", active: "in progress", reviewed: "reviewed" }[sp.state];
    html = `<span class="where">Sprint ${sp.number} of ${sp.max}</span><span class="state">${st}</span>` +
      (sp.state !== "unplanned" ? `<span>${sp.standups} stand-up${sp.standups === 1 ? "" : "s"}</span>` : "");
  }
  p.innerHTML = html;
}

function renderPicker() {
  const box = $("#picker");
  const off = V.meeting.live || busy;
  box.innerHTML = `<span>Meetings:</span>` + V.menu.map((m) => {
    const title = m.done ? "Done" : m.enabled ? "Start this meeting" : (m.reason || "Not now");
    return `<button type="button" data-meet="${m.type}" class="${m.suggested && !off ? "suggested" : ""} ${m.done ? "done" : ""}"
      ${off || !m.enabled ? "disabled" : ""} title="${title}">${m.done ? "&#10003; " : ""}${esc(m.label)}</button>`;
  }).join("");
}

$("#picker").addEventListener("click", (e) => {
  const b = e.target.closest("[data-meet]");
  if (!b) return;
  silence();
  setView("meeting");
  handUp = false;
  act(() => api("api/meeting/open", { type: b.dataset.meet }), "The team is joining...")
    .then(() => setTimeout(pump, 300));
});

function renderMeeting() {
  const cer = V.ceremony;
  $("#ceremonyTitle").textContent = V.project.complete ? "Project complete"
    : V.meeting.count ? cer.label : "Project Kickoff";
  $("#ceremonyPurpose").textContent = V.coach.purpose;
  $("#tiles").innerHTML = V.team.map((p) => `
    <li class="tile ${p.is_user ? "you" : ""} ${p.is_user && V.meeting.awaiting ? "myturn" : ""}" data-role="${p.is_user ? "user" : p.role}">
      ${(p.is_user ? handUp : V.meeting.hands.includes(p.role)) ? '<span class="hand" title="Hand raised">&#9995;</span>' : ""}
      <span class="face" style="background:${p.color}" aria-hidden="true">${p.is_user ? (V.player_name ? esc(V.player_name[0].toUpperCase()) : "You") : esc(p.name[0])}</span>
      <span class="nameplate"><b>${p.is_user ? (V.player_name ? esc(V.player_name) + " (you)" : "You") : esc(p.name)}</b> · ${esc(p.title)}</span>
      <span class="bars" aria-hidden="true"><i></i><i></i><i></i></span>
    </li>`).join("");

  const c = V.coach;
  const goalBox = cer.type === "planning" && V.project.role === "sm" && !V.project.complete ? `
    <div class="goal-edit" style="margin-top:10px">
      <label for="goalInput">Sprint goal</label>
      <input id="goalInput" maxlength="200" value="${esc(V.sprint_goal)}" placeholder="e.g. A user can use the first feature end to end">
      <button class="small" id="goalSave" type="button">Save</button>
    </div>` : "";
  // Sprint Review: the demo is on screen as soon as the review starts.
  const demoBox = $("#demoLive");
  const reviewLive = V.meeting.type === "review" && V.meeting.opened;
  demoBox.hidden = !reviewLive;
  if (reviewLive && demoBox.dataset.sprint !== String(V.current_sprint)) {
    demoBox.dataset.sprint = V.current_sprint;
    demoBox.innerHTML = `<h3>Sprint ${V.current_sprint} demo</h3>
      <iframe class="demo-frame small-frame" title="Prototype after Sprint ${V.current_sprint}" sandbox src="api/prototype/${V.current_sprint}?v=${Date.now()}"></iframe>
      <button type="button" class="small ghost" id="demoBig">Open full size</button>`;
    $("#demoBig").addEventListener("click", () => { demoSprint = V.current_sprint; document.querySelector('#tabs button[data-tab="demo"]').click(); setView("work"); });
  }
  if (!reviewLive) demoBox.dataset.sprint = "";
  // Refinement / planning: the stories with their numbers and points, so you can say "story 2".
  const storyBox = $("#storiesLive");
  const showStories = ["refinement", "planning"].includes(V.meeting.type) && V.meeting.live;
  const showLast = V.meeting.type === "daily" && V.meeting.live && V.last_standup.length;
  storyBox.hidden = !showStories && !showLast;
  if (showLast) {
    storyBox.innerHTML = `<h3>Last stand-up</h3><ul class="mini-stories">${V.last_standup.map((x) =>
      `<li><b>${esc(x.name)}</b> (${esc(x.title)}): ${esc(x.text)}</li>`).join("")}</ul>`;
  }
  if (showStories) {
    const list = V.cards.filter((x) => x.status === "backlog" || (x.status === "todo" && x.sprint === V.current_sprint));
    storyBox.innerHTML = `<h3>Stories</h3><ul class="mini-stories">${list.map((x) =>
      `<li><b>Story ${x.id}</b> ${esc(x.title)} <span class="pts">${x.points || "?"} pts</span>${x.status === "todo" ? ' <span class="chip">in sprint</span>' : ""}</li>`).join("")}</ul>
      <p class="hint">Points = relative effort (1 tiny … 8 very big). Team capacity: about ${V.charts.capacity} points per sprint.</p>`;
  }
  $("#agenda").innerHTML = `<h3>Agenda</h3>
    <dl><dt>Inputs</dt><dd>${esc(c.inputs)}</dd><dt>Outputs</dt><dd>${esc(c.outputs)}</dd>
    <dt>Timebox</dt><dd>${esc(c.timebox)}</dd></dl>
    ${V.project.role === "sm" ? "" : `<p class="job"><strong>Your job as ${esc(c.role)}:</strong> ${esc(c.your_job)}</p>`}${goalBox}`;
  const gs = $("#goalSave");
  if (gs) gs.addEventListener("click", () => act(() => api("api/goal", { goal: $("#goalInput").value })));

  $("#tasks").innerHTML = V.tasks.map((t) => `<li class="${t.done ? "done" : ""}">${esc(t.text)}</li>`).join("");
  const done = V.tasks.filter((t) => t.done).length;
  $("#taskPill").innerHTML = V.project.complete || !V.tasks.length ? "" :
    `Your to-dos: <b>${done} of ${V.tasks.length} done</b>`;
  if (!V.tasks.length) $("#tasks").innerHTML = '<li class="done">Nothing for you right now.</li>';

  renderAfter();

  $("#notes").innerHTML = V.meeting.messages.map((m) => m.who === "system"
    ? `<li><em>${esc(m.text)}</em></li>`
    : `<li><b>${m.who === "user" ? esc(myName()) : esc(m.name)}:</b> ${esc(m.text)}</li>`).join("")
    || "<li><em>Nothing said yet.</em></li>";
}

function renderAfter() {
  // During a meeting: the call. Between meetings: what was decided and what's next.
  const live = V.meeting.live;
  $("#liveArea").hidden = !live;
  $("#afterArea").hidden = live;
  if (live) return;
  const sum = V.meeting.summary || [];
  const name = V.meeting.count ? V.ceremony.label : "";
  $("#afterTitle").textContent = V.project.complete ? "Project complete: 3 sprints done"
    : V.meeting.count ? `${name} finished` : "Your team is ready";
  $("#afterSummary").innerHTML = sum.map((l) => `<li>${esc(l)}</li>`).join("");
  $("#afterSummary").hidden = !sum.length;
  const next = V.menu.filter((m) => m.enabled).sort((a, b) => b.suggested - a.suggested);
  $("#afterNextLabel").textContent = next.length ? "What's next:" : "";
  $("#afterActions").innerHTML = next.map((m) =>
    `<button type="button" data-meet="${m.type}" class="${m.suggested ? "primary" : ""}">Start ${esc(m.label)}${m.suggested ? " (suggested)" : ""}</button>`).join("")
    + (V.meeting.count ? '<button type="button" class="ghost" data-goview="work">Open Team workspace</button>' : "");
}

$("#afterActions").addEventListener("click", (e) => {
  const v = e.target.closest("[data-goview]");
  if (v) { setView("work"); return; }
  const b = e.target.closest("[data-meet]");
  if (!b) return;
  handUp = false;
  act(() => api("api/meeting/open", { type: b.dataset.meet }), "The team is joining...")
    .then(() => setTimeout(pump, 300));
});

$("#nothingBtn").addEventListener("click", () => passTurn());

// Skip your turn: Sam covers it (as a suggestion you can change) and the meeting moves on.
function passTurn() {
  if (!V || !V.meeting.awaiting) return;
  if (V.meeting.awaiting.kind === "floor_answer") {
    $("#msgInput").value = "Nothing from me.";
    $("#composer").requestSubmit();
    return;
  }
  touch();
  act(() => api("api/meeting/pass", {}), null).then(() => { if (V && V.meeting.live) setTimeout(pump, 500); });
}

function renderControls() {
  if (!V) return;
  const live = V.meeting.live && !V.project.complete;
  const hasText = !!$("#msgInput").value.trim();
  $("#msgInput").disabled = !live || busy;
  $("#msgInput").readOnly = listening;
  $("#sendBtn").hidden = listening;                       // while talking, "Done talking" sends
  $("#sendBtn").disabled = !live || busy || !hasText;
  $("#micBtn").disabled = !live || busy;
  $("#endBtn").disabled = !live || busy;
  $("#handBtn").hidden = listening || !!V.meeting.awaiting;   // no need to raise a hand on your turn
  $("#handBtn").disabled = !live || busy;
  $("#nothingBtn").hidden = !(V.meeting.awaiting && V.meeting.awaiting.kind === "floor_answer") || listening;
  $("#handBtn").setAttribute("aria-pressed", String(handUp));
  $("#handBtn").textContent = handUp ? "Lower hand" : "Raise hand";
  $("#micBtn").setAttribute("aria-pressed", String(listening));
  $("#micBtn").innerHTML = listening ? "Done talking" : '<span aria-hidden="true">&#127908;</span> Talk';
  $("#soundBtn").setAttribute("aria-pressed", String(soundOn));
  $("#soundBtn").textContent = soundOn ? "Sound on" : "Sound off";
  $("#speedBtn").textContent = `Speed: ${["slow", "normal", "fast"][speedIdx] || "normal"}`;
  document.querySelector('.tile[data-role="user"]')?.classList.toggle("listening", listening);
  renderPicker();
}

function renderGuide() {
  const g = $("#guide");
  const m = V.meeting;
  const sug = V.menu.find((x) => x.suggested);
  const todo = V.tasks.find((t) => !t.done);
  const name = m.count ? V.ceremony.label.replace(/ \(.*/, "") : "Kickoff";
  let lead, rest;
  if (V.project.complete) {
    lead = "You finished all 3 sprints.";
    rest = "Open Team workspace to see your charts and the final demo.";
  } else if (m.live && m.awaiting) {
    lead = "Your turn.";
    rest = m.awaiting.prompt.replace(/^Your turn:\s*/i, "");
  } else if (m.live) {
    lead = `${name} is on.`;
    const sm = (V.team.find((t) => t.role === "sm") || {}).name || "The Scrum Master";
    rest = `${sm}, the Scrum Master, leads. When it's your turn the team waits for you. Raise your hand any time to jump in. At the end ${sm} asks for last questions, sums up and closes.`;
    if (V.project.role === "sm") rest = "You lead.";
  } else if (m.summary && !V.project.complete) {
    if (!todo) { g.hidden = true; return; }
    lead = "Your to-do:";
    rest = `${todo.text.replace(/ \(.*\)$/, "")} (Team workspace).`;
  } else if (m.invalid) {
    lead = `That wasn't the right meeting yet.`;
    rest = `Your teammate explained why.${sug ? ` Try ${sug.label} (green button at the top).` : ""}`;
  } else if (todo) {
    lead = `${name} is over.`;
    rest = `Your to-do now: ${todo.text.replace(/ \(.*\)$/, "")}. Open Team workspace to do it, then start the next meeting${sug ? ` (suggested: ${sug.label})` : ""}.`;
  } else if (m.count) {
    lead = `${name} is over.`;
    rest = `Start the next meeting from the buttons at the top${sug ? `. Suggested: ${sug.label}` : ""}.`;
  } else {
    lead = "Welcome!";
    rest = "Your team is ready. Start the kickoff below.";
  }
  g.hidden = false;
  g.innerHTML = `<b>${esc(lead)}</b> ${esc(rest)}`;
}

function renderNotice() {
  const n = $("#notice");
  const m = V.mode;
  let html = "";
  let cls = "notice";
  if (V.project.complete) {
    cls += " done";
    html = `<strong>You finished all 3 sprints.</strong> Velocity: ${Object.entries(V.charts.velocity)
      .map(([s, p]) => `Sprint ${s}: ${p} pts`).join(", ")}. See the Charts and Demo tabs.
      <a href="${esc(m.contact_url)}" target="_blank" rel="noopener">Get in touch with the maker</a>`;
  } else if (m.scripted_reason === "budget") {
    html = `Today's free AI budget is used up, so your teammates are reading from a script for now.
      Come back tomorrow for the live AI team. Enjoying it? <a href="${esc(m.contact_url)}" target="_blank" rel="noopener">Let's connect</a>`;
  } else if (m.scripted_reason === "error") {
    html = `The live AI didn't answer, so a scripted line was used. Reason: ${esc(m.last_error)}`;
  } else if (m.scripted_reason === "no_key") {
    html = "Teammates are using scripted lines because no Claude API key is set. Add <code>ANTHROPIC_API_KEY</code> to the <code>.env</code> file and restart for the live AI team.";
  }
  n.className = cls;
  n.innerHTML = html;
  n.hidden = !html;
}

/* ======================= meeting actions ======================= */
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("#msgInput").value.trim();
  if (!text) return;
  if (busy) {
    // Someone's turn is loading: hold your words and send them the moment it's done.
    userTyping = true;
    showCaption({ who: "user", name: myName(), title: V.user_role_title, text, live: true });
    setTimeout(() => $("#composer").requestSubmit(), 400);
    return;
  }
  $("#msgInput").value = "";
  stopTeam();
  queue.length = 0;
  handUp = false;
  floorGiven = false;
  userTyping = false;
  showCaption({ who: "user", name: myName(), title: V.user_role_title, text });
  setSpeaking("user");
  lastSaid = text;
  showLastSaid({ who: "user", text });
  act(() => api("api/meeting/say", { text }), null).then((ok) => {
    setSpeaking(null);
    // Not delivered (server said no, connection dropped): give your words back so nothing is lost.
    if (ok === false && !$("#msgInput").value) { $("#msgInput").value = text; renderControls(); }
    // Teammates with a raised hand speak one at a time.
    if (V && V.meeting.live) setTimeout(pump, V.meeting.hands.length ? 1300 : 500);
  });
});

$("#msgInput").addEventListener("input", () => {
  $("#sendBtn").disabled = !V || !V.meeting.live || busy || !$("#msgInput").value.trim();
  const typing = !!$("#msgInput").value.trim();
  if (typing && !userTyping) { userTyping = true; stopTeam(); }
  if (!typing && userTyping) { userTyping = false; setTimeout(pump, 800); }
});

$("#handBtn").addEventListener("click", () => {
  handUp = !handUp;
  if (!handUp) floorGiven = false;
  renderMeeting();
  renderControls();
  if (handUp && !playing && !busy) giveFloor();
  else if (!handUp) setTimeout(pump, 300);
  else idleCaption();
});
$("#endBtn").addEventListener("click", () => {
  const b = $("#endBtn");
  if (b.dataset.armed !== "1") {            // first click only asks: a stray click can't close the meeting
    b.dataset.armed = "1";
    b.textContent = "Click again to end";
    setTimeout(() => { b.dataset.armed = ""; b.textContent = "End meeting"; }, 3500);
    return;
  }
  b.dataset.armed = "";
  b.textContent = "End meeting";
  silence();
  act(() => api("api/meeting/end", {}), "Wrapping up...");
});
setInterval(() => {
  if (!V) return;
  const t0 = meetingStart[V.ceremony.index];
  $("#clock").classList.toggle("off", !V.meeting.live);
  if (!t0 || !V.meeting.live) return;
  const s = Math.floor((Date.now() - t0) / 1000);
  $("#clock").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}, 1000);

/* ======================= views + tabs ======================= */
function setView(name) {
  view = name;
  document.querySelectorAll(".switch button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === name)));
  $("#meetingView").hidden = name !== "meeting";
  $("#workView").hidden = name !== "work";
  if (name === "work") renderPanels();
}
document.querySelector(".switch").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-view]");
  if (b) setView(b.dataset.view);
});

$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (!b) return;
  tab = b.dataset.tab;
  document.querySelectorAll("#tabs button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
  document.querySelectorAll(".panel").forEach((p) => (p.hidden = p.id !== "panel-" + tab));
  renderPanels();
});

function renderPanels() {
  if (!V || view !== "work") return;
  ({ board: renderBoard, backlog: renderBacklog, charts: renderCharts,
    demo: renderDemo, scenes: renderScenes })[tab]();
}

/* ======================= board ======================= */
function sticky(c) {
  const cls = (c.type === "bug" ? "bug" : c.status === "done" ? "done" : "") + (c.type === "test" ? " testt" : "");
  const res = c.test_result ? `<span class="chip ${c.test_result}">${c.test_result === "pass" ? "Test passed" : "Test failed"}</span>` : "";
  const acc = c.accepted === true ? '<span class="chip">Accepted</span>' : "";
  return `<button class="sticky ${cls}" data-card="${c.id}">
    <span class="t">${esc(c.title)}</span>
    <span class="meta"><span class="chip">${c.type === "test" ? `T${c.test_for}` : `#${c.id}`}</span>${c.type === "test"
      ? `<span class="chip">${c.points || "?"} pts</span><span class="chip">Test story for #${c.test_for}</span>${c.test_cases ? '<span class="chip">Cases written</span>' : ""}`
      : `<span class="chip">${c.points || "?"} pts</span>`}${res}${acc}</span>
  </button>`;
}

// Which sprint the Board and Charts tabs show. Past sprints are read-only.
let viewSprint = null;
function shownSprint() {
  if (viewSprint === null || viewSprint > V.current_sprint) viewSprint = V.current_sprint;
  return viewSprint;
}
function sprintPicker() {
  if (V.current_sprint < 2) return "";
  return `<div class="sprint-pick">${Array.from({ length: V.current_sprint }, (_, i) => i + 1).map((n) =>
    `<button class="small" data-view-sprint="${n}" aria-pressed="${n === viewSprint}">Sprint ${n}${n === V.current_sprint ? " (now)" : ""}</button>`).join("")}</div>`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-view-sprint]");
  if (!b) return;
  viewSprint = +b.dataset.viewSprint;
  renderBoard();
  renderCharts();
});

function meetingHistory(s) {
  const list = (V.history || []).filter((m) => m.sprint === s);
  if (!list.length) return "";
  return `<h3 class="history-h">Meetings in Sprint ${s}</h3><div class="history">${list.map((m) => `
    <details><summary>${esc(m.label)}</summary>
      ${m.summary.length ? `<ul class="minutes">${m.summary.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : '<p class="hint">No minutes.</p>'}
      ${m.messages.length ? `<details><summary>Full conversation (${m.messages.length} lines)</summary>
        <div class="transcript">${m.messages.map((x) => `<p><strong>${esc(x.name)}:</strong> ${esc(x.text)}</p>`).join("")}</div></details>` : ""}
    </details>`).join("")}</div>`;
}

function renderBoard() {
  const s = shownSprint();
  const past = s !== V.current_sprint;
  const cards = V.cards.filter((c) => c.sprint === s);
  const cols = ["todo", "inprogress", "testing", "done"];
  const goalText = (V.project.sprint_goals || {})[String(s)] || "";
  const goal = goalText ? `<div class="goal-line"><strong>Sprint ${s} goal:</strong> ${esc(goalText)}</div>` : "";
  const vel = V.charts.velocity[String(s)];
  const pastNote = past ? `<p class="hint">Sprint ${s} is finished (read-only)${vel != null ? `: ${vel} points accepted` : ""}.</p>` : "";
  const empty = !cards.length ? `<p class="hint">Sprint ${s} is empty. Stories are pulled in from the backlog at Sprint Planning.</p>` : "";
  $("#panel-board").innerHTML = `${sprintPicker()}${goal}${pastNote}${empty}${past ? "" : `<p class="hint">${boardHint()}</p>`}
    <div class="columns">${cols.map((st) => {
      const list = cards.filter((c) => c.status === st);
      const pts = list.reduce((a, c) => a + (c.points || 0), 0);
      return `<div class="col"><h4>${esc(V.statuses[st])} <span>${pts} pts</span></h4>${list.map(sticky).join("")}</div>`;
    }).join("")}</div>${meetingHistory(s)}`;
}

function boardHint() {
  return {
    dev: "Click a sticky to estimate it, add pseudo-code notes and move it along.",
    tester: "Click a sticky in Testing to write test cases and pass or fail it.",
    po: "Click a sticky to read it. At the Sprint Review you accept or reject Done stories.",
    sm: "Click a sticky to read it. Watch for blockers and cards that don't move.",
  }[V.project.role];
}

/* ======================= backlog ======================= */
function renderBacklog() {
  const role = V.project.role;
  const planning = V.project.kickoff_done && V.sprint.state === "unplanned";
  const planningLive = V.meeting.type === "planning" && V.meeting.live;
  // The PO can add a story to the current sprint any time (also mid-sprint); anyone can during Sprint Planning.
  const canAdd = V.project.kickoff_done && !V.project.complete && V.sprint.state !== "reviewed"
    && (role === "po" || planningLive);
  const backlog = V.cards.filter((c) => c.status === "backlog" && c.type !== "test");
  const testOf = (id) => V.cards.find((t) => t.test_for === id);
  const inSprint = V.cards.filter((c) => c.sprint === V.current_sprint && c.status === "todo" && c.type !== "test");
  const add = role === "po" && !V.project.complete ? `
    <form class="add-story" id="addStory">
      <strong>Add a user story</strong>
      <input id="storyTitle" maxlength="120" required placeholder="As a user, I want to ..., so that ...">
      <textarea id="storyAc" rows="2" maxlength="600" placeholder="Acceptance criteria: how we'll know it's done"></textarea>
      <button class="primary small" type="submit">Add story</button>
    </form>` : "";
  const row = (c, i) => {
    const btns = [];
    if (role === "po") {
      btns.push(`<button class="small ghost" data-prio="up" data-id="${c.id}" aria-label="Move up">Up</button>`);
      btns.push(`<button class="small ghost" data-prio="down" data-id="${c.id}" aria-label="Move down">Down</button>`);
      btns.push(`<button class="small ghost danger" data-del="${c.id}" aria-label="Delete story ${c.id}">Delete</button>`);
    }
    if (canAdd) btns.push(`<button class="small" data-sprint="add" data-id="${c.id}">Add to Sprint ${V.current_sprint}</button>`);
    if (role === "dev") {
      btns.push(`<label class="sr" for="est${c.id}">Estimate</label><select id="est${c.id}" data-est="${c.id}">
        <option value="">Estimate</option>${[1, 2, 3, 5, 8, 13].map((p) =>
          `<option ${c.points === p ? "selected" : ""}>${p}</option>`).join("")}</select>`);
    }
    btns.push(`<button class="small ghost" data-card="${c.id}">Open</button>`);
    return `<li><span class="rank">${i + 1}</span><div><strong>Story ${c.id}: ${esc(c.title)}</strong> ${c.type === "bug" ? "(bug)" : ""}
      <div class="ac">${esc(c.acceptance) || "<em>No acceptance criteria yet</em>"}</div>
      ${testOf(c.id) ? `<div class="ac"><button class="small ghost" data-card="${testOf(c.id).id}">+ Test story T${c.id} · ${testOf(c.id).points || "?"} pts</button> (tester's own story and estimate, moves with it)</div>` : ""}</div>
      <div class="actions"><span class="pts">${c.points || "?"} pts</span>${btns.join("")}</div></li>`;
  };
  const sprintRows = inSprint.map((c) => `<li><span class="rank">S${V.current_sprint}</span><div><strong>${esc(c.title)}</strong></div>
      <div class="actions"><span class="pts">${c.points || "?"} pts</span>${canAdd
        ? `<button class="small ghost" data-sprint="remove" data-id="${c.id}">Back to backlog</button>` : ""}</div></li>`).join("");
  // Finished work stays visible: newest sprint first, so after a few sprints you can still see what was done when.
  const doneCards = V.cards.filter((c) => c.status === "done" && c.type !== "test")
    .sort((a, b) => (b.sprint || 0) - (a.sprint || 0) || a.id - b.id);
  const doneRows = doneCards.map((c) => {
    const verdict = c.accepted === true ? " · accepted" : c.accepted === false ? " · rejected" : "";
    return `<li class="done-row"><span class="rank">&#10003;</span><div><strong>${c.type === "bug" ? "Bug" : "Story"} ${c.id}: ${esc(c.title)}</strong>
      <div class="ac">Done in Sprint ${c.sprint || "?"}${verdict}</div></div>
      <div class="actions"><span class="pts">${c.points || "?"} pts</span><button class="small ghost" data-card="${c.id}">Open</button></div></li>`;
  }).join("");
  const committed = inSprint.reduce((a, c) => a + (c.points || 0) + ((testOf(c.id) || {}).points || 0), 0);
  $("#panel-backlog").innerHTML = `${add}
    <h3>Product backlog</h3>
    <p class="hint">Ordered by priority: the top item is the most valuable.${role === "po" && planning ? " Pull stories into the sprint during Sprint Planning." : ""}${role === "dev" ? " Estimate with Fibonacci story points." : ""}</p>
    <p class="hint"><b>Story points</b> = relative effort compared with the other stories (1 tiny, 2 small, 3 medium, 5 big, 8 very big), given by the developer in refinement. <br><b>${esc(V.charts.capacity_note)}</b></p>
    <ul class="backlog-list">${backlog.map(row).join("") || '<li><span></span><span class="hint">The backlog is empty.</span></li>'}</ul>
    ${planning || canAdd ? `<h3 style="margin-top:18px">In Sprint ${V.current_sprint} (${committed} of ~${V.charts.capacity} pts capacity)</h3>
    <ul class="backlog-list">${sprintRows || '<li><span></span><span class="hint">Nothing pulled in yet.</span></li>'}</ul>` : ""}
    ${doneRows ? `<h3 style="margin-top:18px">Completed (${doneCards.length})</h3>
    <ul class="backlog-list">${doneRows}</ul>` : ""}`;
  const f = $("#addStory");
  if (f) f.addEventListener("submit", (e) => {
    e.preventDefault();
    act(() => api("api/card", { title: $("#storyTitle").value, acceptance: $("#storyAc").value }));
  });
}

document.addEventListener("click", (e) => {
  const card = e.target.closest("[data-card]");
  if (card) return openCard(+card.dataset.card);
  const pr = e.target.closest("[data-prio]");
  if (pr) return act(() => api(`api/card/${pr.dataset.id}/priority`, { dir: pr.dataset.prio }));
  const del = e.target.closest("[data-del]");
  if (del) {
    const c = V.cards.find((x) => x.id === +del.dataset.del);
    if (c && confirm(`Delete story ${c.id} "${c.title}"?`)) act(() => api(`api/card/${c.id}/delete`, {}));
    return;
  }
  const sp = e.target.closest("[data-sprint]");
  if (sp) return act(() => api(`api/card/${sp.dataset.id}/sprint`, { add: sp.dataset.sprint === "add" }));
});
document.addEventListener("change", (e) => {
  const sel = e.target.closest("[data-est]");
  if (sel && sel.value) act(() => api(`api/card/${sel.dataset.est}/edit`, { points: +sel.value }));
});

/* ======================= card dialog ======================= */
function openCard(id) {
  const c = V.cards.find((x) => x.id === id);
  if (!c) return;
  const role = V.project.role;
  const ro = (label, val) => `<div><strong style="font-size:14px">${label}</strong><div class="readonly">${esc(val) || "<em>Empty</em>"}</div></div>`;
  let body = "";
  if (role === "po" && c.status !== "done") {
    body += `<label>Title<input name="title" value="${esc(c.title)}" maxlength="120"></label>
      <label>Acceptance criteria<textarea name="acceptance" rows="3" maxlength="600">${esc(c.acceptance)}</textarea></label>`;
  } else {
    body += ro("Acceptance criteria", c.acceptance);
  }
  const isTicket = c.type === "test";
  if (isTicket) {
    if (role === "tester" && c.status !== "done") body += `<label>Test story points (your estimate: effort to write and run the tests)
      <select name="points"><option value="">?</option>${[1, 2, 3, 5, 8].map((p) =>
        `<option ${c.points === p ? "selected" : ""}>${p}</option>`).join("")}</select></label>`;
    body += `<p class="hint"><b>${c.points ? `${c.points} points` : "Not estimated yet"}</b>${c.estimated_by ? `, estimated by ${esc(c.estimated_by.replace(/^Tester: /, ""))}` : ""}.
      The tester's own user story for story #${c.test_for}, with its own status.
      Move it to In Progress and write the test cases (with sample data) while the developer builds. Move it to Testing
      to run them; once story #${c.test_for} is built, click Pass or Fail here.</p>`;
  } else if (role === "dev" && c.status !== "done") {
    body += `<label>Story points<select name="points"><option value="">?</option>${[1, 2, 3, 5, 8, 13].map((p) =>
      `<option ${c.points === p ? "selected" : ""}>${p}</option>`).join("")}</select></label>
      <label>Developer notes (pseudo-code)<textarea name="dev_notes" rows="5" maxlength="1500"
        placeholder="1. Build the form&#10;2. Validate input&#10;3. Call the login API...">${esc(c.dev_notes)}</textarea></label>`;
  } else {
    body += ro("Developer notes", c.dev_notes);
  }
  if (role === "tester" && c.status !== "done") {
    body += `<label>Test cases (write them early, from the acceptance criteria, with sample data)<textarea name="test_cases" rows="5" maxlength="1500"
        placeholder="TC1 Happy path - data: 2 items, tomorrow 10:00. Expected: order placed&#10;TC2 Empty cart - expected: clear error...">${esc(c.test_cases)}</textarea></label>
      ${c.status === "testing" ? '<label>Test note<input name="note" maxlength="400" placeholder="What did you find?"></label>' : ""}`;
  } else {
    body += ro("Test cases", c.test_cases);
  }
  if (c.test_note) body += ro("Test result note", `${c.test_result || ""} ${c.test_note}`);
  if ((c.discussion || []).length) body += `<div><strong style="font-size:14px">Discussion (from meetings)</strong>
    <ul class="discussion">${c.discussion.map((d) => `<li>${esc(d)}</li>`).join("")}</ul></div>`;
  if (!isTicket) body += `<p class="hint">${c.points ? `<b>${c.points} story points</b>, estimated by ${esc(c.estimated_by || "the team")}.`
    : "<b>Not estimated yet.</b> The developer estimates in Backlog Refinement."} Points are relative effort vs the other stories (1 tiny … 8 very big), not hours.</p>`;

  const actions = ['<button value="cancel" class="ghost">Close</button>'];
  const canEdit = c.status !== "done" && ["po", "dev", "tester"].includes(role);
  if (canEdit) actions.push('<button value="save" class="primary">Save</button>');
  if (role === "po" && c.status === "backlog") {
    actions.push('<button value="split">Split in two</button>');
    actions.push('<button value="delete" class="danger">Delete story</button>');
  }
  if (role === "dev" || (role === "tester" && /\b(test|tests|testing|qa|regression)\b/i.test(c.title))) {
    const moves = (isTicket ? { todo: ["inprogress"], inprogress: ["todo", "testing"], testing: ["inprogress"] }
      : { todo: ["inprogress"], inprogress: ["todo", "testing"] })[c.status] || [];
    moves.forEach((m) => actions.push(`<button value="move:${m}">Move to ${esc(V.statuses[m])}</button>`));
  }
  if (role === "tester" && c.status === "testing") {
    actions.push('<button value="test:pass" class="primary">Pass</button>');
    actions.push('<button value="test:fail">Fail</button>');
    actions.push('<button value="bug">Log a bug</button>');
  }
  if (role === "po" && c.status === "done" && c.accepted === null && V.ceremony.type === "review") {
    actions.push('<button value="accept:1" class="primary">Accept</button>');
    actions.push('<button value="accept:0">Reject</button>');
  }
  $("#cardForm").innerHTML = `<h3>${isTicket ? `T${c.test_for}` : `#${c.id}`} ${esc(c.title)}</h3>
    <div class="row"><span class="chip">${esc(V.statuses[c.status])}</span>${isTicket ? `<span class="chip">Test story</span><span class="chip">${c.points || "?"} pts</span>` : `<span class="chip">${c.points || "?"} pts</span>`}
    ${c.type === "bug" ? '<span class="chip">Bug</span>' : ""}</div>${body}<div class="row">${actions.join("")}</div>`;
  const dlg = $("#cardDialog");
  dlg.dataset.id = id;
  dlg.showModal();
}

$("#cardDialog").addEventListener("close", () => {
  const dlg = $("#cardDialog");
  const v = dlg.returnValue;
  const id = dlg.dataset.id;
  if (!v || v === "cancel") return;
  if (v === "delete") return act(() => api(`api/card/${id}/delete`, {}));
  if (v === "split") return act(() => api(`api/card/${id}/split`, {}));
  const f = new FormData($("#cardForm"));
  const fields = {};
  for (const [k, val] of f.entries()) fields[k] = val;
  if (fields.points) fields.points = +fields.points; else delete fields.points;
  act(async () => {
    if (v === "save" || v.startsWith("move:") || v.startsWith("test:")) {
      if (Object.keys(fields).length) await api(`api/card/${id}/edit`, fields);
    }
    if (v.startsWith("move:")) return api(`api/card/${id}/move`, { to: v.slice(5) });
    if (v.startsWith("test:")) return api(`api/card/${id}/test`, { result: v.slice(5), test_cases: fields.test_cases, note: fields.note });
    if (v === "bug") return api(`api/card/${id}/bug`, { title: fields.note || "Problem found while testing", description: fields.test_cases || "" });
    if (v.startsWith("accept:")) return api(`api/card/${id}/accept`, { accepted: v.endsWith("1") });
    return api("api/state");
  });
});

/* ======================= charts (hand-made SVG) ======================= */
const C_DONE = "#1d8a55", C_SCOPE = "#2a6fdb", C_REF = "#9aa8ae";

function axes(w, h, pad, yMax, ticks) {
  let g = "";
  for (let i = 0; i <= ticks; i++) {
    const v = Math.round((yMax / ticks) * i);
    const y = h - pad.b - ((h - pad.t - pad.b) * i) / ticks;
    g += `<line x1="${pad.l}" x2="${w - pad.r}" y1="${y}" y2="${y}" stroke="#e3e9e7"/>
      <text x="${pad.l - 6}" y="${y + 4}" text-anchor="end">${v}</text>`;
  }
  return g;
}

function lineChart(series, labels, yMax, opts = {}) {
  const w = 420, h = 220, pad = { l: 34, r: 56, t: 12, b: 26 };
  const n = labels.length;
  const x = (i) => pad.l + (n === 1 ? 0 : ((w - pad.l - pad.r) * i) / (n - 1));
  const y = (v) => h - pad.b - ((h - pad.t - pad.b) * v) / (yMax || 1);
  let svg = axes(w, h, pad, yMax || 1, 4);
  labels.forEach((l, i) => {
    if (n <= 8 || i % Math.ceil(n / 8) === 0 || i === n - 1)
      svg += `<text x="${x(i)}" y="${h - 8}" text-anchor="middle">${esc(l)}</text>`;
  });
  series.forEach((s) => {
    const pts = s.values.map((v, i) => (v == null ? null : [x(i), y(v), v, labels[i]])).filter(Boolean);
    if (!pts.length) return;
    svg += `<polyline fill="none" stroke="${s.color}" stroke-width="2" ${s.dash ? 'stroke-dasharray="5 4"' : ""}
      points="${pts.map((p) => p[0] + "," + p[1]).join(" ")}"/>`;
    if (!s.dash) pts.forEach((p) => {
      svg += `<circle class="dot" cx="${p[0]}" cy="${p[1]}" r="4.5" fill="${s.color}" stroke="#fff" stroke-width="2">
        <title>${esc(s.name)} · ${esc(p[3])}: ${p[2]} pts</title></circle>`;
    });
    const last = pts.at(-1);
    if (!s.dash) svg += `<text x="${last[0] + 8}" y="${last[1] + 4}" style="fill:var(--ink)">${esc(s.short || s.name)}</text>`;
  });
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(opts.label || "")}">${svg}</svg>`;
}

function renderCharts() {
  const ch = V.charts;
  const s = shownSprint();
  const bd = ch.burndown[String(s)] || [];
  const steps = bd.map((p) => p.label);
  const actual = bd.map((p) => p.remaining);
  const start = actual[0] ?? 0;
  const n = Math.max(1, steps.length - 1);
  const ideal = steps.map((_, i) => +(start - (start * i) / n).toFixed(1));
  const nice = (l) => l === "Plan" ? "Planned" : l.startsWith("SU") ? "Stand-up " + l.slice(2) : l;
  const bdMax = Math.max(5, start, ...actual.filter((v) => v != null));
  const burndown = bd.length ? lineChart([
    { name: "Ideal", short: "Ideal", values: ideal, color: C_REF, dash: true },
    { name: "Remaining", short: "Actual", values: actual, color: C_DONE },
  ], steps.map((l) => l === "Plan" ? "Plan" : l), bdMax, { label: `Sprint ${s} burndown` })
    : '<p class="hint">Starts after Sprint Planning.</p>';

  const bu = ch.burnup;
  const burnup = bu.length ? lineChart([
    { name: "Total scope", short: "Scope", values: bu.map((p) => p.scope), color: C_SCOPE },
    { name: "Done", short: "Done", values: bu.map((p) => p.done), color: C_DONE },
  ], bu.map((p) => p.label), Math.max(5, ...bu.map((p) => p.scope)), { label: "Project burn-up" })
    : '<p class="hint">Starts after Sprint Planning.</p>';

  const vel = [1, 2, 3].map((n) => ch.velocity[String(n)]);
  const vMax = Math.max(ch.capacity, ...vel.filter((v) => v != null)) + 2;
  const w = 420, h = 200, pad = { l: 34, r: 16, t: 14, b: 26 };
  const bw = 64, gap = (w - pad.l - pad.r - bw * 3) / 4;
  const yv = (v) => h - pad.b - ((h - pad.t - pad.b) * v) / vMax;
  let vs = axes(w, h, pad, vMax, 4);
  vel.forEach((v, i) => {
    const x = pad.l + gap + i * (bw + gap);
    vs += `<text x="${x + bw / 2}" y="${h - 8}" text-anchor="middle">Sprint ${i + 1}</text>`;
    if (v == null) return;
    const top = yv(v), bot = h - pad.b;
    vs += `<path class="bar" d="M${x} ${bot}V${top + 4}q0 -4 4 -4h${bw - 8}q4 0 4 4V${bot}z" fill="${C_DONE}">
      <title>Sprint ${i + 1}: ${v} points accepted</title></path>
      <text x="${x + bw / 2}" y="${top - 5}" text-anchor="middle" style="fill:var(--ink);font-weight:700">${v}</text>`;
  });
  const cy = yv(ch.capacity);
  vs += `<line x1="${pad.l}" x2="${w - pad.r}" y1="${cy}" y2="${cy}" stroke="${C_REF}" stroke-dasharray="5 4"/>
    <text x="${w - pad.r}" y="${cy - 5}" text-anchor="end">Capacity ${ch.capacity}</text>`;
  const avg = vel.filter((v) => v != null);

  const table = (head, rows) => `<details><summary>Show the numbers</summary><table><tr>${head.map((x) => `<th>${x}</th>`).join("")}</tr>
    ${rows.map((r) => `<tr>${r.map((x) => `<td>${x ?? "-"}</td>`).join("")}</tr>`).join("")}</table></details>`;

  $("#panel-charts").innerHTML = `${sprintPicker()}<div class="charts">
    <div class="chart"><h4>Sprint ${s} burndown</h4><p>Story points not yet Done, at each stand-up. Below the dashed line = ahead of plan.</p>
      <div class="legend"><span><i style="background:${C_DONE}"></i>Remaining</span><span><i style="background:${C_REF}"></i>Ideal</span></div>
      ${burndown}${table(["When", "Remaining", "Ideal"], steps.map((l, i) => [nice(l), actual[i], ideal[i]]))}</div>
    <div class="chart"><h4>Project burn-up</h4><p>Work Done against total scope. When the blue line jumps, scope grew.</p>
      <div class="legend"><span><i style="background:${C_SCOPE}"></i>Total scope</span><span><i style="background:${C_DONE}"></i>Done</span></div>
      ${burnup}${table(["Point", "Scope", "Done"], bu.map((p) => [p.label, p.scope, p.done]))}</div>
    <div class="chart"><h4>Velocity</h4><p>Points accepted per sprint${avg.length ? `, average ${(avg.reduce((a, b) => a + b, 0) / avg.length).toFixed(1)}` : ""}. Use it to plan the next sprint.</p>
      <svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Velocity per sprint">${vs}</svg>
      ${table(["Sprint", "Points"], vel.map((v, i) => [i + 1, v]))}</div>
  </div>`;
}

/* ======================= demo ======================= */
function renderDemo() {
  const max = V.current_sprint;
  if (demoSprint === null || demoSprint > max) demoSprint = max;
  const reviewReached = (n) => (String(n) in V.charts.velocity) || (n === V.sprint.number && V.sprint.state === "reviewed")
    || (n === V.sprint.number && V.meeting.type === "review" && V.meeting.opened) || V.project.complete;
  const btns = Array.from({ length: max }, (_, i) => i + 1).map((n) =>
    `<button class="small" data-demo="${n}" aria-pressed="${n === demoSprint}">After Sprint ${n}</button>`).join("");
  const ready = reviewReached(demoSprint);
  $("#panel-demo").innerHTML = `<p class="hint">A clickable mock-up of what the team built. It grows every sprint. It is a prototype, not a working app.</p>
    <div class="sprint-pick">${btns}</div>
    ${ready ? `<iframe class="demo-frame" title="Prototype after Sprint ${demoSprint}" sandbox src="api/prototype/${demoSprint}?v=${Date.now()}"></iframe>`
      : `<p class="hint">The Sprint ${demoSprint} demo is shown at the Sprint Review.</p>`}`;
  document.querySelectorAll("[data-demo]").forEach((b) => b.addEventListener("click", () => {
    demoSprint = +b.dataset.demo; renderDemo();
  }));
}

/* ======================= behind the scenes (MCP log) ======================= */
function renderScenes() {
  const log = V.mcp_log.slice().reverse();
  $("#panel-scenes").innerHTML = `
    <h3>How an AI teammate changes the board</h3>
    <div class="flow"><span>Agent (Claude) decides</span><b>&rarr;</b><span>MCP client carries the request</span><b>&rarr;</b><span>MCP board server does it</span><b>&rarr;</b><span>Board updates</span></div>
    <p class="hint">Every line below is a real MCP tool call (CallToolRequest) made by an AI teammate or by the work simulator.</p>
    <ul class="log">${log.map((l) => `<li><strong>${esc(l.agent)}</strong> called <code>${esc(l.tool)}(${esc(fmtArgs(l.args))})</code>
      <br><span class="${l.result.startsWith("ERROR") ? "err" : ""}">${esc(l.result)}</span></li>`).join("") || '<li class="hint">No tool calls yet.</li>'}</ul>`;
}

function fmtArgs(a) {
  return Object.entries(a || {}).map(([k, v]) => {
    let s = typeof v === "string" ? `"${v}"` : JSON.stringify(v);
    if (s.length > 40) s = s.slice(0, 37) + '..."';
    return `${k}=${s}`;
  }).join(", ");
}

init().catch((e) => toast(e.message));
