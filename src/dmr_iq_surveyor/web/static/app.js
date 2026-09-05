/* Field control surface for P25 site geolocation.
 *
 * No build step and no framework: this is served off a Raspberry Pi to one
 * phone, and a tool used in a car park should have as few moving parts as
 * possible.
 */
"use strict";

const TOKEN = new URLSearchParams(location.search).get("token") || "";
const state = {
  settings: null,
  position: null,
  sites: [],
  stops: [],
  plan: null,
  picking: false,
  jobId: null,
  cursor: 0,
  stream: null,
};

/* A drive's own state. Kept apart from `state` because none of it survives a
 * reload: the fixes are worthless a minute later and the measurements they
 * produced are already in the database. */
const live = {
  watchId: null,
  jobId: null,
  timer: null,
  posting: false,
  fixes: 0,
  lastFix: null,
  gpsError: "",
  postError: "",
  lastSolveAt: null,
  binsDrawn: 0,
  // Driver-alone aids. Edge-triggered so each event is spoken once: a
  // stale-GPS state that is announced on every two-second poll would be a
  // stream of noise, and the one thing a solo driver cannot do is read.
  gpsLost: false,
  announcedBins: 0,
  holdWasActive: false,
  pullOverBin: null,
  wakeLock: null,
  lastSpoken: { text: "", at: 0 },
};

/* --------------------------------------------------------------- driving aids */

/* Speech for a driver who must not look down. Throttled so the same sentence
 * is not repeated within twenty seconds, and cut short if a newer one lands,
 * because "GPS lost" must not queue behind "twelve bins". */
function speak(text, { urgent = false } = {}) {
  const toggle = $("#drive-voice");
  if (toggle && !toggle.checked) return;
  if (!("speechSynthesis" in window)) return;
  const now = Date.now();
  if (live.lastSpoken.text === text && now - live.lastSpoken.at < 20000) return;
  live.lastSpoken = { text, at: now };
  try {
    if (urgent) window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.05;
    window.speechSynthesis.speak(utterance);
  } catch (_) {
    /* speech is a convenience, never a dependency */
  }
}

/* Keep the screen -- and with it the GPS watch, which some browsers throttle
 * when the tab is hidden -- alive for the length of the drive. Re-acquired on
 * visibility change because the platform drops it whenever the tab hides. */
async function holdScreenAwake() {
  if (!("wakeLock" in navigator)) return;
  try {
    live.wakeLock = await navigator.wakeLock.request("screen");
  } catch (_) {
    /* denied or unsupported: the drive still works, the screen may dim */
  }
}

function releaseScreen() {
  if (live.wakeLock) {
    live.wakeLock.release().catch(() => {});
    live.wakeLock = null;
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && live.jobId && !live.wakeLock) holdScreenAwake();
});

/* ---------------------------------------------------------------- helpers */

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (TOKEN) headers["X-Auth-Token"] = TOKEN;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, Object.assign({}, options, { headers }));
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch (_) { payload = { error: text }; }
  if (!response.ok) {
    const error = new Error((payload && payload.error) || `HTTP ${response.status}`);
    error.status = response.status;
    error.needsPositionConfirmation = Boolean(payload && payload.needs_position_confirmation);
    throw error;
  }
  return payload;
}

const $ = (selector) => document.querySelector(selector);

/* Popup bodies are assembled as HTML, and their values come from a
 * user-supplied site snapshot (notes, status reasons) and from job messages.
 * A stray angle bracket in a CSV note would otherwise break the popup. */
