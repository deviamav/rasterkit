/* RasterKit front-end (vanilla JS, no build step). */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const RAMP = ["#a50026", "#d73027", "#f46d43", "#fdae61", "#fee08b", "#d9ef8b", "#a6d96a", "#66bd63", "#1a9850", "#006837"];
  const SERIES_COLORS = ["#2de2d0", "#f5b544", "#5aa9ff", "#ff7b72", "#a3e635", "#e2e8f0", "#fb923c", "#4fd18b", "#fde047", "#22d3ee"];
  const RANGES = { NDVI: [-0.1, 0.9], NDWI: [-0.6, 0.6], EVI: [-0.1, 0.9], NDMI: [-0.4, 0.6], CUSTOM: [-1, 1] };

  const state = {
    config: null,
    geojson: null,
    jobId: null,
    polling: null,
    logCursor: 0,
    rows: [],
    summary: null,
    synthetic: false,
    selectedField: "",
    sort: { key: "image_date", dir: 1 },
    sceneDate: "",
    colors: {},
  };

  /* ------------------------------------------------------------ helpers */
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (v, d = 4) => (v === null || v === undefined || Number.isNaN(v) ? "-" : Number(v).toFixed(d));

  async function api(path, opts) {
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { /* non-JSON error body */ }
    if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
    return data;
  }

  function showBanner(msg) {
    const b = $("banner");
    b.textContent = msg || "";
    b.classList.toggle("show", !!msg);
  }

  function rampColor(t) {
    t = Math.max(0, Math.min(1, t));
    const p = t * (RAMP.length - 1);
    const i = Math.min(Math.floor(p), RAMP.length - 2);
    const f = p - i;
    const a = RAMP[i].match(/\w\w/g).map((h) => parseInt(h, 16));
    const b = RAMP[i + 1].match(/\w\w/g).map((h) => parseInt(h, 16));
    return "rgb(" + a.map((v, k) => Math.round(v + (b[k] - v) * f)).join(",") + ")";
  }

  /* ------------------------------------------------------------ theme */
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("rasterkit-theme", t); } catch (_) { /* storage unavailable */ }
    if (state.rows.length) drawChart();
  }
  $("themeBtn").addEventListener("click", () => {
    applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
  });

  /* ------------------------------------------------------------ map */
  const map = L.map("map", { zoomControl: true, attributionControl: true }).setView([30, 10], 2);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);
  L.control.scale({ imperial: false, position: "bottomright" }).addTo(map);
  const coordsEl = $("mapCoords");
  const pad = (v, n) => (v < 0 ? "-" : " ") + Math.abs(v).toFixed(4).padStart(n, "0");
  map.on("mousemove", (e) => {
    const ll = e.latlng.wrap();
    coordsEl.textContent = "lat " + pad(ll.lat, 7) + "   lon " + pad(ll.lng, 8);
  });
  let polyLayer = null;
  let overlay = null;

  function styleFor(feature) {
    const id = feature.properties.field_id;
    const val = state.fieldValues && state.fieldValues[id];
    const [vmin, vmax] = currentRange();
    const sel = id === state.selectedField;
    return {
      color: sel ? "#ffffff" : "#2de2d0",
      weight: sel ? 2.4 : 1.2,
      fillColor: val === undefined || val === null ? "#2de2d0" : rampColor((val - vmin) / (vmax - vmin)),
      fillOpacity: val === undefined || val === null ? 0.18 : 0.62,
    };
  }

  function drawGeometry(geojson) {
    if (polyLayer) map.removeLayer(polyLayer);
    polyLayer = L.geoJSON(geojson, {
      style: styleFor,
      onEachFeature: (f, layer) => {
        layer.on("click", () => selectField(f.properties.field_id === state.selectedField ? "" : f.properties.field_id));
        layer.bindTooltip(() => {
          const id = f.properties.field_id;
          const v = state.fieldValues && state.fieldValues[id];
          return esc(id) + (v === undefined || v === null ? "" : "<br>" + esc(state.indexLabel || "value") + ": <b>" + fmt(v) + "</b>");
        }, { sticky: true });
      },
    }).addTo(map);
    map.fitBounds(polyLayer.getBounds(), { padding: [30, 30], maxZoom: 16 });
    $("mapEmpty").style.display = "none";
  }

  function restyle() { if (polyLayer) polyLayer.setStyle(styleFor); }

  function currentRange() {
    const o = state.summary && state.summary.overlays && state.summary.overlays[0];
    if (o) return [o.vmin, o.vmax];
    return RANGES[state.indexKey || $("indexSelect").value] || [-1, 1];
  }

  /* ------------------------------------------------------------ geometry input */
  function setGeometry(geojson, count, bbox, source) {
    state.geojson = geojson;
    state.fieldValues = null;
    const info = $("geoInfo");
    info.classList.add("ok");
    const b = bbox.map((v) => v.toFixed(4)).join(", ");
    info.innerHTML = "<strong>" + count + "</strong> polygon" + (count === 1 ? "" : "s") + " from " + esc(source) + "<br>bbox: " + b;
    drawGeometry(geojson);
    populateFieldFilter(geojson.features.map((f) => f.properties.field_id));
  }

  function clearGeometry() {
    state.geojson = null;
    state.fieldValues = null;
    if (polyLayer) { map.removeLayer(polyLayer); polyLayer = null; }
    clearOverlay();
    $("geoInfo").className = "geo-info";
    $("geoInfo").textContent = "No geometry loaded.";
    $("mapEmpty").style.display = "";
    $("fileInput").value = "";
    $("mapToolbar").classList.remove("show");
    $("legend").classList.remove("show");
  }

  async function validateAndSet(payload, source) {
    showBanner("");
    try {
      const init = typeof payload === "string"
        ? { method: "POST", headers: { "Content-Type": "application/json" }, body: payload }
        : { method: "POST", body: payload };
      const res = await api("/api/validate", init);
      setGeometry(res.geojson, res.count, res.bbox, source);
    } catch (err) {
      showBanner(err.message);
    }
  }

  $("fileInput").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    validateAndSet(fd, file.name);
  });
  const dz = $("dropzone");
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    validateAndSet(fd, file.name);
  });
  $("demoGeoBtn").addEventListener("click", async () => {
    try {
      const gj = await api("/api/demo-geometry");
      await validateAndSet(JSON.stringify(gj), "demo set");
    } catch (err) { showBanner(err.message); }
  });
  $("clearGeoBtn").addEventListener("click", clearGeometry);

  /* ------------------------------------------------------------ form wiring */
  function setupForm(cfg) {
    const sel = $("indexSelect");
    cfg.indices.forEach((i) => sel.add(new Option(`${i.label} - ${i.description}`, i.key)));
    sel.add(new Option("Custom band math", "CUSTOM"));
    sel.addEventListener("change", syncFormula);
    syncFormula();

    $("startDate").value = cfg.defaults.start;
    $("endDate").value = cfg.defaults.end;
    $("cloudRange").value = cfg.defaults.max_cloud;
    $("cloudOut").textContent = cfg.defaults.max_cloud + "%";
    $("collectionInput").value = cfg.defaults.collection;
    $("cloudRange").addEventListener("input", (e) => { $("cloudOut").textContent = e.target.value + "%"; });

    const demo = $("demoToggle");
    demo.addEventListener("change", syncMode);
    if (cfg.demo_mode_forced) {
      demo.checked = true;
      demo.disabled = true;
      $("demoHint").textContent = "DEMO_MODE is enabled on the server - all jobs run offline on synthetic data.";
      $("fallbackRow").style.display = "none";
    } else if (!cfg.live_available) {
      $("demoHint").textContent = "Live extras are not installed (see requirements-live.txt). Demo mode works offline.";
    }
    syncMode();
  }

  function syncFormula() {
    const key = $("indexSelect").value;
    const input = $("formulaInput");
    const def = state.config.indices.find((i) => i.key === key);
    if (key === "CUSTOM") {
      input.readOnly = false;
      if (!input.dataset.custom) input.value = "(B08 - B04) / (B08 + B04)";
      else input.value = input.dataset.custom;
      input.placeholder = "e.g. (B08 - B11) / (B08 + B11)";
      $("exprHint").textContent = "(editable)";
    } else {
      input.readOnly = true;
      input.value = def.expression;
      $("exprHint").textContent = "";
    }
  }
  $("formulaInput").addEventListener("input", (e) => { if (!e.target.readOnly) e.target.dataset.custom = e.target.value; });

  function syncMode() {
    const demo = $("demoToggle").checked;
    const badge = $("modeBadge");
    badge.className = "badge " + (demo ? "demo" : "live");
    $("modeText").textContent = demo ? "DEMO MODE" : "LIVE - Planetary Computer";
    $("fallbackRow").style.display = demo || (state.config && state.config.demo_mode_forced) ? "none" : "";
    $("collectionInput").disabled = false;
  }

  function collectParams() {
    return {
      index: $("indexSelect").value,
      custom_formula: $("indexSelect").value === "CUSTOM" ? $("formulaInput").value : "",
      start: $("startDate").value,
      end: $("endDate").value,
      max_cloud: Number($("cloudRange").value),
      collection: $("collectionInput").value.trim() || state.config.defaults.collection,
      outputs: Array.from(document.querySelectorAll('input[name="outputs"]:checked')).map((c) => c.value),
      demo: $("demoToggle").checked,
      fallback_to_demo: $("fallbackToggle").checked,
    };
  }

  /* ------------------------------------------------------------ jobs */
  $("jobForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    showBanner("");
    if (!state.geojson) { showBanner("Load a GeoJSON file or the demo polygons first."); return; }
    const params = collectParams();
    if (!params.outputs.length) { showBanner("Select at least one output format."); return; }

    resetResults();
    setStatus("queued");
    $("runBtn").disabled = true;
    $("runBtn").innerHTML = '<span class="spinner"></span> Running...';
    switchTab("log");
    try {
      const res = await api("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ geojson: state.geojson, params }),
      });
      state.jobId = res.job_id;
      state.indexKey = params.index;
      state.indexLabel = params.index === "CUSTOM" ? "Custom" : params.index;
      poll();
    } catch (err) {
      showBanner(err.message);
      finishRun("failed");
    }
  });

  function poll() {
    clearTimeout(state.polling);
    api(`/api/jobs/${state.jobId}?since=${state.logCursor}`)
      .then((job) => {
        appendLog(job.logs);
        state.logCursor = job.log_cursor;
        setStatus(job.status, job.progress);
        if (job.status === "done") { onDone(job); finishRun("done"); return; }
        if (job.status === "failed") { showBanner(job.error || "Job failed."); finishRun("failed"); return; }
        state.polling = setTimeout(poll, 600);
      })
      .catch((err) => { showBanner(err.message); finishRun("failed"); });
  }

  function finishRun(status) {
    setStatus(status, status === "done" ? 1 : 0);
    $("runBtn").disabled = false;
    $("runBtn").textContent = "Run job";
  }

  function setStatus(status, progress) {
    const chip = $("statusChip");
    chip.className = "chip " + status;
    chip.textContent = status;
    const pb = $("progress");
    const active = status === "running" || status === "queued";
    pb.classList.toggle("show", active);
    pb.firstElementChild.style.width = Math.round((progress || 0) * 100) + "%";
  }

  function resetResults() {
    state.logCursor = 0;
    state.rows = [];
    state.summary = null;
    state.fieldValues = null;
    state.sceneDate = "";
    $("log").innerHTML = "";
    $("tableWrap").innerHTML = '<div class="empty">Running...</div>';
    $("chart").innerHTML = '<div class="empty">Running...</div>';
    $("chartLegend").innerHTML = "";
    $("stats").innerHTML = "";
    $("rowCount").textContent = "0";
    $("syntheticNote").classList.remove("show");
    ["dlCsv", "dlSqlite", "dlPng"].forEach((id) => { $(id).disabled = true; });
    clearOverlay();
    $("mapToolbar").classList.remove("show");
    $("legend").classList.remove("show");
    restyle();
  }

  function appendLog(lines) {
    if (!lines || !lines.length) return;
    const log = $("log");
    lines.forEach((l) => {
      const span = document.createElement("div");
      if (/ERROR/.test(l)) span.className = "err";
      else if (/WARNING|Falling back/.test(l)) span.className = "warn";
      span.textContent = l;
      log.appendChild(span);
    });
    log.scrollTop = log.scrollHeight;
  }

  async function onDone(job) {
    state.summary = job.summary;
    state.synthetic = job.summary.synthetic;
    $("syntheticNote").classList.toggle("show", state.synthetic);
    const files = job.summary.files;
    $("dlCsv").disabled = !files.csv;
    $("dlSqlite").disabled = !files.sqlite;
    $("dlPng").disabled = !files.png;
    try {
      state.rows = await api(`/api/jobs/${state.jobId}/results`);
    } catch (err) { showBanner(err.message); return; }

    const st = job.summary.stats;
    $("stats").innerHTML =
      `<span>Rows <b>${st.rows}</b></span><span>Fields <b>${st.fields}</b></span><span>Scenes <b>${st.scenes}</b></span>` +
      `<span>Mean <b>${fmt(st.mean)}</b></span><span>Range <b>${fmt(st.min, 3)} to ${fmt(st.max, 3)}</b></span>`;
    $("rowCount").textContent = state.rows.length;

    const fields = [...new Set(state.rows.map((r) => r.field_id))];
    fields.forEach((f, i) => { state.colors[f] = SERIES_COLORS[i % SERIES_COLORS.length]; });
    renderTable();
    drawChart();
    setupScenePicker(job.summary);
    switchTab("results");
  }

  /* ------------------------------------------------------------ scene picker / overlay */
  function setupScenePicker(summary) {
    const sel = $("sceneSelect");
    sel.innerHTML = "";
    const dates = [...new Set(state.rows.map((r) => r.image_date))].sort();
    dates.forEach((d) => {
      const cc = (summary.scenes.find((s) => s.image_date === d) || {}).cloud_cover;
      sel.add(new Option(d + (cc !== null && cc !== undefined ? `  (cloud ${cc}%)` : ""), d));
    });
    $("opacityWrap").style.display = summary.overlays.length ? "" : "none";
    $("mapToolbar").classList.add("show");
    sel.onchange = () => applyScene(sel.value);
    applyScene(dates[dates.length - 1]);

    const [vmin, vmax] = currentRange();
    $("legendTitle").textContent = (state.indexLabel || "Index") + (state.synthetic ? " (synthetic)" : "");
    $("legMin").textContent = vmin;
    $("legMax").textContent = vmax;
    $("legend").classList.add("show");
  }

  function clearOverlay() {
    if (overlay) { map.removeLayer(overlay); overlay = null; }
  }

  function applyScene(date) {
    state.sceneDate = date;
    $("sceneSelect").value = date;
    const vals = {};
    state.rows.filter((r) => r.image_date === date).forEach((r) => {
      vals[r.field_id] = vals[r.field_id] === undefined ? r.mean_value : (vals[r.field_id] + r.mean_value) / 2;
    });
    state.fieldValues = vals;
    restyle();

    clearOverlay();
    const o = state.summary.overlays.find((x) => x.image_date === date);
    if (o) {
      overlay = L.imageOverlay(`/api/jobs/${state.jobId}/preview/${encodeURIComponent(o.file)}`, o.bounds, {
        opacity: Number($("opacityRange").value) / 100,
        interactive: false,
      }).addTo(map);
      if (polyLayer) polyLayer.bringToFront();
    }
  }
  $("opacityRange").addEventListener("input", (e) => { if (overlay) overlay.setOpacity(e.target.value / 100); });

  /* ------------------------------------------------------------ field filter / selection */
  function populateFieldFilter(ids) {
    const sel = $("fieldFilter");
    sel.innerHTML = "";
    sel.add(new Option("All fields", ""));
    ids.forEach((id) => sel.add(new Option(id, id)));
  }
  $("fieldFilter").addEventListener("change", (e) => selectField(e.target.value));

  function selectField(id) {
    state.selectedField = id;
    $("fieldFilter").value = id;
    restyle();
    if (state.rows.length) { renderTable(); drawChart(); }
  }

  /* ------------------------------------------------------------ results table */
  const COLS = [
    ["field_id", "Field", false],
    ["image_date", "Date", false],
    ["mean_value", "Mean", true],
    ["min_value", "Min", true],
    ["max_value", "Max", true],
    ["std_value", "Std", true],
    ["pixel_count", "Pixels", true],
    ["cloud_cover", "Cloud %", true],
    ["scene_id", "Scene", false],
  ];
  const ROW_CAP = 1500;

  function renderTable() {
    let rows = state.rows.filter((r) => !state.selectedField || r.field_id === state.selectedField);
    const { key, dir } = state.sort;
    rows = rows.slice().sort((a, b) => {
      const x = a[key], y = b[key];
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (x > y ? 1 : -1) * dir;
    });
    if (!rows.length) { $("tableWrap").innerHTML = '<div class="empty">No rows.</div>'; return; }

    const head = COLS.map(([k, label, num]) =>
      `<th data-key="${k}" class="${num ? "num" : ""}">${label}${k === key ? `<span class="arrow">${dir > 0 ? "&#9650;" : "&#9660;"}</span>` : ""}</th>`).join("");
    const body = rows.slice(0, ROW_CAP).map((r) => {
      const [vmin, vmax] = currentRange();
      const color = r.mean_value === null ? "transparent" : rampColor((r.mean_value - vmin) / (vmax - vmin));
      return `<tr>
        <td><span class="swatch" style="background:${color}"></span>${esc(r.field_id)}</td>
        <td>${esc(r.image_date)}</td>
        <td class="num"><b>${fmt(r.mean_value)}</b></td>
        <td class="num">${fmt(r.min_value)}</td>
        <td class="num">${fmt(r.max_value)}</td>
        <td class="num">${fmt(r.std_value)}</td>
        <td class="num">${r.pixel_count}</td>
        <td class="num">${r.cloud_cover === null ? "-" : r.cloud_cover}</td>
        <td class="mono">${esc(r.scene_id)}</td></tr>`;
    }).join("");
    const more = rows.length > ROW_CAP ? `<div class="hint" style="padding:8px 12px">Showing first ${ROW_CAP} of ${rows.length} rows - download the file for everything.</div>` : "";
    $("tableWrap").innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>${more}`;
    $("tableWrap").querySelectorAll("th").forEach((th) => th.addEventListener("click", () => {
      const k = th.dataset.key;
      state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : 1 };
      renderTable();
    }));
  }

  /* ------------------------------------------------------------ chart */
  function drawChart() {
    const host = $("chart");
    if (!state.rows.length) return;
    const valid = state.rows.filter((r) => r.mean_value !== null);
    if (!valid.length) { host.innerHTML = '<div class="empty">No valid values to plot.</div>'; return; }

    const ids = [...new Set(valid.map((r) => r.field_id))];
    const times = valid.map((r) => Date.parse(r.image_date));
    const tMin = Math.min(...times), tMax = Math.max(...times);
    let yMin = Math.min(...valid.map((r) => r.mean_value)), yMax = Math.max(...valid.map((r) => r.mean_value));
    const pad = (yMax - yMin) * 0.12 || 0.05;
    yMin -= pad; yMax += pad;

    const W = Math.max(host.clientWidth - 24, 320) || 900;
    const H = Math.max(host.clientHeight - 12, 160) || 260;
    const m = { l: 46, r: 14, t: 10, b: 28 };
    const x = (t) => m.l + (tMax === tMin ? (W - m.l - m.r) / 2 : ((t - tMin) / (tMax - tMin)) * (W - m.l - m.r));
    const y = (v) => H - m.b - ((v - yMin) / (yMax - yMin)) * (H - m.t - m.b);

    let svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Mean index over time">`;
    for (let i = 0; i <= 4; i++) {
      const v = yMin + ((yMax - yMin) * i) / 4;
      svg += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${v.toFixed(2)}</text>`;
    }
    const dates = [...new Set(valid.map((r) => r.image_date))].sort();
    const step = Math.max(1, Math.ceil(dates.length / 8));
    dates.forEach((d, i) => {
      if (i % step) return;
      svg += `<text x="${x(Date.parse(d))}" y="${H - 8}" text-anchor="middle">${d.slice(5)}</text>`;
    });
    ids.forEach((id) => {
      const pts = valid.filter((r) => r.field_id === id).sort((a, b) => (a.image_date > b.image_date ? 1 : -1));
      const cls = state.selectedField ? (id === state.selectedField ? "series hl" : "series dim") : "series";
      const color = state.colors[id] || "#2de2d0";
      svg += `<polyline class="${cls}" stroke="${color}" points="${pts.map((p) => `${x(Date.parse(p.image_date)).toFixed(1)},${y(p.mean_value).toFixed(1)}`).join(" ")}"><title>${esc(id)}</title></polyline>`;
      pts.forEach((p) => {
        svg += `<circle cx="${x(Date.parse(p.image_date)).toFixed(1)}" cy="${y(p.mean_value).toFixed(1)}" r="2.6" fill="${color}" opacity="${cls.includes("dim") ? 0.2 : 1}"><title>${esc(id)} - ${p.image_date}: ${p.mean_value.toFixed(4)}</title></circle>`;
      });
    });
    svg += "</svg>";
    host.innerHTML = svg;

    $("chartLegend").innerHTML = ids.map((id) =>
      `<span data-id="${esc(id)}" class="${id === state.selectedField ? "on" : ""}"><i class="swatch" style="background:${state.colors[id] || "#2de2d0"}"></i>${esc(id)}</span>`).join("");
    $("chartLegend").querySelectorAll("span").forEach((s) => s.addEventListener("click", () =>
      selectField(s.dataset.id === state.selectedField ? "" : s.dataset.id)));
  }

  /* ------------------------------------------------------------ tabs / downloads */
  function switchTab(name) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    document.querySelectorAll(".tab-pane").forEach((p) => p.classList.toggle("active", p.id === "pane-" + name));
    if (name === "chart" && state.rows.length) drawChart();
  }
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));

  const download = (fmt) => { if (state.jobId) window.location.href = `/api/jobs/${state.jobId}/download/${fmt}`; };
  $("dlCsv").addEventListener("click", () => download("csv"));
  $("dlSqlite").addEventListener("click", () => download("sqlite"));
  $("dlPng").addEventListener("click", () => download("png"));

  window.addEventListener("resize", () => {
    map.invalidateSize();
    if (state.rows.length && $("pane-chart").classList.contains("active")) drawChart();
  });

  /* ------------------------------------------------------------ boot */
  (async function init() {
    try {
      state.config = await api("/api/config");
      setupForm(state.config);
    } catch (err) {
      showBanner("Could not load configuration: " + err.message);
    }
  })();
})();
