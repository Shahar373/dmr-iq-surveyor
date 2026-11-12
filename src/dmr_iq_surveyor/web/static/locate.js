/* Locate: the Phase 8 field app. Type a frequency, press Start, drive.
 *
 * A thin client over the server's Locate endpoints (docs/phase8-field-app-locate.md):
 *   POST /api/locate/target   choose the frequency; the Pi switches to its campaign
 *   POST /api/calibrate/gain  one minute parked, on the first Start of a target
 *   POST /api/live/start      the drive; GET /api/live for its HUD
 *   POST /api/locate/solve    the region, only when asked; GET /api/locate/result
 * Nothing is computed here that the server does not already decide. No build
 * step and no framework, like the classic app at /.
 */
"use strict";

const TOKEN = new URLSearchParams(location.search).get("token") || "";
const API_TIMEOUT_MS = 20000;
const POLL_MS = 2000;

const S = {
  settings: null,
  tilesViaPi: false,
  device: null,
  disk: null,
  gnss: null,
  active: null,          // the target in force on the Pi (describe_target)
  targets: [],
  screen: "home",
  previous: "home",
  draft: { emission: "continuous", bandwidth: 12500 },
  liveJobId: null,
  livePollTimer: null,
  driveWasRunning: false,
  calJobId: null,
  calTimer: null,
  resultTimer: null,
  solvedAt: null,
  watchId: null,
  lastFix: null,
  posting: false,
  gpsError: "",
  wakeLock: null,
  spoken: { text: "", at: 0 },
  announcedBins: 0,
  lastHeard: null,
  pullOverBin: null,
  gpsLost: false,
};