const escapeHtml = (value) =>
  String(value === null || value === undefined ? "" : value).replace(
    /[&<>"']/g,
    (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch],
  );
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

function setConnection(text, kind) {
  const node = $("#connection");
  node.textContent = text;
  node.className = "pill" + (kind ? " " + kind : "");
}

function formatGiB(bytes) {
  return (bytes / 1073741824).toFixed(2) + " GiB";
}

function formatArea(value) {
  if (value === null || value === undefined) return "-";
  return value < 1 ? `${Math.round(value * 100)} ha` : `${value.toFixed(2)} km²`;
}

/* Level colours span the useful range of channel SNR above the local noise
 * floor. The scale is relative, never calibrated power, so it is labelled
 * in dB above noise rather than in dBm. */
function levelColour(db) {
  const stops = [
    [0, "#7fb3ff"], [8, "#3ecf8e"], [16, "#c8d420"],
    [26, "#f5a623"], [38, "#b42318"],
  ];
  if (db === null || db === undefined) return "#8b95a1";
  let colour = stops[0][1];
  for (const [threshold, value] of stops) if (db >= threshold) colour = value;
  return colour;
}

/* ------------------------------------------------------------------- map */

let map = null;
const layers = {};

const LEAFLET_SOURCES = [
  { js: "/vendor/leaflet.js", css: "/vendor/leaflet.css" },
  {
    js: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js",
    css: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css",
  },
  {
    js: "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    css: "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
  },
];

function loadTag(tag, attributes) {
  return new Promise((resolve, reject) => {
    const node = document.createElement(tag);
    Object.assign(node, attributes);
    node.onload = resolve;
    node.onerror = () => reject(new Error("could not load " + (attributes.src || attributes.href)));
    document.head.appendChild(node);
  });
}

/* A vendored copy is tried before the CDN. When Leaflet is local, losing
 * internet costs only the map tiles -- the measurement points and credible
 * regions still draw, on a blank background, which is the part that matters. */
async function ensureLeaflet() {
  for (const source of LEAFLET_SOURCES) {
    try {
      await loadTag("script", { src: source.js, async: false });
      if (window.L) {
        loadTag("link", { rel: "stylesheet", href: source.css }).catch(() => {});
        return true;
      }
    } catch (_) {
      /* try the next source */
    }
  }
  return false;
}

function initMap() {
  if (typeof L === "undefined") {
    const banner = el("div", "notice error",
      "The map library could not be loaded, so the map is unavailable — everything else " +
      "still works. Run scripts/vendor_leaflet.sh once while online to keep a local copy.");
    $("#sheet").prepend(banner);
    return false;
  }
  const settings = state.settings;
  map = L.map("map", { zoomControl: false }).setView(settings.map_center, settings.map_zoom);
  L.control.zoom({ position: "topright" }).addTo(map);
  L.tileLayer(settings.tile_url, { attribution: settings.tile_attribution, maxZoom: 19 }).addTo(map);
  layers.regions90 = L.layerGroup().addTo(map);
  layers.regions50 = L.layerGroup().addTo(map);
  layers.measurements = L.layerGroup().addTo(map);
  layers.nondetections = L.layerGroup().addTo(map);
  layers.estimates = L.layerGroup().addTo(map);
  layers.plan = L.layerGroup().addTo(map);
  layers.bins = L.layerGroup().addTo(map);
  layers.track = L.layerGroup().addTo(map);
  layers.position = L.layerGroup().addTo(map);

  map.on("click", (event) => {
    if (!state.picking) return;
    state.picking = false;
    $("#pick-on-map").classList.remove("armed");
    savePosition(event.latlng.lat, event.latlng.lng, null, "manual");
  });
  return true;
}

function renderPosition() {
  const position = state.position;
  const readout = $("#position-readout");
  if (!position || position.latitude === null || position.latitude === undefined) {
    readout.textContent = "not set — record needs a position";
    return;
  }
  const accuracy = position.accuracy_m ? ` ±${Math.round(position.accuracy_m)} m` : "";
  const label = position.label ? `${position.label} · ` : "";
  readout.textContent =
    `${label}${position.latitude.toFixed(5)}, ${position.longitude.toFixed(5)}${accuracy} (${position.source})`;
  if (position.label) $("#stop-label").value = position.label;
  if (!map) return;
  layers.position.clearLayers();
  L.circleMarker([position.latitude, position.longitude], {
    radius: 9, color: "#1f6feb", weight: 3, fillColor: "#1f6feb", fillOpacity: 0.35,
  }).addTo(layers.position).bindPopup("<b>You are here</b>");
  if (position.accuracy_m) {
    L.circle([position.latitude, position.longitude], {
      radius: position.accuracy_m, color: "#1f6feb", weight: 1, opacity: 0.5, fillOpacity: 0.06,
    }).addTo(layers.position);
  }
}

function measurementPopup(properties) {
  const rows = [
    ["site", properties.site_key],
    ["frequency", `${(properties.frequency_hz / 1e6).toFixed(6)} MHz`],
    ["run", properties.survey_run_id],
    ["attribution", properties.attribution],
  ];
  if (properties.detected) {
    rows.splice(1, 0, ["level", `${properties.level_db.toFixed(1)} dB above noise`]);
  } else {
    rows.splice(1, 0, ["result", `below ${properties.censor_level_db.toFixed(1)} dB — looked, heard nothing`]);
  }
  if (properties.usability !== "usable") rows.push(["excluded", properties.usability]);
  if (properties.quality_flags && properties.quality_flags.length) {
    rows.push(["flags", properties.quality_flags.join(", ")]);
  }
  return `<b>${properties.detected ? "Detection" : "Non-detection"}</b><dl>` +
    rows.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join("") +
    "</dl>";
}

function estimatePopup(properties) {
  const rows = [
    ["status", properties.status],
    ["detections", properties.detection_count],
    ["non-detections", properties.non_detection_count],
    ["50% region", formatArea(properties.area_km2_50)],
    ["90% region", formatArea(properties.area_km2_90)],
    ["azimuth span", properties.azimuth_span_deg ? `${Math.round(properties.azimuth_span_deg)}°` : "-"],
  ];
  const warnings = (properties.warnings || [])
    .map((w) => `<div class="site warn">${escapeHtml(w)}</div>`)
    .join("");
  return `<b>${escapeHtml(properties.site_key)}</b><dl>` +
    rows.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join("") +
    "</dl>" +
    (properties.status_reason ? `<p>${escapeHtml(properties.status_reason)}</p>` : "") +
    warnings;
}

async function refreshMap() {
  if (!map) return;
  // Which layers the operator turned off must survive a refresh -- they are
  // turned off precisely when the map is too busy to read.
  const hidden = Object.keys(layers).filter((name) => !map.hasLayer(layers[name]));
  const collection = await api("/api/geojson");
  ["regions50", "regions90", "measurements", "nondetections", "estimates", "plan"].forEach(
    (name) => layers[name].clearLayers()
  );
  for (const feature of collection.features) {
    const properties = feature.properties || {};
    if (properties.kind === "measurement") {
      const detected = properties.detected;
      const marker = L.circleMarker(
        [feature.geometry.coordinates[1], feature.geometry.coordinates[0]],
        detected
          ? { radius: 7, weight: 2, color: "#ffffff", fillColor: levelColour(properties.level_db), fillOpacity: 0.95 }
          : { radius: 5, weight: 2, color: "#8b95a1", fillOpacity: 0, dashArray: "3 3" }
      ).bindPopup(measurementPopup(properties));
      marker.addTo(detected ? layers.measurements : layers.nondetections);
    } else if (properties.kind === "credible_region") {
      const inner = properties.credible_level === 0.5;
      L.geoJSON(feature, {
        style: {
          color: "#1f6feb",
          weight: inner ? 2 : 1,
          opacity: 0.9,
          fillColor: "#1f6feb",
          fillOpacity: inner ? 0.35 : 0.12,
          dashArray: properties.touches_analysed_edge ? "6 4" : null,
        },
      }).bindPopup(
        `<b>${escapeHtml(properties.site_key)}</b><dl>` +
        `<dt>region</dt><dd>${Math.round(properties.credible_level * 100)}% credible</dd>` +
        `<dt>area</dt><dd>${formatArea(properties.area_km2)}</dd>` +
        `<dt>status</dt><dd>${escapeHtml(properties.status)}</dd></dl>`
      ).addTo(inner ? layers.regions50 : layers.regions90);
    } else if (properties.kind === "plan_cell") {
      L.geoJSON(feature, {
        style: {
          stroke: false,
          fillColor: "#7a3ff2",
          fillOpacity: 0.06 + 0.34 * properties.value,
        },
      }).addTo(layers.plan);
    } else if (properties.kind === "plan_stop") {
      const helps = (properties.helps_most || []).map((h) => h.site_key).join(", ");
      L.marker([feature.geometry.coordinates[1], feature.geometry.coordinates[0]], {
        icon: L.divIcon({
          className: "",
          html: `<div style="width:26px;height:26px;border-radius:50%;background:#7a3ff2;color:#fff;
                 display:grid;place-items:center;font:700 13px system-ui;border:2px solid #fff;
                 box-shadow:0 1px 4px rgba(0,0,0,.4)">${properties.rank}</div>`,
          iconSize: [26, 26],
          iconAnchor: [13, 13],
        }),
      })
        .bindPopup(
          `<b>Suggested stop ${properties.rank}</b><dl>` +
            `<dt>value</dt><dd>${properties.value.toFixed(2)}</dd>` +
            `<dt>helps most</dt><dd>${escapeHtml(helps)}</dd></dl>` +
            "<p>A stop here is worth making because its outcome is hard to predict.</p>"
        )
        .addTo(layers.plan);
    } else if (properties.kind === "estimate") {
      L.marker([feature.geometry.coordinates[1], feature.geometry.coordinates[0]])
        .bindPopup(estimatePopup(properties))
        .addTo(layers.estimates);
    }
  }
  for (const name of hidden) {
    if (map.hasLayer(layers[name])) map.removeLayer(layers[name]);
  }
}

/* ----------------------------------------------------------------- sites */

function statusBadge(status) {
  if (status === "ok") return ["ok", "bounded region"];
  if (status === "unbounded_region") return ["warn", "unbounded"];
  if (status === "weak_geometry") return ["warn", "weak geometry"];
  // Not "not enough detections": the gate counts non-detections too, and a
  // site refused with four of them would read as though they had not counted.
  if (status === "insufficient_evidence") return ["none", "not enough evidence"];
  if (status === "frequency_unknown") return ["none", "no control channel known"];
  if (status === "no_measurements") return ["none", "nothing usable"];
  return ["none", status || "not solved"];
}

function renderPlan() {
  const box = $("#plan-box");
  const list = $("#plan-list");
  const reason = $("#plan-reason");
  const plan = (state.plan && state.plan.plan) || {};
  const stops = plan.top_stops || [];
  list.replaceChildren();
  // With no solvable site yet the map has nothing on it but the stops, which
  // reads as a broken app. The planner already knows why -- "no site has a
  // posterior to plan against yet" -- so say that instead of hiding the panel
  // and leaving the operator to guess whether anything is working.
  if (!stops.length) {
    const why = (state.plan && state.plan.reason) || "";
    box.hidden = !why;
    $("#plan-hint").hidden = true;
    reason.hidden = !why;
    reason.textContent = why;
    return;
  }
  box.hidden = false;
  reason.hidden = true;
  $("#plan-hint").hidden = false;
  stops.slice(0, 3).forEach((stop, index) => {
    const item = el("div", "plan-item");
    item.append(el("div", "rank", String(index + 1)));
    const body = el("div");
    body.append(el("div", "where", `${stop.latitude.toFixed(5)}, ${stop.longitude.toFixed(5)}`));
    body.append(
      el("div", "why", "helps: " + (stop.helps_most || []).map((h) => h.site_key).join(", "))
    );
    item.append(body);
    item.addEventListener("click", () => {
      if (map) map.setView([stop.latitude, stop.longitude], 14);
    });
    list.append(item);
  });
}

function renderStops() {
  const container = $("#stop-list");
  container.replaceChildren();
  for (const stop of state.stops || []) {
    const excluded = Boolean(stop.exclusion_reason);
    const card = el("div", "stop" + (excluded ? " excluded" : ""));
    const header = el("header");
    header.append(el("span", "key", stop.survey_run_id));
    header.append(
      el("span", "badge " + (excluded ? "warn" : "ok"), excluded ? "not counting" : "counting")
    );
    card.append(header);
    const when = (stop.capture_start_utc || "").replace("T", " ").slice(0, 16);
    card.append(
      el(
        "div",
        "meta",
        `${when} · ${stop.detections} detection(s), ${stop.non_detections} non-detection(s)` +
          (stop.gain !== null && stop.gain !== undefined ? ` · gain ${stop.gain}` : "") +
          (stop.gps_latitude !== null && stop.gps_latitude !== undefined
            ? ` · ${stop.gps_latitude.toFixed(4)}, ${stop.gps_longitude.toFixed(4)}`
            : " · no position")
      )
    );
    if (excluded) card.append(el("div", "meta", stop.exclusion_reason));

    const actions = el("div", "actions");
    const toggle = el("button", null, excluded ? "Put back" : "Set aside");
    toggle.addEventListener("click", async () => {
      const path = `/api/stops/${encodeURIComponent(stop.survey_run_id)}/` +
        (excluded ? "include" : "exclude");
      const body = excluded ? {} : { reason: "set aside by the operator in the field" };
      try {
        await api(path, { method: "POST", body: JSON.stringify(body) });
        await refreshState();
      } catch (error) {
        alert("Could not change the stop: " + error.message);
      }
    });
    actions.append(toggle);

    const remove = el("button", "danger", "Delete");
    remove.addEventListener("click", async () => {
      if (!confirm(`Delete ${stop.survey_run_id} and everything measured at it? This cannot be undone.`)) return;
      try {
        await api(`/api/stops/${encodeURIComponent(stop.survey_run_id)}/delete`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        await refreshState();
        await refreshMap();
      } catch (error) {
        alert("Could not delete the stop: " + error.message);
      }
    });
    actions.append(remove);
    card.append(actions);

    if (stop.gps_latitude !== null && stop.gps_latitude !== undefined) {
      card.addEventListener("dblclick", () => {
        if (map) map.setView([stop.gps_latitude, stop.gps_longitude], 14);
      });
    }
    container.append(card);
  }
  if (!(state.stops || []).length) {
    container.append(el("p", "hint", "No stops recorded yet."));
  }
}

async function showHistory(siteKey, target) {
  try {
    const payload = await api("/api/history/" + encodeURIComponent(siteKey));
    const areas = payload.history
      .map((entry) => entry.area_km2_90)
      .filter((value) => value !== null && value !== undefined);
    if (areas.length < 2) {
      target.textContent = "90% region: one solve so far";
      return;
    }
    const shown = areas.slice(-5).map((value) => formatArea(value));
    target.replaceChildren();
    target.append(document.createTextNode("90% region: " + shown.join(" → ")));
    if (areas[areas.length - 1] < areas[0]) {
      const factor = areas[0] / areas[areas.length - 1];
      const gain = el("b", null, `  (${factor.toFixed(1)}x tighter)`);
      target.append(gain);
    }
  } catch (error) {
    target.textContent = "history unavailable: " + error.message;
  }
}

function renderSites() {
  const container = $("#site-list");
  container.replaceChildren();
  let solved = 0;
  for (const site of state.sites) {
    if (site.status === "ok") solved += 1;
    const card = el("div", "site");
    const header = el("header");
    header.append(el("span", "key", site.site_key));
    const [badgeKind, badgeText] = statusBadge(site.status);
    header.append(el("span", "badge " + badgeKind, badgeText));
    card.append(header);

    const channels = (site.channels || [])
      .map((channel) => (channel.frequency_hz / 1e6).toFixed(6) + (channel.sharing_site_count > 1 ? "*" : ""))
      .join(", ") || "no frequency on record";
    card.append(el("div", "meta",
      `${channels} · ${site.detections} detection(s), ${site.non_detections} non-detection(s)` +
      (site.excluded ? `, ${site.excluded} excluded` : "") +
      (site.area_km2_90 ? ` · 90% ${formatArea(site.area_km2_90)}` : "")));

    if (site.status_reason) card.append(el("div", "meta", site.status_reason));
    for (const warning of site.warnings || []) card.append(el("div", "warn", warning));

    if (site.solved_at) {
      const trend = el("div", "trend", "90% region: …");
      card.append(trend);
      showHistory(site.site_key, trend);
    }

    if (site.mode_latitude !== null && site.mode_latitude !== undefined) {
      card.style.cursor = "pointer";
      card.addEventListener("click", () => {
        if (map) map.setView([site.mode_latitude, site.mode_longitude], 14);
      });
    }
    container.append(card);
  }
  $("#site-summary").textContent =
    `${solved} of ${state.sites.length} site(s) have a bounded region`;
}

/* ------------------------------------------------------------------ jobs */

function jobLog(message) {
  const list = $("#job-log");
  list.prepend(el("li", null, message));
  while (list.children.length > 200) list.lastChild.remove();
}

function watchJob(jobId, cursor = 0) {
  state.jobId = jobId;
  state.cursor = cursor;
  $("#job").hidden = false;
  if (!cursor) $("#job-log").replaceChildren();
  $("#record").disabled = true;
  $("#resolve").disabled = true;
  $("#cancel-job").hidden = false;
  if (state.stream) state.stream.close();

  const query = new URLSearchParams({ cursor: String(cursor) });
  if (TOKEN) query.set("token", TOKEN);
  const url = "/api/jobs/" + jobId + "/events?" + query.toString();
  const stream = new EventSource(url);
  state.stream = stream;
  stream.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.stage === "closed") {
      stream.close();
      finishJob(jobId);
      return;
    }
    state.cursor += 1;
    $("#progress-bar").style.width = Math.round((data.progress || 0) * 100) + "%";
    $("#job-stage").textContent = `${data.stage}: ${data.message}`;
    jobLog(`${(data.at || "").slice(11, 19)} ${data.stage} — ${data.message}`);
  };
  // A phone that loses Wi-Fi mid-capture drops the stream, but the capture
  // keeps running on the Pi. Reporting it as finished would tell the operator
  // to drive away from a stop that is still recording, so the client asks the
  // server what actually happened and resumes from where it left off.
  stream.onerror = async () => {
    stream.close();
    if (state.jobId !== jobId) return;
    try {
      const job = await api("/api/jobs/" + jobId);
      if (job.status === "running" || job.status === "pending") {
        jobLog("connection lost — the capture is still running, reconnecting…");
        setTimeout(() => { if (state.jobId === jobId) watchJob(jobId, state.cursor); }, 2000);
        return;
      }
    } catch (_) {
      jobLog("connection lost — retrying…");
      setTimeout(() => { if (state.jobId === jobId) watchJob(jobId, state.cursor); }, 4000);
      return;
    }
    finishJob(jobId);
  };
}

