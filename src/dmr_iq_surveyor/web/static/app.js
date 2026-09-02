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
  picking: false,
  jobId: null,
  stream: null,
};

/* ---------------------------------------------------------------- helpers */

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (TOKEN) headers["X-Auth-Token"] = TOKEN;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, Object.assign({}, options, { headers }));
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch (_) { payload = { error: text }; }
  if (!response.ok) throw new Error((payload && payload.error) || `HTTP ${response.status}`);
  return payload;
}

const $ = (selector) => document.querySelector(selector);
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

function initMap() {
  if (typeof L === "undefined") {
    const banner = el("div", "notice error",
      "The map library could not be loaded. Check the phone's internet connection, " +
      "or place leaflet.js and leaflet.css in the server's static directory.");
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
    rows.map(([key, value]) => `<dt>${key}</dt><dd>${value}</dd>`).join("") + "</dl>";
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
  const warnings = (properties.warnings || []).map((w) => `<div class="site warn">${w}</div>`).join("");
  return `<b>${properties.site_key}</b><dl>` +
    rows.map(([key, value]) => `<dt>${key}</dt><dd>${value}</dd>`).join("") + "</dl>" +
    (properties.status_reason ? `<p>${properties.status_reason}</p>` : "") + warnings;
}

async function refreshMap() {
  if (!map) return;
  const collection = await api("/api/geojson");
  ["regions50", "regions90", "measurements", "nondetections", "estimates"].forEach(
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
        `<b>${properties.site_key}</b><dl>` +
        `<dt>region</dt><dd>${Math.round(properties.credible_level * 100)}% credible</dd>` +
        `<dt>area</dt><dd>${formatArea(properties.area_km2)}</dd>` +
        `<dt>status</dt><dd>${properties.status}</dd></dl>`
      ).addTo(inner ? layers.regions50 : layers.regions90);
    } else if (properties.kind === "estimate") {
      L.marker([feature.geometry.coordinates[1], feature.geometry.coordinates[0]])
        .bindPopup(estimatePopup(properties))
        .addTo(layers.estimates);
    }
  }
}

/* ----------------------------------------------------------------- sites */

function statusBadge(status) {
  if (status === "ok") return ["ok", "bounded region"];
  if (status === "unbounded_region") return ["warn", "unbounded"];
  if (status === "weak_geometry") return ["warn", "weak geometry"];
  if (status === "insufficient_evidence") return ["none", "not enough detections"];
  if (status === "frequency_unknown") return ["none", "no control channel known"];
  if (status === "no_measurements") return ["none", "nothing usable"];
  return ["none", status || "not solved"];
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

function watchJob(jobId) {
  state.jobId = jobId;
  $("#job").hidden = false;
  $("#job-log").replaceChildren();
  $("#record").disabled = true;
  $("#resolve").disabled = true;
  $("#cancel-job").hidden = false;
  if (state.stream) state.stream.close();

  const url = "/api/jobs/" + jobId + "/events" + (TOKEN ? "?token=" + encodeURIComponent(TOKEN) : "");
  const stream = new EventSource(url);
  state.stream = stream;
  stream.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.stage === "closed") {
      stream.close();
      finishJob(jobId);
      return;
    }
    $("#progress-bar").style.width = Math.round((data.progress || 0) * 100) + "%";
    $("#job-stage").textContent = `${data.stage}: ${data.message}`;
    jobLog(`${(data.at || "").slice(11, 19)} ${data.stage} — ${data.message}`);
  };
  stream.onerror = () => { stream.close(); finishJob(jobId); };
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

async function startCapture() {
  try {
    const job = await api("/api/capture", {
      method: "POST",
      body: JSON.stringify({
        duration_seconds: Number($("#duration").value),
        center_frequency_hz: Number($("#center").value) * 1e6,
        sample_rate_hz: Number($("#rate").value) * 1e6,
        if_gain_reduction_db: Number($("#ifgr").value),
        lna_state: Number($("#lna").value),
        label: $("#stop-label").value,
        solve: true,
      }),
    });
    watchJob(job.job_id);
  } catch (error) {
    alert("Could not start the recording: " + error.message);
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

/* ----------------------------------------------------------------- setup */

async function refreshState() {
  const payload = await api("/api/state");
  state.settings = payload.settings;
  state.position = payload.position;
  state.sites = payload.sites;
  renderPosition();
  renderSites();

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
  $("#record").addEventListener("click", startCapture);
  $("#resolve").addEventListener("click", startSolve);
  $("#cancel-job").addEventListener("click", async () => {
    if (state.jobId) await api("/api/jobs/" + state.jobId + "/cancel", { method: "POST" });
  });

  const toggles = {
    "layer-measurements": "measurements",
    "layer-nondetections": "nondetections",
    "layer-estimates": "estimates",
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
    if (initMap()) {
      renderPosition();
      await refreshMap();
    }
    const running = (payload.jobs || []).find(
      (job) => job.status === "running" || job.status === "pending"
    );
    if (running) watchJob(running.job_id);
  } catch (error) {
    setConnection("offline", "bad");
    $("#sheet").prepend(el("div", "notice error", "Could not reach the server: " + error.message));
  }
}

boot();