/* ------------------------------------------------------------- helpers */

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (TOKEN) headers["X-Auth-Token"] = TOKEN;
  if (options.body) headers["Content-Type"] = "application/json";
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs || API_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(path, Object.assign({}, options, { headers, signal: controller.signal }));
  } catch (err) {
    if (err && err.name === "AbortError") throw new Error("ה-Pi לא ענה בזמן");
    throw new Error("אין חיבור ל-Pi");
  } finally {
    clearTimeout(timer);
  }
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch (_) { payload = { error: text }; }
  if (!response.ok) {
    const error = new Error((payload && payload.error) || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function mhz(hz) {
  // At least three decimals, never trailing noise: 469.35e6 -> "469.350".
  let text = (Number(hz) / 1e6).toFixed(6).replace(/0+$/, "");
  const decimals = (text.split(".")[1] || "").length;
  if (decimals < 3) text = (Number(hz) / 1e6).toFixed(3);
  return text;
}

function num(value) {
  return `<span class="num">${value}</span>`;
}

const escapeHtml = (value) =>
  String(value === null || value === undefined ? "" : value).replace(
    /[&<>"']/g,
    (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch],
  );

function banner(message) {
  const node = $("#banner");
  node.hidden = !message;
  node.textContent = message || "";
}

function compass(deg) {
  const names = ["צפון", "צפון-מזרח", "מזרח", "דרום-מזרח", "דרום", "דרום-מערב", "מערב", "צפון-מערב"];
  return names[Math.round(((deg % 360) + 360) % 360 / 45) % 8];
}

function parseMaybeJson(value) {
  if (value && typeof value === "object") return value;
  if (typeof value === "string" && value) {
    try { return JSON.parse(value); } catch (_) { return null; }
  }
  return null;
}

/* --------------------------------------------------------- voice, screen */

let hebrewVoice = null;
function pickVoice() {
  if (!("speechSynthesis" in window)) return;
  const voices = window.speechSynthesis.getVoices();
  hebrewVoice = voices.find((voice) => /^he|^iw/i.test(voice.lang)) || null;
}
if ("speechSynthesis" in window) {
  pickVoice();
  window.speechSynthesis.onvoiceschanged = pickVoice;
}

/* Spoken in Hebrew when the phone has a Hebrew voice. Throttled so one
 * sentence is not repeated within twenty seconds, and an urgent one cuts in. */
function speak(text, { urgent = false } = {}) {
  if (!$("#voice").checked || !("speechSynthesis" in window)) return;
  const now = Date.now();
  if (S.spoken.text === text && now - S.spoken.at < 20000) return;
  S.spoken = { text, at: now };
  try {
    if (urgent) window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "he-IL";
    if (hebrewVoice) utterance.voice = hebrewVoice;
    utterance.rate = 1.05;
    window.speechSynthesis.speak(utterance);
  } catch (_) {
    /* speech is a convenience, never a dependency */
  }
}

async function holdScreenAwake() {
  if (!("wakeLock" in navigator) || S.wakeLock) return;
  try { S.wakeLock = await navigator.wakeLock.request("screen"); } catch (_) { /* may dim */ }
}
function releaseScreen() {
  if (S.wakeLock) { S.wakeLock.release().catch(() => {}); S.wakeLock = null; }
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && S.liveJobId) {
    S.wakeLock = null;
    holdScreenAwake();
  }
});

/* --------------------------------------------------------------- screens */

function show(name) {
  if (name !== S.screen) S.previous = S.screen;
  S.screen = name;
  $$("[data-screen]").forEach((node) => { node.hidden = node.dataset.screen !== name; });
  $("#hud").hidden = name !== "drive";
  $("#sheet").scrollTop = 0;
  if (name === "advanced") {
    fillAdvanced();
    // The run count (which locks the tuning) moves while a target is in force.
    loadTargets().then(() => { if (S.screen === "advanced") fillAdvanced(); });
  }
  if (name === "menu") fillMenu();
  if (map) setTimeout(() => map.invalidateSize(), 50);
}

/* ------------------------------------------------------------------- map */

let map = null;
const layers = {};
const LEAFLET_SOURCES = [
  { js: "/vendor/leaflet.js", css: "/vendor/leaflet.css" },
  { js: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js",
    css: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css" },
  { js: "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    css: "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" },
];

function loadTag(tag, attributes) {
  return new Promise((resolve, reject) => {
    const node = document.createElement(tag);
    Object.assign(node, attributes);
    node.onload = resolve;
    node.onerror = () => reject(new Error("could not load"));
    document.head.appendChild(node);
  });
}

async function ensureLeaflet() {
  for (const source of LEAFLET_SOURCES) {
    try {
      // The stylesheet first: a map drawn before its CSS lands is a mess of
      // full-size tiles, and on a slow link that is what the driver sees.
      await loadTag("link", { rel: "stylesheet", href: source.css }).catch(() => {});
      await loadTag("script", { src: source.js, async: false });
      if (window.L) return true;
    } catch (_) {
      /* next source */
    }
  }
  return false;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function initMap() {
  if (typeof L === "undefined") {
    banner("ספריית המפה לא נטענה, אז אין מפה. כל השאר עובד. הרץ פעם אחת את scripts/vendor_leaflet.sh כשיש אינטרנט.");
    return;
  }
  const settings = S.settings;
  map = L.map("map", { zoomControl: false, attributionControl: true })
    .setView(settings.map_center, settings.map_zoom);
  const tileUrl = S.tilesViaPi
    ? "/tiles/{z}/{x}/{y}.png" + (TOKEN ? "?token=" + encodeURIComponent(TOKEN) : "")
    : settings.tile_url;
  L.tileLayer(tileUrl, { attribution: settings.tile_attribution, maxZoom: 19 }).addTo(map);
  for (const name of ["regions", "track", "bins", "plan", "mode", "here"]) {
    layers[name] = L.layerGroup().addTo(map);
  }
}

function binColour(target) {
  if (!target || !target.heard) return cssVar("--off");
  const level = Math.max(0, Math.min(25, Number(target.level_db) || 0));
  const strength = Math.round(40 + 60 * (level / 25));
  return `color-mix(in srgb, ${cssVar("--ok")} ${strength}%, white)`;
}

function drawDrive(payload) {
  if (!map) return;
  layers.bins.clearLayers();
  for (const bin of payload.bins || []) {
    if (typeof bin.latitude !== "number") continue;
    const target = bin.target;
    L.circleMarker([bin.latitude, bin.longitude], {
      radius: target && target.heard ? 7 : 5,
      color: "#ffffff",
      weight: 1.5,
      fillColor: binColour(target),
      fillOpacity: 0.95,
    }).bindTooltip(
      target && target.heard ? `נשמע · ${target.level_db} dB` : "לא נשמע",
      { direction: "top" },
    ).addTo(layers.bins);
  }
  layers.track.clearLayers();
  const trail = (payload.trail || []).filter((p) => typeof p.latitude === "number");
  if (trail.length > 1) {
    L.polyline(trail.map((p) => [p.latitude, p.longitude]), {
      color: cssVar("--accent"), weight: 3, opacity: 0.45, dashArray: "2 6",
    }).addTo(layers.track);
  }
  const last = trail[trail.length - 1];
  layers.here.clearLayers();
  if (last) {
    L.marker([last.latitude, last.longitude], {
      icon: L.divIcon({ className: "", html: '<div class="here-marker"></div>', iconSize: [18, 18] }),
      interactive: false,
    }).addTo(layers.here);
    if (S.screen === "drive") map.panTo([last.latitude, last.longitude], { animate: true });
  }
}

function drawResult(result) {
  if (!map) return;
  layers.regions.clearLayers();
  layers.mode.clearLayers();
  layers.plan.clearLayers();
  if (!result || !result.solution) return;
  const regionColour = cssVar("--region");
  let bounds = null;
  if (result.regions && result.regions.features) {
    const features = result.regions.features
      .filter((f) => f.geometry)
      .sort((a, b) => b.properties.credible_level - a.properties.credible_level);
    for (const feature of features) {
      const ninety = feature.properties.credible_level >= 0.85;
      const layer = L.geoJSON(feature, {
        style: {
          color: regionColour, weight: 2, dashArray: ninety ? "6 4" : null,
          fillColor: regionColour, fillOpacity: ninety ? 0.14 : 0.3,
        },
        interactive: false,
      }).addTo(layers.regions);
      if (ninety) bounds = layer.getBounds();
    }
  }
  const solution = result.solution;
  if (solution.mode_latitude !== null && solution.mode_latitude !== undefined) {
    L.circleMarker([solution.mode_latitude, solution.mode_longitude], {
      radius: 5, color: "#ffffff", weight: 2, fillColor: regionColour, fillOpacity: 1,
    }).bindTooltip("התא הסביר ביותר, לא מיקום המשדר", { direction: "top" }).addTo(layers.mode);
  }
  const stops = (result.plan && result.plan.top_stops) || [];
  // The best place gets the label; the next two are only marked, so three
  // nearby suggestions do not stack their labels on top of each other.
  stops.slice(0, 3).forEach((stop, index) => {
    if (index === 0) {
      L.marker([stop.latitude, stop.longitude], {
        icon: L.divIcon({ className: "", html: '<div class="plan-marker">מדוד כאן</div>', iconSize: null }),
        interactive: false,
      }).addTo(layers.plan);
    } else {
      L.circleMarker([stop.latitude, stop.longitude], {
        radius: 9, color: cssVar("--accent"), weight: 2, dashArray: "3 4",
        fillColor: cssVar("--accent"), fillOpacity: 0.1,
      }).bindTooltip("גם כאן כדאי למדוד", { direction: "top" }).addTo(layers.plan);
    }
  });
  if (bounds && bounds.isValid() && S.screen === "result") {
    map.fitBounds(bounds.pad(0.25), { animate: true });
  }
}

/* ------------------------------------------------------------- state */

async function refreshState() {
  const payload = await api("/api/state", { timeoutMs: 10000 });
  S.settings = payload.settings;
  S.tilesViaPi = Boolean(payload.tiles_via_pi);
  S.device = payload.device;
  S.disk = payload.disk;
  S.gnss = payload.gnss || null;
  S.active = (payload.locate && payload.locate.active) || null;
  renderStatus();
  return payload;
}

async function loadTargets() {
  try {
    const payload = await api("/api/locate/targets");
    S.targets = payload.targets || [];
    if (payload.active) S.active = payload.active;
  } catch (_) {
    S.targets = [];
  }
  renderTargets();
}

function usbFresh() {
  const gnss = S.gnss;
  return Boolean(gnss && gnss.state === "fix" && gnss.fix_age_s !== null && gnss.fix_age_s <= 5);
}

function phoneFresh() {
  return Boolean(S.lastFix && Date.now() - S.lastFix.at < 10000);
}

function renderStatus() {
  const pills = [];
  const device = S.device || {};
  if (device.state === "available") pills.push(["", `מקלט ${escapeHtml(device.resolved_label || "")}`.trim()]);
  else if (device.state === "busy") pills.push(["warn", "המקלט בשימוש"]);
  else if (device.state === "checking") pills.push(["off", "בודק מקלט…"]);
  else pills.push(["bad", "אין מקלט"]);

  if (usbFresh()) {
    const sats = S.gnss.satellites;
    pills.push(["", `USB GPS${sats !== null && sats !== undefined ? ` · ${num(sats)} לוויינים` : ""}`]);
  } else if (phoneFresh()) {
    pills.push(["", "GPS מהטלפון"]);
  } else if (S.gnss) {
    pills.push(["bad", S.gnss.state === "no_fix" ? "USB GPS מחפש לוויינים" : "אין GPS"]);
  } else {
    pills.push(["bad", "אין GPS"]);
  }
  if (S.disk) pills.push([S.disk.ready ? "" : "bad", `דיסק ${num(S.disk.free_gib)} GB`]);
  $("#status").innerHTML = pills
    .map(([kind, text]) => `<span class="pill"><span class="dot ${kind}"></span>${text}</span>`)
    .join("");
}

function renderTargets() {
  const container = $("#recent");
  container.innerHTML = "";
  for (const target of S.targets) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.setAttribute("role", "listitem");
    const solution = target.latest_solution;
    const parts = [];
    if (target.label) parts.push(escapeHtml(target.label));
    parts.push(`${num(target.run_count)} מדידות`);
    if (solution && solution.area_km2_90) parts.push(`${num(formatArea(solution.area_km2_90))} קמ״ר`);
    else if (solution) parts.push("אין אזור עדיין");
    else parts.push("טרם חושב");
    chip.innerHTML = `<span class="f">${mhz(target.frequency_hz)}</span><span class="d">${parts.join(" · ")}</span>`;
    chip.setAttribute("aria-pressed", String(Boolean(S.active && S.active.campaign_id === target.campaign_id)));
    chip.addEventListener("click", () => {
      $("#freq").value = mhz(target.frequency_hz);
      $$(".chip").forEach((node) => node.setAttribute("aria-pressed", String(node === chip)));
      updateHomeHint();
    });
    container.appendChild(chip);
  }
  container.hidden = S.targets.length === 0;
}

function knownTargetFor(frequencyHz) {
  return S.targets.find((target) => Math.abs(target.frequency_hz - frequencyHz) <= 1) || null;
}

function updateHomeHint() {
  const value = parseFrequency();
  const known = value ? knownTargetFor(value) : null;
  $("#home-hint").textContent = known
    ? (known.needs_calibration
      ? "יעד קיים שעדיין לא כויל: לפני הנסיעה יש כיול של דקה בעמידה."
      : "יעד קיים: ממשיכים את הקמפיין שלו באותו הגבר, בלי כיול.")
    : "יעד חדש: לפני הנסיעה יש כיול של דקה בעמידה, במקום שבו התדר נשמע.";
}

function parseFrequency() {
  const raw = String($("#freq").value || "").replace(",", ".").trim();
  const value = Number(raw);
  if (!raw || !isFinite(value) || value <= 0) return null;
  // Typed in MHz. A value above 10,000 was typed in kHz or Hz by mistake.
  return value > 10000 ? null : Math.round(value * 1e6);
}

/* ------------------------------------------------------------ start flow */

async function startLocate() {
  banner("");
  const frequencyHz = parseFrequency();
  if (!frequencyHz) {
    banner("הקלד תדר במגה-הרץ, למשל 469.350");
    return;
  }
  const button = $("#start");
  button.disabled = true;
  try {
    const payload = { frequency_hz: frequencyHz };
    // Settings chosen in "target settings" before the first Start of a new
    // frequency go with it; an existing target keeps its own.
    if (!knownTargetFor(frequencyHz)) {
      payload.emission = S.draft.emission;
      payload.channel_bandwidth_hz = S.draft.bandwidth;
      if (S.draft.label) payload.label = S.draft.label;
      if (S.draft.truth) payload.truth = S.draft.truth;
    }
    const answer = await post("/api/locate/target", payload);
    S.active = answer.active;
    S.draft = { emission: "continuous", bandwidth: 12500 };
    loadTargets();
    holdScreenAwake();
    if (S.active.needs_calibration) await startCalibration();
    else await startDrive();
  } catch (error) {
    banner(error.message);
    show("home");
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------------ calibration */

async function startCalibration() {
  show("calibrate");
  $("#cal-hint").innerHTML =
    `קובע הגבר קבוע לקמפיין של ${num(mhz(S.active.frequency_hz))} MHz. עדיף לעמוד במקום שבו התדר נשמע.`;
  $("#cal-result").hidden = true;
  $("#cal-choice").hidden = true;
  $("#cal-cancel").hidden = false;
  setRing(0);
  $("#cal-label").textContent = "מתחיל…";
  try {
    const job = await post("/api/calibrate/gain", {});
    S.calJobId = job.job_id;
    speak("מכייל הגבר. עמוד במקום כדקה.", { urgent: true });
    pollCalibration();
  } catch (error) {
    calibrationOutcome(`הכיול לא התחיל: ${error.message}`);
  }
}

function setRing(progress) {
  $("#ring").setAttribute("stroke-dashoffset", String(377 * (1 - Math.max(0, Math.min(1, progress)))));
}

async function pollCalibration() {
  if (!S.calJobId) return;
  let job;
  try {
    job = await api("/api/jobs/" + S.calJobId);
  } catch (_) {
    S.calTimer = setTimeout(pollCalibration, 2000);
    return;
  }
  setRing(job.progress || 0);
  const step = /step (\d+)\/(\d+)/.exec(job.message || "");
  if (step) $("#cal-label").innerHTML = `מודד הגבר ${num(step[1])} מתוך ${num(step[2])}`;
  if (job.status === "running" || job.status === "pending") {
    S.calTimer = setTimeout(pollCalibration, 1000);
    return;
  }
  S.calJobId = null;
  $("#cal-cancel").hidden = true;
  if (job.status === "succeeded") {
    const result = job.result || {};
    setRing(1);
    const judgedByTarget = result.method === "target_snr_plateau";
    if (result.applied && result.status === "ok" && judgedByTarget) {
      $("#cal-label").textContent = "ההגבר נקבע. אפשר לנסוע.";
      speak("ההגבר נקבע. אפשר לנסוע.", { urgent: true });
      await refreshState();
      await sleep(900);
      await startDrive();
      return;
    }
    const why = !judgedByTarget
      ? "התדר לא נשמע כאן, אז ההגבר נקבע לפי רצפת הרעש בלבד. עדיף לכייל במקום שבו התדר נשמע."
      : `ההגבר לא שונה: ${result.apply_reason || result.status}`;
    calibrationOutcome(why);
    speak("הכיול הסתיים. בדוק את המסך.", { urgent: true });
  } else if (job.status === "cancelled") {
    show("home");
  } else {
    calibrationOutcome(`הכיול נכשל: ${job.error || "שגיאה לא ידועה"}`);
    speak("הכיול נכשל.", { urgent: true });
  }
}

function calibrationOutcome(message) {
  $("#cal-label").textContent = "";
  const node = $("#cal-result");
  node.hidden = false;
  node.textContent = message;
  $("#cal-choice").hidden = false;
  $("#cal-cancel").hidden = true;
}

async function cancelCalibration() {
  if (S.calJobId) {
    try { await post("/api/jobs/" + S.calJobId + "/cancel"); } catch (_) { /* shown by the poll */ }
  } else {
    show("home");
  }
}

/* ------------------------------------------------------------ GPS (phone) */

function startPhoneGps() {
  if (!navigator.geolocation || !window.isSecureContext || S.watchId !== null) return;
  S.gpsError = "";
  S.watchId = navigator.geolocation.watchPosition(
    sendFix,
    (error) => { S.gpsError = error.message; renderStatus(); },
    { enableHighAccuracy: true, maximumAge: 0, timeout: 30000 },
  );
  fillMenu();
}

function stopPhoneGps() {
  if (S.watchId !== null) navigator.geolocation.clearWatch(S.watchId);
  S.watchId = null;
  fillMenu();
}

async function sendFix(fix) {
  const age = typeof fix.timestamp === "number" ? Math.max(0, (Date.now() - fix.timestamp) / 1000) : null;
  S.lastFix = { at: Date.now() - (age || 0) * 1000 };
  if (S.posting) return;  // the Pi keeps only the latest; never queue stale ones
  S.posting = true;
  try {
    await post("/api/live/position", {
      latitude: fix.coords.latitude,
      longitude: fix.coords.longitude,
      accuracy_m: fix.coords.accuracy,
      speed_mps: fix.coords.speed,
      fix_age_s: age,
    });
  } catch (_) {
    /* the HUD shows GPS as lost if fixes stop reaching the Pi */
  } finally {
    S.posting = false;
  }
}

/* ------------------------------------------------------------------ drive */

async function startDrive() {
  banner("");
  if (!usbFresh()) {
    if (!window.isSecureContext) {
      banner("אין GPS: ה-USB GPS של ה-Pi בלי קליטה, והטלפון ישתף מיקום רק בחיבור https.");
      show("home");
      return;
    }
    startPhoneGps();
    const until = Date.now() + 15000;
    while (!S.lastFix && !S.gpsError && Date.now() < until) await sleep(250);
    if (S.posting) await sleep(500);
  }
  try {
    const job = await post("/api/live/start", {});
    S.liveJobId = job.job_id;
  } catch (error) {
    // A second tap may have started it already: join that drive instead.
    try {
      const status = await api("/api/live");
      if (status.running) S.liveJobId = status.job_id;
    } catch (_) { /* fall through */ }
    if (!S.liveJobId) {
      banner(`הנסיעה לא התחילה: ${error.message}`);
      show("home");
      return;
    }
  }
  S.announcedBins = 0;
  S.lastHeard = null;
  S.pullOverBin = null;
  S.gpsLost = false;
  S.driveWasRunning = true;
  $("#drive-title").innerHTML = `${num(mhz(S.active.frequency_hz))} MHz · בנסיעה`;
  show("drive");
  holdScreenAwake();
  speak("הנסיעה התחילה. מודד.");
  pollLive();
}

async function pollLive() {
  clearTimeout(S.livePollTimer);
  let payload = null;
  try {
    payload = await api("/api/live");
  } catch (_) {
    S.livePollTimer = setTimeout(pollLive, POLL_MS);
    return;   // a dropped link is not the end of the drive: it runs on the Pi
  }
  S.gnss = payload.gnss || S.gnss;
  renderDrive(payload);
  if (!payload.running) {
    if (S.driveWasRunning) {
      S.driveWasRunning = false;
      S.liveJobId = null;
      releaseScreen();
      speak(`הנסיעה נעצרה. ${payload.bin_count || 0} מדידות.`);
      show("result");
      requestSolve();
    }
    return;
  }
  S.livePollTimer = setTimeout(pollLive, POLL_MS);
}

function renderDrive(payload) {
  const target = payload.target;
  const lastCell = $("#hud-last");
  const last = target && target.last_bin;
  if (!last) {
    lastCell.className = "silent";
    $("#hud-last-v").textContent = "ממתין…";
  } else if (last.heard) {
    lastCell.className = "heard";
    $("#hud-last-v").innerHTML = `נשמע · ${num(Math.round(last.level_db))} dB`;
  } else {
    lastCell.className = "silent";
    $("#hud-last-v").textContent = last.absence_is_evidence ? "לא נשמע" : "לא שודר / לא נשמע";
  }
  const bins = payload.bin_count || 0;
  // Short enough for a phone held upright: "12/30", heard over measured.
  if (target) {
    $("#hud-bins").textContent = `${target.heard}/${target.heard + target.not_heard}`;
    $("#hud-bins-l").textContent = "נשמע / נמדד";
  } else {
    $("#hud-bins").textContent = String(bins);
    $("#hud-bins-l").textContent = "מדידות";
  }

  // GPS: whichever receiver is placing the windows, and whether it is fresh.
  const age = payload.position_age_seconds;
  const stale = age === null || age === undefined || age > 10;
  const gpsCell = $("#hud-gps");
  gpsCell.className = stale ? "lost" : "";
  $("#hud-gps-v").textContent = stale ? "אבד" : (payload.position_source === "usb_gnss" ? "USB" : "טלפון");
  $("#hud-gps-l").textContent = stale ? "GPS" : `GPS · ${Math.round(age)} ש׳`;

  // Edge-triggered speech: each event once, never a stream.
  if (stale && !S.gpsLost && payload.running) { S.gpsLost = true; speak("ה-GPS אבד.", { urgent: true }); }
  if (!stale && S.gpsLost) { S.gpsLost = false; speak("ה-GPS חזר."); }
  if (bins >= S.announcedBins + 5) { S.announcedBins = bins; speak(`${bins} מדידות.`); }
  if (last) {
    if (S.lastHeard === false && last.heard) speak("נשמע שוב.");
    if (S.lastHeard === true && !last.heard && last.absence_is_evidence) speak("לא נשמע.");
    S.lastHeard = last.heard;
  }

  const pull = payload.pull_over || {};
  const hold = payload.hold || {};
  $("#worth").hidden = !(pull.suggest && !hold.active);
  $("#hold").classList.toggle("pulse", Boolean(pull.suggest && !hold.active));
  if (pull.suggest && pull.bin && pull.bin !== S.pullOverBin) {
    S.pullOverBin = pull.bin;
    speak("שווה לעצור כאן לדקה.");
  }
  const holdButton = $("#hold");
  if (hold.active) {
    holdButton.disabled = true;
    holdButton.innerHTML = `עומד · ${num(Math.ceil(hold.seconds_left))} ש׳`;
  } else if (!hold.requested) {
    holdButton.disabled = false;
    holdButton.innerHTML = `מדוד כאן ${num(60)} ש׳`;
  }
  drawDrive(payload);
}

async function requestHold() {
  const button = $("#hold");
  button.disabled = true;
  try {
    const result = await post("/api/live/hold", { seconds: 60 });
    if (result.accepted) speak("עמוד במקום. מודד שישים שניות.", { urgent: true });
    else { button.disabled = false; banner(result.reason || "המדידה לא התחילה"); }
  } catch (error) {
    button.disabled = false;
    banner(error.message);
  }
}

async function stopDrive() {
  if (!S.liveJobId) return;
  $("#stop").disabled = true;
  try {
    await post("/api/jobs/" + S.liveJobId + "/cancel");
  } catch (error) {
    banner(`לא הצלחתי לעצור: ${error.message}`);
  } finally {
    $("#stop").disabled = false;
  }
}

/* ------------------------------------------------------------ result */

async function requestSolve() {
  const button = $("#solve");
  button.disabled = true;
  button.textContent = "מחשב…";
  if (S.screen === "result") $("#res-title").textContent = "מחשב…";
  try {
    const started = await post("/api/locate/solve", {});
    if (!started.started && started.reason) {
      /* one is already running: just wait for it */
    }
  } catch (error) {
    button.disabled = false;
    button.textContent = "עדכן תוצאה";
    banner(`החישוב לא התחיל: ${error.message}`);
    if (S.screen === "result") $("#res-title").textContent = "אין תוצאה";
    return;
  }
  pollResult();
}

async function pollResult() {
  clearTimeout(S.resultTimer);
  let answer;
  try {
    answer = await api("/api/locate/result");
  } catch (_) {
    S.resultTimer = setTimeout(pollResult, POLL_MS);
    return;
  }
  if (answer.solving) {
    S.resultTimer = setTimeout(pollResult, POLL_MS);
    return;
  }
  const button = $("#solve");
  button.disabled = false;
  button.textContent = "עדכן תוצאה";
  showResult(answer.result);
  if (S.screen === "drive" && answer.result && answer.result.solution) speak("התוצאה עודכנה.");
}

function formatArea(km2) {
  const value = Number(km2);
  return value >= 100 ? Math.round(value).toLocaleString("en-US") : value.toFixed(1);
}

function statusTitle(solution) {
  const area = solution.area_km2_90;
  switch (solution.status) {
    case "ok":
      return `אזור חיפוש: ${num(formatArea(area))} קמ״ר`;
    case "unbounded_region":
      return "האזור עדיין פתוח לקצה";
    case "weak_geometry":
      return "נשמע מכיוון אחד בלבד";
    case "insufficient_evidence":
      return "עוד אין מספיק מדידות";
    case "no_measurements":
      return "אין עדיין מדידות ליעד הזה";
    default:
      return escapeHtml(solution.status);
  }
}

function statusExplanation(solution) {
  switch (solution.status) {
    case "unbounded_region":
      return "אזור ה-90% מגיע עד קצה השטח שנבדק. כדאי לנסוע גם מעבר לו, בצד הרחוק.";
    case "weak_geometry":
      return "כל המדידות מאותו צד. מדידות מסביב למשדר יסגרו את האזור הרבה יותר מנסיעה ארוכה בצד אחד.";
    case "insufficient_evidence":
      return "צריך עוד מדידות, גם במקומות שבהם התדר לא נשמע.";
    default:
      return "";
  }
}

function showResult(result) {
  S.lastResult = result;
  drawResult(result);
  const drawable = Boolean(result && result.regions && (result.regions.features || [])
    .some((feature) => feature.geometry));
  $("#region-age").hidden = !drawable;
  if (drawable) $("#region-age").textContent = "אזור מהחישוב האחרון";
  if (!result) {
    $("#res-title").textContent = "אין עדיין תוצאה";
    $("#res-facts").hidden = true;
    ["#res-plan", "#res-suspect", "#res-truth"].forEach((id) => { $(id).hidden = true; });
    return;
  }
  if (result.error) {
    $("#res-title").textContent = "החישוב נכשל";
    banner(result.error);
    return;
  }
  const solution = result.solution;
  $("#res-title").innerHTML = statusTitle(solution);
  $("#res-facts").hidden = false;
  $("#res-heard").textContent = String(solution.detection_count || 0);
  $("#res-silent").textContent = String(solution.non_detection_count || 0);
  $("#res-area").textContent = solution.area_km2_90 ? formatArea(solution.area_km2_90) : "–";

  const plan = result.plan;
  const planNode = $("#res-plan");
  const stops = (plan && plan.top_stops) || [];
  const explanation = statusExplanation(solution);
  if (stops.length && stops[0].bearing_from_mode_deg !== undefined) {
    const km = (stops[0].distance_from_mode_m / 1000).toFixed(1);
    planNode.innerHTML =
      `כדאי למדוד גם מ${compass(stops[0].bearing_from_mode_deg)} לאזור, כ-${num(km)} ק״מ ` +
      "מהנקודה הסבירה. מדידה שם תקטין את האזור הכי הרבה." +
      (explanation ? ` ${explanation}` : "");
    planNode.hidden = false;
    speakPlan(stops[0]);
  } else if (explanation) {
    planNode.textContent = explanation;
    planNode.hidden = false;
  } else {
    planNode.hidden = true;
  }

  const diagnostics = parseMaybeJson(solution.diagnostics) || parseMaybeJson(solution.diagnostics_json) || {};
  const suspects = ((diagnostics.suspect_stops || {}).labels) || [];
  $("#res-suspect").hidden = suspects.length === 0;
  $("#res-suspect").textContent = suspects.length === 1
    ? "מדידה אחת לא מתיישבת עם השאר. היא לא הוסרה. כדאי למדוד שוב בסביבתה."
    : `${suspects.length} מדידות לא מתיישבות עם השאר. הן לא הוסרו. כדאי למדוד שוב בסביבתן.`;

  const truth = parseMaybeJson(solution.truth) || parseMaybeJson(solution.truth_json) || {};
  const truthNode = $("#res-truth");
  if (truth.mode_error_m !== undefined && truth.mode_error_m !== null) {
    const inside = truth.inside && truth.inside["0.9"];
    truthNode.innerHTML =
      `אימות: הנקודה הסבירה רחוקה ${num(Math.round(truth.mode_error_m).toLocaleString("he-IL"))} מ׳ ` +
      `מהמיקום הידוע, שנמצא ${inside ? "בתוך" : "<b>מחוץ</b> ל"}אזור ה-90%.`;
    truthNode.className = "notice " + (inside ? "good" : "bad");
    truthNode.hidden = false;
  } else {
    truthNode.hidden = true;
  }
}

function speakPlan(stop) {
  if (S.screen !== "result" && S.screen !== "drive") return;
  speak(`כדאי למדוד גם מ${compass(stop.bearing_from_mode_deg)} לאזור.`);
}

/* ------------------------------------------------------------ advanced */

function setSeg(attribute, value) {
  $$(`[data-${attribute}]`).forEach((node) => {
    node.setAttribute("aria-pressed", String(String(node.dataset[attribute]) === String(value)));
  });
}

function emissionHint(value) {
  $("#emission-hint").textContent = value === "intermittent"
    ? "מקום שבו לא נשמע נשמר אבל לא נחשב: ייתכן שפשוט לא שידר."
    : "מקום שבו לא נשמע נחשב ראיה, וזה חלק חשוב מהתוצאה.";
}

function fillAdvanced() {
  $("#adv-error").hidden = true;
  $("#adv-restart-confirm").hidden = true;
  const active = S.active;
  const typed = parseFrequency();
  const editingActive = active && (!typed || Math.abs(typed - active.frequency_hz) <= 1);
  if (editingActive) {
    $("#adv-for").innerHTML =
      `${num(mhz(active.frequency_hz))} MHz · קמפיין ${num(escapeHtml(active.campaign_id))} · ${num(active.run_count)} מדידות`;
    $("#adv-label").value = active.label || "";
    setSeg("emission", active.emission);
    setSeg("bw", active.channel_bandwidth_hz);
    emissionHint(active.emission);
    $("#adv-center").value = mhz(active.tuning.center_frequency_hz);
    $("#adv-rate").value = String(active.tuning.sample_rate_hz / 1e6);
    $("#adv-auto").hidden = active.tuning.source !== "auto";
    const gain = active.gain;
    $("#adv-gain").textContent =
      `IFGR ${gain.if_gain_reduction_db} · LNA ${gain.lna_state}` +
      (gain.calibrated_at_utc ? " · מכויל" : " · לא כויל");
    const locked = active.run_count > 0;
    $$("[data-bw]").forEach((node) => { node.disabled = locked; });
    $("#adv-center").disabled = locked;
    $("#adv-rate").disabled = locked;
    $("#adv-restart").hidden = false;
    $("#adv-save").textContent = "שמור";
    $("#adv-lat").placeholder = active.has_known_position ? "שמור מיקום ידוע" : "32.0812";
  } else {
    $("#adv-for").textContent = typed
      ? `ההגדרות יחולו כשתתחיל את ${mhz(typed)} MHz.`
      : "ההגדרות יחולו על התדר הבא שתתחיל.";
    $("#adv-label").value = S.draft.label || "";
    setSeg("emission", S.draft.emission);
    setSeg("bw", S.draft.bandwidth);
    emissionHint(S.draft.emission);
    $("#adv-center").value = typed ? mhz(typed - 100000) : "";
    $("#adv-rate").value = "2";
    $("#adv-auto").hidden = false;
    $("#adv-gain").textContent = "ייקבע בכיול";
    $$("[data-bw]").forEach((node) => { node.disabled = false; });
    $("#adv-center").disabled = true;
    $("#adv-rate").disabled = true;
    $("#adv-restart").hidden = true;
    $("#adv-save").textContent = "שמור לתדר הבא";
  }
}

function pressed(attribute) {
  const node = $$(`[data-${attribute}]`).find((item) => item.getAttribute("aria-pressed") === "true");
  return node ? node.dataset[attribute] : null;
}

function truthFromForm() {
  const lat = $("#adv-lat").value.trim();
  const lon = $("#adv-lon").value.trim();
  if (!lat && !lon) return undefined;
  return { latitude: Number(lat), longitude: Number(lon), source: $("#adv-src").value.trim() };
}

async function saveAdvanced() {
  const error = $("#adv-error");
  error.hidden = true;
  const active = S.active;
  const typed = parseFrequency();
  const editingActive = active && (!typed || Math.abs(typed - active.frequency_hz) <= 1);
  const truth = truthFromForm();
  if (!editingActive) {
    S.draft = {
      label: $("#adv-label").value.trim(),
      emission: pressed("emission") || "continuous",
      bandwidth: Number(pressed("bw") || 12500),
      truth,
    };
    show(S.previous === "advanced" ? "home" : S.previous);
    return;
  }
  const payload = {
    frequency_hz: active.frequency_hz,
    label: $("#adv-label").value.trim(),
    emission: pressed("emission"),
  };
  if (active.run_count === 0) {
    payload.channel_bandwidth_hz = Number(pressed("bw"));
    payload.center_frequency_hz = Math.round(Number($("#adv-center").value) * 1e6);
    payload.sample_rate_hz = Math.round(Number($("#adv-rate").value) * 1e6);
  }
  if (truth) payload.truth = truth;
  try {
    const answer = await post("/api/locate/target", payload);
    S.active = answer.active;
    loadTargets();
    show(S.previous === "advanced" ? "home" : S.previous);
  } catch (err) {
    error.hidden = false;
    error.textContent = err.message;
  }
}

async function restartCampaign() {
  try {
    const answer = await post("/api/locate/target", {
      frequency_hz: S.active.frequency_hz, start_over: true,
    });
    S.active = answer.active;
    await loadTargets();
    showResult(null);
    $("#freq").value = mhz(S.active.frequency_hz);
    updateHomeHint();
    show("home");
  } catch (err) {
    $("#adv-error").hidden = false;
    $("#adv-error").textContent = err.message;
  }
}

/* ------------------------------------------------------------ menu */

function fillMenu() {
  $("#classic-link").href = "/" + (TOKEN ? "?token=" + encodeURIComponent(TOKEN) : "");
  $("#share-gps-state").textContent = S.watchId !== null
    ? "משתף · הקש כדי להפסיק"
    : (window.isSecureContext ? "לא משתף · הקש כדי לשתף" : "דורש חיבור https");
  const settings = S.settings || {};
  const active = S.active;
  $("#menu-info").innerHTML = active
    ? `יעד פעיל: ${num(mhz(active.frequency_hz))} MHz · קמפיין ${num(escapeHtml(active.campaign_id))}`
    : `אין יעד פעיל. קמפיין נוכחי: ${num(escapeHtml(settings.campaign_id || "ללא"))}`;
}

/* ------------------------------------------------------------ wiring */

function wire() {
  $$("[data-go]").forEach((node) => node.addEventListener("click", () => show(node.dataset.go)));
  $$("[data-back]").forEach((node) => node.addEventListener("click", () => {
    show(S.previous && S.previous !== S.screen ? S.previous : "home");
  }));
  $("#start").addEventListener("click", startLocate);
  $("#freq").addEventListener("input", () => {
    $$(".chip").forEach((node) => node.setAttribute("aria-pressed", "false"));
    updateHomeHint();
  });
  $("#freq").addEventListener("keydown", (event) => { if (event.key === "Enter") startLocate(); });
  $("#cal-cancel").addEventListener("click", cancelCalibration);
  $("#cal-retry").addEventListener("click", startCalibration);
  $("#cal-drive").addEventListener("click", startDrive);
  $("#hold").addEventListener("click", requestHold);
  $("#solve").addEventListener("click", requestSolve);
  $("#stop").addEventListener("click", stopDrive);
  $("#res-drive").addEventListener("click", startDrive);
  $("#res-refresh").addEventListener("click", requestSolve);
  $$("[data-emission]").forEach((node) => node.addEventListener("click", () => {
    setSeg("emission", node.dataset.emission);
    emissionHint(node.dataset.emission);
  }));
  $$("[data-bw]").forEach((node) => node.addEventListener("click", () => setSeg("bw", node.dataset.bw)));
  $("#adv-save").addEventListener("click", saveAdvanced);
  $("#adv-restart").addEventListener("click", () => { $("#adv-restart-confirm").hidden = false; });
  $("#adv-restart-no").addEventListener("click", () => { $("#adv-restart-confirm").hidden = true; });
  $("#adv-restart-yes").addEventListener("click", restartCampaign);
  $("#share-gps").addEventListener("click", () => {
    if (S.watchId !== null) stopPhoneGps(); else startPhoneGps();
  });
  try {
    const stored = localStorage.getItem("locate.voice");
    if (stored !== null) $("#voice").checked = stored === "1";
  } catch (_) { /* per-phone convenience only */ }
  $("#voice").addEventListener("change", () => {
    try { localStorage.setItem("locate.voice", $("#voice").checked ? "1" : "0"); } catch (_) { /* ignore */ }
  });
}

/* Status pills stay current on the home screen without a reload. */
async function pollHome() {
  if (S.screen === "home") {
    try { await refreshState(); } catch (_) { /* shown on the next success */ }
  }
  setTimeout(pollHome, 5000);
}

async function boot() {
  wire();
  let payload;
  try {
    payload = await refreshState();
  } catch (error) {
    banner(`אין חיבור ל-Pi: ${error.message}`);
    setTimeout(() => location.reload(), 5000);
    return;
  }
  await loadTargets();
  if (S.active) $("#freq").value = mhz(S.active.frequency_hz);
  updateHomeHint();
  if (!window.isSecureContext && !S.settings.gnss_device) {
    banner("החיבור אינו https, אז הטלפון לא ישתף מיקום. פתח את הכתובת עם https.");
  }
  // The map loads alongside, never in front: on a slow link (or none, when
  // Leaflet is not vendored and the CDN is out of reach) it can take many
  // seconds, and the screen must not change under an operator who has
  // already pressed Start.
  ensureLeaflet().then(() => {
    initMap();   // says so on screen when the library could not be loaded
    if (S.lastResult) drawResult(S.lastResult);
  });

  // A drive or a calibration keeps running on the Pi when the phone locks or
  // the page reloads; rejoin it rather than offering to start another.
  const jobs = payload.jobs || [];
  const calibrating = jobs.find((job) => job.kind === "calibration" &&
    (job.status === "running" || job.status === "pending"));
  if (payload.live && payload.live.running && S.active) {
    S.liveJobId = payload.live.job_id;
    S.driveWasRunning = true;
    $("#drive-title").innerHTML = `${num(mhz(S.active.frequency_hz))} MHz · בנסיעה`;
    show("drive");
    holdScreenAwake();
    pollLive();
  } else if (calibrating && S.active) {
    show("calibrate");
    S.calJobId = calibrating.job_id;
    $("#cal-cancel").hidden = false;
    pollCalibration();
  } else {
    show("home");
  }
  if (S.active) {
    const answer = await api("/api/locate/result").catch(() => null);
    if (answer) {
      showResult(answer.result);
      if (answer.solving) pollResult();
    }
  }
  pollHome();
}

boot();