async function finishJob(jobId) {
  $("#record").disabled = false;
  $("#resolve").disabled = false;
  $("#cancel-job").hidden = true;
  state.jobId = null;
  try {
    const job = await api("/api/jobs/" + jobId);
    if (job.status === "failed") {
      jobLog("FAILED: " + job.error);
      $("#job-stage").textContent = "failed: " + job.error;
    } else if (job.status === "cancelled") {
      $("#job-stage").textContent = "cancelled";
    } else {
      $("#job-stage").textContent = "complete";
    }
  } catch (error) {
    jobLog("could not read the job result: " + error.message);
  }
  await refreshState();
  await refreshMap();
}

/* --------------------------------------------------------------- actions */

async function savePosition(latitude, longitude, accuracy, source) {
  try {
    state.position = await api("/api/position", {
      method: "POST",
      body: JSON.stringify({
        latitude, longitude, accuracy_m: accuracy,
        label: $("#stop-label").value, source,
      }),
    });
    renderPosition();
    if (map) map.setView([latitude, longitude], Math.max(map.getZoom(), 14));
  } catch (error) {
    alert("Could not save the position: " + error.message);
  }
}

function useDeviceGps() {
  if (!navigator.geolocation) {
    alert("This browser does not expose a GPS position.");
    return;
  }
  const button = $("#use-gps");
  button.disabled = true;
  button.textContent = "locating…";
  navigator.geolocation.getCurrentPosition(
    (fix) => {
      button.disabled = false;
      button.textContent = "Use phone GPS";
      savePosition(fix.coords.latitude, fix.coords.longitude, fix.coords.accuracy, "device");
    },
    (error) => {
      button.disabled = false;
      button.textContent = "Use phone GPS";
      alert(
        "Could not get a GPS fix: " + error.message +
        "\n\nBrowsers only expose location over HTTPS or from localhost. " +
        "Tap the map to place your position instead."
      );
    },
    { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 }
  );
}

async function startCapture(confirmPosition = false) {
  // Disabled on the way IN, not once the POST answers. The server probes the
  // SDR before it replies, and on a phone that pause is long enough for a
  // second tap to land: the first request starts the capture, the second is
  // refused, and the refusal is the only thing the operator sees.
  const button = $("#record");
  button.disabled = true;
  const body = {
    duration_seconds: Number($("#duration").value),
    center_frequency_hz: Number($("#center").value) * 1e6,
    sample_rate_hz: Number($("#rate").value) * 1e6,
    if_gain_reduction_db: Number($("#ifgr").value),
    lna_state: Number($("#lna").value),
    label: $("#stop-label").value,
    solve: true,
  };
  if (confirmPosition) body.confirm_position = true;
  try {
    const job = await api("/api/capture", { method: "POST", body: JSON.stringify(body) });
    watchJob(job.job_id);  // keeps the button disabled until the job ends
  } catch (error) {
    // A stale marked position is recoverable, and recording a stop against
    // the PREVIOUS stop's coordinates is the one mistake that silently
    // corrupts a whole campaign -- so it asks rather than proceeding.
    if (error.needsPositionConfirmation) {
      button.disabled = false;
      if (confirm(error.message + "\n\nRecord this stop at the marked position anyway?")) {
        await startCapture(true);
      }
      return;
    }
    // The race the comment above describes: a duplicate tap that landed
    // during the SDR probe loses the claim on the job slot even though it
    // is the SAME operator's SAME recording that is now running under the
    // other request. Before alarming them over their own successful tap,
    // check whether a capture is in fact under way -- if so, it is theirs.
    try {
      const jobs = (await api("/api/jobs")).jobs || [];
      const running = jobs.find(
        (job) => job.kind === "capture" && (job.status === "running" || job.status === "pending")
      );
      if (running) {
        watchJob(running.job_id);
        return;
      }
    } catch (_) {
      /* the server did not answer either; fall through to the real error */
    }
    button.disabled = false;
    alert("Could not start the recording: " + error.message);
  }
}

async function purgeRecordings() {
  if (!confirm("Delete every kept recording? Their measurements are already stored; only the raw IQ goes.")) return;
  try {
    const result = await api("/api/recordings/purge", { method: "POST", body: JSON.stringify({}) });
    alert(`Freed ${result.freed_gib} GiB from ${result.deleted_count} recording(s).`);
    await refreshState();
  } catch (error) {
    alert("Could not free disk: " + error.message);
  }
}

async function startSolve() {
  try {
    const job = await api("/api/solve", {
      method: "POST",
      body: JSON.stringify({ rebuild_measurements: true }),
    });
    watchJob(job.job_id);
  } catch (error) {
    alert("Could not start the solve: " + error.message);
  }
}

/* ----------------------------------------------------------------- drive */

/* Browsers refuse geolocation outside a secure context. Said here, in the
 * one place it stops the operator, with the exact fix rather than a generic
 * permission complaint -- over plain HTTP the prompt never even appears. */
function checkSecureContext() {
  const notice = $("#drive-insecure");
  if (window.isSecureContext) {
    notice.hidden = true;
    return true;
  }
  notice.hidden = false;
  notice.textContent =
    "This page is not a secure context, so the browser will not share GPS and a drive " +
    "cannot run. Restart the app with --tls and reload over https://, accepting the " +
    "certificate warning once.";
  $("#drive-share").disabled = true;
  $("#drive-start").disabled = true;
  return false;
}

function renderDriveGps() {
  const readout = $("#drive-gps");
  if (live.watchId === null) {
    readout.textContent = live.gpsError || "not sharing";
    return;
  }
  if (!live.lastFix) {
    readout.textContent = "waiting for a fix…";
    return;
  }
  const age = Math.round((Date.now() - live.lastFix.at) / 1000);
  const accuracy = live.lastFix.accuracy ? ` ±${Math.round(live.lastFix.accuracy)} m` : "";
  readout.textContent =
    `${live.lastFix.latitude.toFixed(5)}, ${live.lastFix.longitude.toFixed(5)}${accuracy}` +
    ` · ${age} s ago · ${live.fixes} fix(es)` +
    (live.postError ? ` · not reaching the Pi: ${live.postError}` : "");
}

async function sendFix(fix) {
  live.fixes += 1;
  live.lastFix = {
    latitude: fix.coords.latitude,
    longitude: fix.coords.longitude,
    accuracy: fix.coords.accuracy,
    at: Date.now(),
  };
  renderDriveGps();
  // One request in flight at a time. Fixes arrive about once a second and the
  // server keeps only the latest, so dropping one while another is on the
  // wire loses nothing -- while queueing them on a slow link would deliver
  // stale positions late, which is the one error that misplaces a measurement.
  if (live.posting) return;
  live.posting = true;
  try {
    await api("/api/live/position", {
      method: "POST",
      body: JSON.stringify({
        latitude: fix.coords.latitude,
        longitude: fix.coords.longitude,
        accuracy_m: fix.coords.accuracy,
        speed_mps: fix.coords.speed,
      }),
    });
    live.postError = "";
  } catch (error) {
    live.postError = error.message;
  } finally {
    live.posting = false;
    renderDriveGps();
  }
}

function startLocationSharing() {
  if (!navigator.geolocation) {
    alert("This browser does not expose a GPS position.");
    return;
  }
  live.gpsError = "";
  live.watchId = navigator.geolocation.watchPosition(
    sendFix,
    (error) => {
      live.gpsError = "GPS unavailable: " + error.message;
      renderDriveGps();
    },
    { enableHighAccuracy: true, maximumAge: 0, timeout: 30000 }
  );
  $("#drive-share").textContent = "Stop sharing";
  $("#drive-share").classList.add("armed");
  renderDriveGps();
}

function stopLocationSharing() {
  if (live.watchId !== null) navigator.geolocation.clearWatch(live.watchId);
  live.watchId = null;
  $("#drive-share").textContent = "Share my location";
  $("#drive-share").classList.remove("armed");
  renderDriveGps();
}

async function startDrive() {
  // Disabled on the way IN, not once the POST answers, for the same reason
  // Record does this: the server probes the SDR before it replies, and on a
  // phone that pause is long enough for a second tap -- or a second tab --
  // to land.
  const button = $("#drive-start");
  button.disabled = true;
  if (live.watchId === null) startLocationSharing();
  try {
    const job = await api("/api/live/start", {
      method: "POST",
      body: JSON.stringify({
        max_seconds: Number($("#drive-minutes").value) * 60,
        center_frequency_hz: Number($("#center").value) * 1e6,
        sample_rate_hz: Number($("#rate").value) * 1e6,
        if_gain_reduction_db: Number($("#ifgr").value),
        lna_state: Number($("#lna").value),
      }),
    });
    live.jobId = job.job_id;
    live.binsDrawn = 0;
    live.announcedBins = 0;
    live.gpsLost = false;
    live.pullOverBin = null;
    if (layers.bins) layers.bins.clearLayers();
    if (layers.track) layers.track.clearLayers();
    holdScreenAwake();
    speak("Drive started. Measuring.");
    watchJob(job.job_id);
    pollLive();
  } catch (error) {
    // The duplicate-tap race Record has: this endpoint also probes real
    // hardware before it claims the job slot, so a second request that
    // landed during that probe can lose the claim even though it is the
    // SAME drive the operator meant to start that is now running under the
    // other request. Check before alarming them over their own tap.
    try {
      const status = await api("/api/live");
      if (status.running) {
        live.jobId = status.job_id;
        live.binsDrawn = 0;
        if (layers.bins) layers.bins.clearLayers();
        if (layers.track) layers.track.clearLayers();
        watchJob(status.job_id);
        pollLive();
        return;
      }
    } catch (_) {
      /* the server did not answer either; fall through to the real error */
    }
    button.disabled = false;
    alert("Could not start the drive: " + error.message);
  }
}

async function solveNow() {
  const button = $("#drive-solve");
  button.disabled = true;
  try {
    const result = await api("/api/live/solve", { method: "POST", body: "{}" });
    if (!result.started) jobLog("solve not started: " + result.reason);
  } catch (error) {
    alert("Could not solve: " + error.message);
  } finally {
    button.disabled = false;
  }
}

async function requestHold() {
  const button = $("#drive-hold");
  button.disabled = true;
  try {
    const result = await api("/api/live/hold", { method: "POST", body: JSON.stringify({ seconds: 60 }) });
    if (result.accepted) speak("Stay still. Measuring for sixty seconds.", { urgent: true });
    else jobLog("hold not started: " + result.reason);
  } catch (error) {
    alert("Could not start the measurement: " + error.message);
  } finally {
    button.disabled = false;
  }
}

async function stopDrive() {
  if (!live.jobId) return;
  $("#drive-stop").disabled = true;
  try {
    await api("/api/jobs/" + live.jobId + "/cancel", { method: "POST" });
  } catch (error) {
    alert("Could not stop the drive: " + error.message);
  } finally {
    $("#drive-stop").disabled = false;
  }
}

const DRIVE_STATS = [
  ["bins_written", "bins measured"],
  ["windows_recorded", "windows used"],
  ["holds_written", "stationary measurements"],
  ["bins_superseded", "earlier bins replaced"],
  ["bins_analysed_inline", "bins that stalled the stream"],
  ["bins_failed", "bins lost to an error"],
  ["bins_capped", "bins filled while stopped"],
  ["windows_dwelled", "windows while parked"],
  ["windows_revisited", "windows on measured road"],
  ["windows_held_apart", "windows held back in traffic"],
  ["windows_too_fast", "windows dropped, too fast"],
  ["windows_without_position", "windows with no fix"],
  ["bins_too_short", "bins dropped, too few windows"],
  ["overflow_count", "driver overflows"],
];

function renderLive(payload) {
  if (!payload) return;
  const running = Boolean(payload.running);
  $("#drive-start").hidden = running;
  $("#drive-stop").hidden = !running;
  $("#drive-solve").hidden = !running;
  $("#drive-solve").disabled = Boolean(payload.solving);
  if (!running) $("#drive-start").disabled = !window.isSecureContext;
  if (running && payload.job_id) live.jobId = payload.job_id;

  const status = $("#drive-status");
  if (running) {
    status.hidden = false;
    status.className = "notice";
    const solve = payload.last_solve;
    status.textContent =
      `Driving · ${payload.bin_count} bin(s) written` +
      (payload.solving ? " · solving in the background" : "") +
      (solve ? ` · last solve: ${solve.solved} of ${solve.sites} site(s) bounded` : "");
  } else if (payload.bin_count) {
    status.hidden = false;
    status.className = "notice";
    status.textContent = `Last drive wrote ${payload.bin_count} bin(s).`;
  } else {
    status.hidden = true;
  }

  const stats = payload.stats || {};

  // -- the HUD: three numbers, readable at a glance -------------------------
  const hud = $("#drive-hud");
  hud.hidden = !running;
  if (running) {
    const hold = payload.hold || {};
    const bins = payload.bins || [];
    const last = bins.length ? bins[bins.length - 1] : null;
    const gpsAge = payload.position_age_seconds;
    const gpsStale = gpsAge === null || gpsAge === undefined || gpsAge > 5;

    $("#hud-bins").textContent = hold.active
      ? `${Math.ceil(hold.seconds_left)}s`
      : String(payload.bin_count || 0);
    $("#hud-bins").parentElement.querySelector(".hud-label").textContent = hold.active ? "measuring, stay still" : "bins";
    $("#hud-sites").textContent = last ? String(last.observations) : "–";
    const gpsCell = $("#hud-gps").parentElement;
    $("#hud-gps").textContent = gpsStale ? "LOST" : `${Math.round(gpsAge)}s`;
    gpsCell.classList.toggle("bad", gpsStale);
    gpsCell.classList.toggle("ok", !gpsStale);

    // GPS, edge-triggered both ways.
    if (gpsStale && !live.gpsLost) { live.gpsLost = true; speak("GPS lost.", { urgent: true }); }
    if (!gpsStale && live.gpsLost) { live.gpsLost = false; speak("GPS back."); }

    // Progress, every five bins rather than every bin.
    const count = payload.bin_count || 0;
    if (count >= live.announcedBins + 5) { live.announcedBins = count; speak(`${count} bins.`); }

    // Hold finished: the one message a pulled-over driver is waiting for.
    if (live.holdWasActive && !hold.active) speak("Done. You can drive.", { urgent: true });
    live.holdWasActive = Boolean(hold.active);

    // Pull-over hint, once per bin that raised it.
    const hint = payload.pull_over || {};
    const notice = $("#drive-pullover");
    const holdRow = $("#drive-hold-row");
    if (hint.suggest && !hold.active) {
      const names = (hint.sites || []).map((s) => s.site_key.split(":").slice(-2).join(":")).join(", ");
      notice.hidden = false;
      notice.className = "notice pullover";
      notice.textContent = `Worth a stop here: ${names} just under the gate. ${hint.reason}`;
      holdRow.hidden = false;
      if (hint.bin && hint.bin !== live.pullOverBin) {
        live.pullOverBin = hint.bin;
        speak(`Worth a stop here. ${(hint.sites || []).length} site${(hint.sites || []).length === 1 ? "" : "s"} just under the gate.`);
      }
    } else if (hold.active) {
      notice.hidden = false;
      notice.className = "notice";
      notice.textContent = `Measuring here — ${Math.ceil(hold.seconds_left)} s left. Stay still.`;
      holdRow.hidden = true;
    } else {
      notice.hidden = true;
      holdRow.hidden = true;
    }
  } else {
    $("#drive-pullover").hidden = true;
    $("#drive-hold-row").hidden = true;
  }

  const span = $("#drive-span");
  if (stats.bin_size_m) {
    span.textContent =
      `${Math.round(stats.bin_size_m)} m per measurement` +
      (stats.speed_kmh ? ` · ${Math.round(stats.speed_kmh)} km/h` : "");
  } else if (!running) {
    span.textContent = "follows your speed";
  }
  const box = $("#drive-stats");
  box.replaceChildren();
  const shown = { ...stats };
  if (running) shown.bins_written = payload.bin_count;
  for (const [key, label] of DRIVE_STATS) {
    const value = shown[key];
    if (value === undefined) continue;
    const row = el("div", "stat" + (value ? "" : " muted"));
    row.appendChild(el("span", null, label));
    row.appendChild(el("b", null, String(value)));
    box.appendChild(row);
  }

  if (map && layers.bins) {
    // Redrawn only when there are new bins: a poll every two seconds must not
    // rebuild the whole layer while the operator is panning the map.
    const bins = payload.bins || [];
    if (bins.length !== live.binsDrawn) {
      layers.bins.clearLayers();
      for (const bin of bins) {
        L.circleMarker([bin.latitude, bin.longitude], {
          radius: 5,
          weight: 1,
          color: "#0b7a5f",
          fillColor: "#10a37f",
          fillOpacity: 0.75,
        })
          .bindPopup(
            `<b>Drive bin</b><dl>` +
              `<dt>windows</dt><dd>${bin.windows}</dd>` +
              `<dt>observations</dt><dd>${bin.observations}</dd>` +
              `<dt>spread</dt><dd>${bin.spread_m} m</dd></dl>` +
              "<p>One virtual stop. It contributes exactly as a recorded stop does.</p>"
          )
          .addTo(layers.bins);
      }
      live.binsDrawn = bins.length;
    }
    const trail = payload.trail || [];
    layers.track.clearLayers();
    if (trail.length > 1) {
      L.polyline(trail.map((point) => [point.latitude, point.longitude]), {
        color: "#10a37f",
        weight: 3,
        opacity: 0.5,
      }).addTo(layers.track);
    }
  }

  // A background solve finishing is the only thing that changes the regions
  // mid-drive, so the map is refetched then and not on every poll.
  const solvedAt = payload.last_solve ? payload.last_solve.at : null;
  if (solvedAt && solvedAt !== live.lastSolveAt) {
    live.lastSolveAt = solvedAt;
    refreshMap().catch(() => {});
  }
}

async function pollLive() {
  if (live.timer) clearTimeout(live.timer);
  let payload = null;
  try {
    payload = await api("/api/live");
    renderLive(payload);
  } catch (_) {
    /* a dropped link is not a reason to stop polling */
  }
  renderDriveGps();
  if (payload && payload.running) {
    live.timer = setTimeout(pollLive, 2000);
  } else {
    if (live.jobId && payload) speak(`Drive stopped. ${payload.bin_count || 0} bins.`);
    live.timer = null;
    live.jobId = null;
    releaseScreen();
  }
}

/* ----------------------------------------------------------------- setup */

async function refreshState() {
  const payload = await api("/api/state");
  state.settings = payload.settings;
  state.position = payload.position;
  state.sites = payload.sites;
  state.stops = payload.stops || [];
  state.plan = payload.plan || null;
  renderPosition();
  renderSites();
  renderStops();
  renderPlan();
  renderLive(payload.live);

  const disk = payload.disk;
  const diskPill = $("#disk");
  const diskNotice = $("#disk-status");
  if (disk) {
    diskPill.textContent = `${formatGiB(disk.free_bytes)} free · ${disk.captures_that_fit} stop(s)`;
    diskPill.className = "pill" + (disk.ready ? (disk.captures_that_fit <= 2 ? " bad" : " ok") : " bad");
    if (!disk.ready) {
      diskNotice.hidden = false;
      diskNotice.className = "notice error";
      diskNotice.textContent = disk.reason;
    } else if (disk.captures_that_fit <= 2) {
      diskNotice.hidden = false;
      diskNotice.className = "notice";
      diskNotice.textContent =
        `Only ${disk.captures_that_fit} more stop(s) fit. Each is ${formatGiB(disk.per_capture_bytes)}; ` +
        `${disk.retained_count} recording(s) are being kept. Use "Free disk" on the Sites tab to clear them.`;
    } else {
      diskNotice.hidden = true;
    }
  }

  const age = payload.position_age_seconds;
  if (age !== null && age !== undefined && state.position && state.position.latitude !== null) {
    const minutes = Math.round(age / 60);
    const readout = $("#position-readout");
    readout.textContent += minutes >= 1 ? ` · marked ${minutes} min ago` : " · marked just now";
  }

  const device = payload.device;
  const notice = $("#device-status");
  if (device.available) {
    setConnection(device.resolved_label || "SDR ready", "ok");
    notice.hidden = true;
  } else {
    setConnection("no SDR", "bad");
    notice.hidden = false;
    notice.textContent = device.probe_error || "no SDR device found";
  }
  return payload;
}

function applyDefaults() {
  const settings = state.settings;
  $("#duration").value = settings.duration_seconds;
  $("#center").value = (settings.center_frequency_hz / 1e6).toFixed(6);
  $("#rate").value = settings.sample_rate_hz / 1e6;
  $("#ifgr").value = settings.if_gain_reduction_db;
  $("#lna").value = settings.lna_state;
}

function wireUi() {
  document.querySelectorAll("#tabs button").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("#tabs button").forEach((other) => other.classList.remove("active"));
      button.classList.add("active");
      document.querySelectorAll(".panel").forEach((panel) => {
        panel.hidden = panel.dataset.panel !== button.dataset.panel;
      });
    });
  });

  $("#use-gps").addEventListener("click", useDeviceGps);
  $("#pick-on-map").addEventListener("click", () => {
    state.picking = !state.picking;
    $("#pick-on-map").classList.toggle("armed", state.picking);
  });
  $("#save-position").addEventListener("click", () => {
    const position = state.position;
    if (!position || position.latitude === null || position.latitude === undefined) {
      alert("Set a position first, with the phone's GPS or by tapping the map.");
      return;
    }
    savePosition(position.latitude, position.longitude, position.accuracy_m, position.source === "browser_gps" ? "device" : "manual");
  });
  $("#record").addEventListener("click", () => startCapture());
  $("#purge").addEventListener("click", purgeRecordings);
  $("#drive-share").addEventListener("click", () => {
    if (live.watchId === null) startLocationSharing();
    else stopLocationSharing();
  });
  $("#drive-start").addEventListener("click", startDrive);
  $("#drive-solve").addEventListener("click", solveNow);
  $("#drive-hold").addEventListener("click", requestHold);
  $("#drive-stop").addEventListener("click", stopDrive);
  for (const [id, format] of [["#export-kml", "kml"], ["#export-gpx", "gpx"]]) {
    $(id).addEventListener("click", () => {
      const query = new URLSearchParams({ format });
      if (TOKEN) query.set("token", TOKEN);
      window.open("/api/export?" + query.toString(), "_blank");
    });
  }
  $("#resolve").addEventListener("click", startSolve);
  $("#cancel-job").addEventListener("click", async () => {
    if (state.jobId) await api("/api/jobs/" + state.jobId + "/cancel", { method: "POST" });
  });

  const toggles = {
    "layer-measurements": "measurements",
    "layer-nondetections": "nondetections",
    "layer-estimates": "estimates",
    "layer-plan": "plan",
    "layer-bins": "bins",
    "layer-track": "track",
  };
  for (const [id, name] of Object.entries(toggles)) {
    $("#" + id).addEventListener("change", (event) => {
      if (!map) return;
      if (event.target.checked) layers[name].addTo(map);
      else map.removeLayer(layers[name]);
    });
  }
  $("#layer-regions").addEventListener("change", (event) => {
    if (!map) return;
    for (const name of ["regions50", "regions90"]) {
      if (event.target.checked) layers[name].addTo(map);
      else map.removeLayer(layers[name]);
    }
  });

  // Drag the sheet to trade map area against controls -- both matter, and
  // which one matters more changes between "where am I" and "what did we find".
  const grabber = $("#grabber");
  let dragging = false;
  const setHeight = (pixels) => {
    const height = Math.min(Math.max(pixels, 120), window.innerHeight - 120);
    document.documentElement.style.setProperty("--sheet-height", height + "px");
    if (map) map.invalidateSize();
  };
  const move = (event) => {
    if (!dragging) return;
    const y = event.touches ? event.touches[0].clientY : event.clientY;
    setHeight(window.innerHeight - y);
    event.preventDefault();
  };
  const stop = () => { dragging = false; };
  grabber.addEventListener("mousedown", () => { dragging = true; });
  grabber.addEventListener("touchstart", () => { dragging = true; }, { passive: true });
  window.addEventListener("mousemove", move);
  window.addEventListener("touchmove", move, { passive: false });
  window.addEventListener("mouseup", stop);
  window.addEventListener("touchend", stop);
}

async function boot() {
  wireUi();
  try {
    const payload = await refreshState();
    applyDefaults();
    await ensureLeaflet();
    if (initMap()) {
      renderPosition();
      await refreshMap();
    }
    checkSecureContext();
    const running = (payload.jobs || []).find(
      (job) => job.status === "running" || job.status === "pending"
    );
    if (running) watchJob(running.job_id);
    // A drive survives the phone locking, the browser being closed, or a
    // reload: it is running on the Pi. Rejoining it is the difference between
    // a lost drive and a paused screen.
    if (payload.live && payload.live.running) {
      live.jobId = payload.live.job_id;
      pollLive();
    }
  } catch (error) {
    setConnection("offline", "bad");
    $("#sheet").prepend(el("div", "notice error", "Could not reach the server: " + error.message));
  }
}

boot();
