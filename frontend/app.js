// FireCal frontend v2: карта, календарь, аномалии, сезон, топ, помощник, ступени
// (без шаблонных строк и без знака доллара: безопасно для копирования)
var API = location.protocol === "file:" ? "http://127.0.0.1:8000" : "";
function el(s) { return document.querySelector(s); }
function els(s) { return Array.prototype.slice.call(document.querySelectorAll(s)); }

// Visitors through a public link get a lighter map: the busiest cells only, so it loads over slow connections.
var LOCAL_VIEW = /^(localhost|127\.\d+\.\d+\.\d+|\[::1\])$/.test(location.hostname) || location.protocol === "file:";
var MAP_LIMIT = LOCAL_VIEW ? 50000 : 3000;
var FRP_STOPS = [10, 40, 120, 350];
var FRP_LABELS = ["<10", "10–<40", "40–<120", "120–<350", "≥350"];
var archiveBounds, coverageLayer;
var motionPreference = matchMedia("(prefers-reduced-motion: reduce)");
var globe = new AtlasGlobe(el("#globe"));
var state = {
  bbox: null, start: null, end: null, minConf: 1, sensor: "", land: "", normalize: false,
  year: null, level: "day", z: 2, minDate: null, maxDate: null,
  day: null, cumulative: false, playing: false,
  cal: {}, anom: [], hit: {}, season: null, zonesShown: false, kpi: null, palette: "fire"
};
var startupFailed = false;
var stagesRequest = 0;
var chatVersion = 0;

var SENSOR_VARS = { MODIS: "--s-modis", VIIRS: "--s-viirs", both: "--s-both" };
function sensorColor(k) { return cssVar(SENSOR_VARS[k] || "--s-modis") || "#999999"; }
function frpPalette() {
  return FRP_LABELS.map(function (_, i) {
    return cssVar("--frp" + (i + 1));
  });
}
function frpColor(frp, palette) {
  var index = FRP_STOPS.findIndex(function (stop) { return (frp || 0) < stop; });
  return palette[index < 0 ? FRP_STOPS.length : index];
}
function meanFrp(row) {
  return row.days > 0 ? row.frp / row.days : 0;
}
function renderFrpLegend() {
  var legend = el("#frpLegend"), palette = frpPalette();
  legend.replaceChildren(t("frp_unit"));
  FRP_LABELS.forEach(function (label, i) {
    var item = document.createElement("span"), swatch = document.createElement("i");
    swatch.style.setProperty("--swatch", palette[i]);
    item.append(swatch, label);
    legend.append(item);
  });
}
function confName(c) { return I18N[LANG]["conf" + c] || c; }

// ---------- утилиты ----------
function iso(v) { return String(v).slice(0, 10); }
function parse(s) { return new Date(iso(s) + "T00:00:00Z"); }
function validDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  var date = parse(value);
  return Number.isFinite(+date) && date.toISOString().slice(0, 10) === value;
}
function addDays(s, n) {
  var d = parse(s);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function daysBetween(a, b) { return Math.round((parse(b) - parse(a)) / 864e5); }
function fmt(n) { return Number(n).toLocaleString(LANG === "en" ? "en-US" : "ru-RU"); }
function round1(n) { return Math.round(Number(n) * 10) / 10; }

function qs(extra) {
  var p = new URLSearchParams();
  if (state.bbox) p.set("bbox", state.bbox);
  if (state.minConf > 1) p.set("min_conf", state.minConf);
  if (state.normalize) p.set("normalize", "1");
  if (state.land) p.set("land", state.land);
  extra = extra || {};
  Object.keys(extra).forEach(function (k) {
    var v = extra[k];
    if (v !== null && v !== undefined && v !== "") p.set(k, v);
  });
  return p.toString();
}
function api(path, params) {
  return fetch(API + path + "?" + (params || "")).then(function (r) {
    if (!r.ok) throw new Error(path + ": " + r.status);
    return r.json();
  });
}
function status(msg) { el("#status").textContent = msg || ""; }
function animateUpdate(node, keyframes, duration) {
  if (motionPreference.matches || !node.animate) return;
  node.getAnimations().forEach(function (animation) { animation.cancel(); });
  node.animate(keyframes, { duration: duration, easing: "cubic-bezier(.2,.7,.3,1)" });
}
function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
function showError(e) { console.error(e); status(t("err") + e.message); }

// ---------- карта ----------
var map = L.map("map", {
  preferCanvas: true,
  zoomAnimation: !motionPreference.matches, fadeAnimation: !motionPreference.matches
}).setView([48, 69.5], 5);
var archivePane = map.createPane("archivePoints");
archivePane.style.zIndex = 405;
var canvas = L.canvas({ padding: 0.3, pane: "archivePoints" });
var tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18,
  crossOrigin: "anonymous",
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors · Fire data: NASA FIRMS'
}).addTo(map);
L.control.scale({ imperial: false, maxWidth: 105 }).addTo(map);
var areaStyle = { color: "#8dc9ed", weight: 2, fillOpacity: .10 };
var coverageStyle = { color: "#79b6d5", weight: 1.5, fill: false, dashArray: "6 5", interactive: false };
var gridLayer = L.layerGroup().addTo(map);
map.on("moveend resize", drawGrid);
new ResizeObserver(function () {
  if (!el("#atlas").hidden) map.invalidateSize({ debounceMoveend: true });
}).observe(map.getContainer());
function drawGrid() {
  gridLayer.clearLayers();
  var b = map.getBounds(), zoom = map.getZoom();
  var step = zoom < 5 ? 10 : zoom < 7 ? 5 : zoom < 9 ? 2 : zoom < 11 ? 1 : .25;
  var model = { color: cssVar("--grid"), opacity: .23, weight: 1, interactive: false };
  for (var x = Math.ceil(b.getWest() / step) * step; x < b.getEast(); x += step) {
    L.polyline([[b.getSouth(), x], [b.getNorth(), x]], model).addTo(gridLayer);
  }
  for (var y = Math.ceil(b.getSouth() / step) * step; y < b.getNorth(); y += step) {
    L.polyline([[y, b.getWest()], [y, b.getEast()]], model).addTo(gridLayer);
  }
  var c = map.getCenter();
  el("#mapCoordinates").textContent = c.lat.toFixed(2) + "°, " + L.Util.wrapNum(c.lng, [-180, 180]).toFixed(2) + "°";
}
tiles.on("tileerror", function (event) {
  var tile = event.tile;
  if (!tile.crossOrigin) return;
  tile.removeAttribute("crossorigin");
  tile.src = tiles.getTileUrl(event.coords);
});
function setTiles() {
  map.getContainer().classList.toggle("dark-map", document.documentElement.dataset.theme === "dark");
}
var fireLayer = L.layerGroup().addTo(map);
function hotspotRadius(frp) {
  var scale = Math.min(1, Math.pow(1.45, map.getZoom() - 9));
  return Math.max(0.95, Math.min(11, 2.5 + Math.sqrt(frp) * 0.42) * scale);
}
map.on("zoomend", function () {
  fireLayer.eachLayer(function (marker) {
    marker.setRadius(hotspotRadius(marker.options.frp));
  });
});
var zoneLayer = L.layerGroup();
var drawn = new L.FeatureGroup().addTo(map);
var drawControl = new L.Control.Draw({
  draw: {
    polygon: false, polyline: false, circle: false, marker: false, circlemarker: false,
    rectangle: { showArea: false, shapeOptions: areaStyle }
  },
  edit: { featureGroup: drawn }
});
function renderDrawControl() {
  var draw = L.drawLocal.draw.toolbar, edit = L.drawLocal.edit.toolbar;
  draw.buttons.rectangle = t("draw_rectangle");
  draw.actions.title = draw.actions.text = t("draw_cancel");
  L.drawLocal.draw.handlers.rectangle.tooltip.start = t("draw_drag");
  L.drawLocal.draw.handlers.simpleshape.tooltip.end = t("draw_release");
  edit.buttons.edit = edit.buttons.editDisabled = t("draw_edit");
  edit.buttons.remove = edit.buttons.removeDisabled = t("draw_delete");
  edit.actions.save.title = edit.actions.save.text = t("draw_save");
  edit.actions.cancel.title = edit.actions.cancel.text = t("draw_cancel");
  edit.actions.clearAll.title = edit.actions.clearAll.text = t("draw_clear");
  L.drawLocal.edit.handlers.edit.tooltip.text = t("draw_handles");
  L.drawLocal.edit.handlers.edit.tooltip.subtext = t("draw_undo");
  L.drawLocal.edit.handlers.remove.tooltip.text = t("draw_remove_shape");
  map.removeControl(drawControl);
  map.addControl(drawControl);
}
function setBboxFromLayer(layer) {
  var b = layer.getBounds();
  state.bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]
    .map(function (v) { return v.toFixed(4); }).join(",");
  loadAll();
}
map.on(L.Draw.Event.CREATED, function (e) {
  drawn.clearLayers();
  drawn.addLayer(e.layer);
  setBboxFromLayer(e.layer);
});
map.on(L.Draw.Event.EDITED, function () {
  var l = drawn.getLayers()[0];
  if (l) setBboxFromLayer(l);
});
map.on(L.Draw.Event.DELETED, function () {
  if (!drawn.getLayers().length) { state.bbox = null; loadAll(); }
});
el("#clearArea").onclick = function () {
  drawn.clearLayers();
  state.bbox = null;
  loadAll();
};

// ---------- карта: пожары ----------
var firesRequest = 0;
var visibleFires = [];
function loadFires() {
  if (comparison.enabled) return loadComparison();
  var request = ++firesRequest;
  var start = state.start, end = state.end;
  if (state.day) {
    end = state.day;
    start = state.cumulative ? state.start : state.day;
  }
  return api("/api/fires", qs({ start: start, end: end, sensor: state.sensor, limit: MAP_LIMIT }))
    .then(function (rows) {
      if (request !== firesRequest) return;
      visibleFires = rows;
      fireLayer.clearLayers();
      var count = { MODIS: 0, VIIRS: 0, both: 0 };
      var frp = 0;
      var palette = frpPalette();
      rows.forEach(function (r) {
        var power = meanFrp(r);
        var color = frpColor(power, palette);
        count[r.seen_by] = (count[r.seen_by] || 0) + 1;
        frp += r.frp || 0;
        var popup =
          "<b>" + r.cell + "</b><br>" + t("p_days") + r.days +
          "<br>" + t("frp_unit") + ": " + fmt(round1(power)) +
          "<br>" + t("st_frp") + ": " + fmt(r.frp) + "<br>" + t("p_seen") + (r.seen_by === "both" ? t("s_both") : r.seen_by) +
          "<br>" + t("p_conf") + confName(r.conf) + "<br>" + t("land_label") + ": " + t("land_" + (r.land_type || "unknown"));
        L.circleMarker([r.lat, r.lon], {
          renderer: canvas,
          frp: power, radius: hotspotRadius(power),
          stroke: false, fillColor: color, fillOpacity: 0.72
        }).bindPopup(popup).addTo(fireLayer);
      });
      renderReliability(count, rows.length, frp);
    });
}
var lastRel = null;
function renderReliability(c, total, frp) {
  lastRel = [c, total, frp];
  function pct(n) { return total ? (n / total) * 100 : 0; }
  el("#rel").innerHTML = ["MODIS", "VIIRS", "both"].map(function (k) {
    return '<div style="width:' + pct(c[k] || 0) + "%;background:" + sensorColor(k) + '"></div>';
  }).join("");
  el("#relInfo").textContent = total
    ? t("confirmed") + pct(c.both || 0).toFixed(0) + "%"
    : t("nodata");
  el("#mapLimit").textContent = total === MAP_LIMIT ? t("map_limit", { n: fmt(total) }) : "";
  el("#stats").innerHTML =
    "<div><b>" + fmt(total) + "</b><span>" + t("st_cells") + "</span></div>" +
    "<div><b>" + fmt(c.both || 0) + "</b><span>" + t("st_both") + "</span></div>" +
    "<div><b>" + fmt(Math.round(frp)) + "</b><span>" + t("st_frp") + "</span></div>";
}

// ---------- календарь ----------
function loadCalendar() {
  var request = ++calendarRequest;
  return api("/api/calendar", qs({ year: state.year })).then(function (rows) {
    if (request !== calendarRequest) return;
    state.cal = {};
    rows.forEach(function (r) { state.cal[iso(r.date)] = r; });
    drawCalendar();
    animateUpdate(cal, [{ opacity: 0.35 }, { opacity: 1 }], 300);
  });
}
var calendarRequest = 0;
var calendarWidth = 500, calendarStep = 12, calendarLeft = 32, calendarTop = 21;
function drawCalendar(exportContext, exportStep, exportX, exportY) {
  var cvs = el("#cal"), y = state.year;
  if (!y) return;
  var jan1 = new Date(Date.UTC(y, 0, 1));
  var offset = (jan1.getUTCDay() + 6) % 7; // понедельник = 0
  var days = (Date.UTC(y + 1, 0, 1) - Date.UTC(y, 0, 1)) / 864e5;
  var weeks = Math.ceil((offset + days) / 7);
  var ctx = exportContext, step = exportStep;
  if (ctx) {
    ctx.save();
    ctx.translate(exportX, exportY);
  } else {
    calendarWidth = cvs.parentElement.clientWidth || calendarWidth;
    calendarStep = Math.min(19, Math.max(10, (calendarWidth - 12 - calendarLeft) / weeks));
    step = calendarStep;
    var width = calendarLeft + weeks * step, height = calendarTop + 7 * step;
    var dpr = Math.max(2, window.devicePixelRatio || 1);
    cvs.width = width * dpr;
    cvs.height = height * dpr;
    cvs.style.width = width + "px";
    cvs.style.height = height + "px";
    ctx = cvs.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  var cellSize = step - 2;

  var colors = [0, 1, 2, 3, 4].map(function (i) { return cssVar("--c" + i); });
  var text = cssVar("--muted"), accent = cssVar("--accent"), fg = cssVar("--text");
  var max = 1;
  Object.keys(state.cal).forEach(function (k) { max = Math.max(max, state.cal[k].cells); });
  var anomDays = {};
  if (state.level === "day") state.anom.forEach(function (a) { anomDays[iso(a.date)] = true; });

  ctx.fillStyle = text;
  ctx.font = "10px Inter, system-ui";
  ctx.textBaseline = "middle";
  t("dow").forEach(function (dn, i) {
    ctx.fillText(dn, 2, calendarTop + i * 2 * step + cellSize / 2);
  });
  var months = t("mon");

  if (!exportContext) state.hit = {};
  for (var i = 0; i < days; i++) {
    var date = addDays(y + "-01-01", i), idx = i + offset;
    var col = Math.floor(idx / 7), row = idx % 7;
    var x = calendarLeft + col * step, yy = calendarTop + row * step;
    var v = state.cal[date] ? state.cal[date].cells : 0, ratio = v / max;
    var level = v === 0 ? 0 : ratio < 0.15 ? 1 : ratio < 0.4 ? 2 : ratio < 0.7 ? 3 : 4;
    if (date >= state.minDate && date <= state.maxDate) {
      ctx.fillStyle = colors[level];
      ctx.beginPath();
      ctx.roundRect(x, yy, cellSize, cellSize, 2);
      ctx.fill();
      if (anomDays[date]) { ctx.strokeStyle = accent; ctx.lineWidth = 2; ctx.stroke(); }
      if (state.day === date) { ctx.strokeStyle = fg; ctx.lineWidth = 2; ctx.stroke(); }
    }
    if (!exportContext) state.hit[col + "," + row] = date;
    if (date.slice(8) === "01") {
      ctx.fillStyle = text;
      ctx.fillText(months[+date.slice(5, 7) - 1], x, 8);
    }
  }
  if (exportContext) ctx.restore();
}
var cal = el("#cal"), tip = el("#tip");
function calDate(e) {
  var r = cal.getBoundingClientRect();
  var col = Math.floor((e.clientX - r.left - calendarLeft) / calendarStep);
  var row = Math.floor((e.clientY - r.top - calendarTop) / calendarStep);
  return state.hit[col + "," + row];
}
cal.addEventListener("mousemove", function (e) {
  var d = calDate(e);
  if (!d) { tip.style.display = "none"; return; }
  var r = state.cal[d];
  var label = d < state.minDate || d > state.maxDate ? "tip_outside" : "tip_none";
  tip.textContent = r
    ? t("tip_row", { d: d, c: round1(r.cells), f: r.frp, b: r.confirmed_both })
    : t(label, { d: d });
  tip.style.display = "block";
  tip.style.left = Math.min(e.clientX + 12, window.innerWidth - tip.offsetWidth - 8) + "px";
  tip.style.top = Math.min(e.clientY + 12, window.innerHeight - tip.offsetHeight - 8) + "px";
});
cal.addEventListener("mouseleave", function () { tip.style.display = "none"; });
cal.addEventListener("click", function (e) {
  var d = calDate(e);
  if (d) { stop(); setDay(d); }
});
new ResizeObserver(function () {
  var width = cal.parentElement.clientWidth;
  if (!width || width === calendarWidth) return;
  drawCalendar();
  drawSeason();
  drawKpiChart();
}).observe(cal.parentElement);

// ---------- аномалии ----------
function loadAnomalies() {
  return api("/api/anomalies", qs({ level: state.level, z: state.z, start: state.start, end: state.end }))
    .then(function (res) {
      state.anom = res.items || [];
      var info;
      if (res.mean === null) info = t("nodata");
      else if (res.years < 4) info = t("anom_need");
      else info = t("anom_found", { n: state.anom.length });
      el("#anomInfo").textContent = info;
      var ul = el("#anomList");
      ul.innerHTML = "";
      state.anom.slice(0, 30).forEach(function (a) {
        var li = document.createElement("li");
        var label = state.level === "month" ? iso(a.date).slice(0, 7) : iso(a.date);
        var ratio = a.expected > 0 ? a.n / a.expected : null;
        li.title = "z = " + a.z;
        li.innerHTML = "<span>" + label + "</span><span>" + fmt(round1(a.n)) + " · " + t("anom_norm") + " " +
          fmt(round1(a.expected)) + " <b class=\"ratio\">" +
          (ratio === null ? "↑" : "×" + fmt(ratio >= 10 ? Math.round(ratio) : round1(ratio))) + "</b></span>";
        li.onclick = function () {
          if (state.level === "month") setMonth(iso(a.date));
          else setDay(iso(a.date));
        };
        makeKeyboardAction(li);
        ul.appendChild(li);
      });
      drawCalendar();
    });
}
function setMonth(first) {
  stop();
  var d = parse(first);
  var last = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)).toISOString().slice(0, 10);
  first = first < state.minDate ? state.minDate : first;
  last = last > state.maxDate ? state.maxDate : last;
  state.start = first;
  state.end = last;
  state.day = null;
  el("#start").value = first;
  el("#end").value = last;
  syncSlider();
  loadAll();
}

// ---------- сезон по месяцам ----------
function loadSeason() {
  return api("/api/seasons", qs({})).then(function (res) {
    state.season = res;
    drawSeason();
  });
}
function drawSeason() {
  var cvs = el("#season"), d = state.season;
  var w = cvs.clientWidth || 360, h = 130, dpr = window.devicePixelRatio || 1;
  cvs.width = w * dpr;
  cvs.height = h * dpr;
  cvs.style.height = h + "px";
  var ctx = cvs.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!d) return;
  var yr = d.years[String(state.year)] || [];
  var max = 1;
  d.mean.concat(yr).forEach(function (v) { if (v !== null && v > max) max = v; });
  var padT = 8, padB = 4, bw = w / 12, usable = h - padT - padB;
  ctx.fillStyle = cssVar("--c2");
  d.mean.forEach(function (v, i) {
    if (v === null) return;
    var bh = (v / max) * usable;
    ctx.fillRect(i * bw + bw * 0.15, h - padB - bh, bw * 0.7, bh);
  });
  ctx.strokeStyle = cssVar("--text");
  ctx.lineWidth = 2;
  ctx.beginPath();
  var started = false;
  yr.forEach(function (v, i) {
    if (v === null) { started = false; return; }
    var x = i * bw + bw / 2, y = h - padB - (v / max) * usable;
    if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
  });
  ctx.stroke();
}

// ---------- ключевые показатели ----------
function yearsWord(n) {
  if (LANG === "en") return n === 1 ? "year" : "years";
  var m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return "год";
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return "года";
  return "лет";
}
function loadKpis() {
  return api("/api/kpis", qs({ start: state.start, end: state.end })).then(function (res) {
    state.kpi = res;
    renderKpis();
    animateUpdate(el("#kpiChart"), [{ opacity: 0.3, translate: "0 5px" }, { opacity: 1, translate: "0 0" }], 380);
  });
}
function renderKpis() {
  var k = state.kpi, box = el("#kpi");
  el("#seriesLabel").textContent = seriesLabel();
  countTo(el("#summaryValue"), k && !k.empty ? Math.round(k.current.cells) : null);
  el("#summaryPeriod").textContent = k && !k.empty ? k.window.start + " – " + k.window.end : t("k_none");
  var delta = el("#summaryCompare");
  delta.hidden = !(k && !k.empty);
  if (k && !k.empty) {
    var p = k.mode === "window" ? k.pct_vs_median : null;
    delta.className = "delta" + (p === null ? " neutral" : p > 10 ? " up" : p < -10 ? " down" : " flat");
    delta.textContent = p === null
      ? t(k.mode === "long" ? "delta_long" : "delta_none")
      : t("delta_pct", { p: (p > 0 ? "+" : "") + p, n: k.n_other, w: yearsWord(k.n_other) });
  }
  var bbox = state.bbox && state.bbox.split(",").map(Number);
  el("#summaryArea").textContent = bbox ?
    bbox[1].toFixed(2) + "°, " + bbox[0].toFixed(2) + "° → " + bbox[3].toFixed(2) + "°, " + bbox[2].toFixed(2) + "°" : t("clear");
  if (!k || k.empty) {
    box.innerHTML = '<p class="muted">' + t("k_none") + "</p>";
    el("#kpiWin").textContent = "";
    drawKpiChart();
    return;
  }
  el("#kpiWin").textContent = k.window.start + " – " + k.window.end;
  var html = '<div class="stats">' +
    "<div><b>" + fmt(Math.round(k.current.cells)) + "</b><span>" + t("k_events") + "</span></div>" +
    "<div><b>" + fmt(k.current.points) + "</b><span>" + t("k_points") + "</span></div>" +
    "<div><b>" + k.current.active_days + "</b><span>" + t("k_days") + "</span></div></div>";
  var cmp;
  if (k.mode === "long") {
    cmp = t("k_long");
  } else if (k.median == null) {
    cmp = t("k_need");
  } else {
    cmp = "";
    if (k.pct_vs_median !== null) {
      cmp += t("k_pct", { p: "<b>" + (k.pct_vs_median > 0 ? "+" : "") + k.pct_vs_median + "</b>" });
    }
    cmp += t("k_over", { n: k.n_other, w: yearsWord(k.n_other), m: fmt(round1(k.median)) });
    if (k.rank) cmp += t("k_rank", { r: k.rank, n: k.n_years });
  }
  box.innerHTML = html + '<p class="kpicmp">' + cmp + "</p>";
  drawKpiChart();
}
function drawKpiChart() {
  var cvs = el("#kpiChart"), k = state.kpi;
  var w = cvs.clientWidth || 360, h = 120, dpr = window.devicePixelRatio || 1;
  cvs.width = w * dpr;
  cvs.height = h * dpr;
  cvs.style.height = h + "px";
  var ctx = cvs.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!k || k.empty || !k.history || !k.history.length) return;
  var hist = k.history, n = hist.length, max = 1;
  hist.forEach(function (it) { if (it.cells > max) max = it.cells; });
  var padT = 10, padB = 16, bw = w / n, usable = h - padT - padB;
  ctx.font = "10px system-ui";
  ctx.textAlign = "center";
  hist.forEach(function (it, i) {
    var bh = (it.cells / max) * usable, x = i * bw + bw * 0.12, wd = bw * 0.76, y = h - padB - bh;
    var cur = it.year === k.current_year;
    ctx.fillStyle = cur ? cssVar("--accent") : cssVar("--c2");
    ctx.fillRect(x, y, wd, bh);
    if (cur) {
      ctx.strokeStyle = cssVar("--text");
      ctx.lineWidth = 2;
      ctx.strokeRect(x, y, wd, bh);
    }
    if (n <= 12 || i % 3 === 0 || cur) {
      ctx.fillStyle = cssVar("--muted");
      ctx.fillText(String(it.year).slice(2), x + wd / 2, h - 3);
    }
  });
  if (k.mode === "window" && k.mean !== null) {
    var ym = h - padB - (k.mean / max) * usable;
    ctx.strokeStyle = cssVar("--text");
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(0, ym);
    ctx.lineTo(w, ym);
    ctx.stroke();
    ctx.setLineDash([]);
  }
}

// ---------- область по координатам ----------
function setAreaAround(lat, lon, rkm) {
  var dLat = rkm / 111.32;
  var dLon = rkm / (111.32 * Math.cos(lat * Math.PI / 180));
  var b = L.latLngBounds([[lat - dLat, lon - dLon], [lat + dLat, lon + dLon]]);
  drawn.clearLayers();
  var rect = L.rectangle(b, areaStyle);
  drawn.addLayer(rect);
  map.fitBounds(b);
  setBboxFromLayer(rect);
}
el("#goPoint").onclick = function () {
  var lat = parseFloat(el("#lat").value), lon = parseFloat(el("#lon").value), r = parseFloat(el("#rad").value);
  if (isNaN(lat) || isNaN(lon) || isNaN(r) || lat < -85 || lat > 85 || lon < -180 || lon > 180 || r < 5 || r > 500) {
    status(t("geo_bad"));
    return;
  }
  setAreaAround(lat, lon, r);
};
el("#myLoc").onclick = function () {
  if (!navigator.geolocation) { status(t("geo_no")); return; }
  navigator.geolocation.getCurrentPosition(function (pos) {
    el("#lat").value = pos.coords.latitude.toFixed(4);
    el("#lon").value = pos.coords.longitude.toFixed(4);
    el("#goPoint").onclick();
  }, function () { status(t("geo_fail")); });
};

// ---------- палитра ----------
el("#palette").onchange = function (e) {
  state.palette = e.target.value;
  document.documentElement.dataset.palette = state.palette;
  try { localStorage.setItem("palette", state.palette); } catch (err) {}
  redrawAll();
};
function redrawAll() {
  renderFrpLegend();
  setTiles();
  drawGrid();
  drawCalendar();
  drawSeason();
  drawKpiChart();
  if (state.start) loadFires().catch(showError);
}

// ---------- топ-10 ----------
function loadTop() {
  return api("/api/top", qs({ start: state.start, end: state.end, n: 10 })).then(function (rows) {
    var ul = el("#topList");
    ul.innerHTML = "";
    rows.forEach(function (r, i) {
      var li = document.createElement("li");
      li.innerHTML = "<span>" + (i + 1) + ". " + Number(r.lat).toFixed(2) + ", " + Number(r.lon).toFixed(2) +
        "</span><span>" + t("top_row", { d: r.days, f: fmt(Math.round(r.frp)) }) + "</span>";
      li.onclick = function () { map.setView([r.lat, r.lon], 10); };
      makeKeyboardAction(li);
      ul.appendChild(li);
    });
  });
}

// ---------- таймлапс ----------
var slider = el("#slider");
function syncSlider() {
  slider.max = state.start && state.end ? Math.max(0, daysBetween(state.start, state.end)) : 0;
  slider.value = state.day
    ? Math.min(slider.max, Math.max(0, daysBetween(state.start, state.day)))
    : slider.max;
  el("#dayLabel").textContent = state.day || t("all_period");
  slider.setAttribute("aria-valuetext", state.day || t("all_period"));
}
function setDay(d) {
  if (!state.start || d < state.minDate || d > state.maxDate) return;
  if (comparison.enabled) closeComparison(false);
  syncCalendarYear(d);
  if (d < state.start || d > state.end) {
    stop();
    state.start = d < state.start ? d : state.start;
    state.end = d > state.end ? d : state.end;
    el("#start").value = state.start;
    el("#end").value = state.end;
    state.day = d;
    syncSlider();
    loadAll();
    return;
  }
  state.day = d;
  syncSlider();
  loadFires().catch(showError);
  drawCalendar();
}
slider.addEventListener("input", function () {
  if (!state.start) return;
  stop();
  setDay(addDays(state.start, +slider.value));
});
el("#allPeriod").onclick = function () {
  stop();
  closeComparison(false);
  state.day = null;
  syncSlider();
  loadFires().catch(showError);
  drawCalendar();
};
el("#cumulative").onchange = function (e) {
  state.cumulative = e.target.checked;
  if (state.day) loadFires().catch(showError);
};
function syncPlayButton() {
  var button = el("#play"), label = state.playing ? "pause_t" : "play_t";
  button.textContent = state.playing ? "⏸" : "▶";
  button.title = t(label);
  button.setAttribute("aria-label", t(label));
}
var playbackVersion = 0;
function stop() { state.playing = false; playbackVersion++; syncPlayButton(); }
function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
el("#play").onclick = async function () {
  if (state.playing) return stop();
  if (!state.start) return;
  if (comparison.enabled) closeComparison(false);
  var version = ++playbackVersion;
  state.playing = true;
  syncPlayButton();
  var i = state.day ? daysBetween(state.start, state.day) : 0;
  if (i >= +slider.max) i = 0;
  while (version === playbackVersion && i <= +slider.max) {
    state.day = addDays(state.start, i);
    syncCalendarYear(state.day);
    syncSlider();
    drawCalendar();
    try { await loadFires(); } catch (e) { showError(e); break; }
    await sleep(200);
    i++;
  }
  if (version === playbackVersion) stop();
};

// ---------- помощник ----------
function addMsg(text, who) {
  var d = document.createElement("div");
  d.className = "msg " + who;
  d.textContent = text;
  var log = el("#chatLog");
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}
function askBot(q) {
  q = (q || "").trim();
  if (!q) return;
  var version = chatVersion;
  addMsg(q, "me");
  api("/api/ask", qs({ q: q, start: state.start, end: state.end, lang: LANG }))
    .then(function (r) { if (version === chatVersion) addMsg(r.answer, "bot"); })
    .catch(function (e) { if (version === chatVersion) addMsg(t("ans_err") + e.message, "bot"); });
}
el("#chatForm").onsubmit = function (e) {
  e.preventDefault();
  var inp = el("#chatInput");
  var q = inp.value;
  inp.value = "";
  askBot(q);
};
els(".chip").forEach(function (c) {
  c.onclick = function () { askBot(c.getAttribute("data-q")); };
});

// ---------- ступени ----------
function loadStages() {
  var request = ++stagesRequest;
  el("#zoneNote").textContent = t("computing");
  return api("/api/stages", new URLSearchParams({
    start: state.start, end: state.end, min_conf: state.minConf, lang: LANG,
    ...(state.land ? { land: state.land } : {})
  }))
    .then(function (res) { if (request === stagesRequest) renderStages(res); })
    .catch(function (e) {
      if (request === stagesRequest) el("#zoneNote").textContent = t("err") + e.message;
    });
}
function renderStages(res) {
  zoneLayer.clearLayers();
  var box = el("#zoneList");
  box.innerHTML = "";
  res.zones.forEach(function (z) {
    var circle = L.circle([z.lat, z.lon], {
      radius: z.radius_km * 1000, color: "#8dc9ed", weight: 2, dashArray: z.approx ? "6 4" : null, fillOpacity: 0.08
    });
    var pop = document.createElement("div");
    var b = document.createElement("b");
    b.textContent = z.name;
    pop.appendChild(b);
    [z.note, t("z_src") + z.source, z.approx ? t("z_approx") : ""].forEach(function (t) {
      if (!t) return;
      var p = document.createElement("div");
      p.textContent = t;
      pop.appendChild(p);
    });
    circle.bindPopup(pop);
    zoneLayer.addLayer(circle);

    var item = document.createElement("div");
    item.className = "zone";
    var head = document.createElement("div");
    head.className = "zonehead";
    var title = document.createElement("b");
    title.textContent = z.id;
    head.appendChild(title);
    if (z.approx) {
      var badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = t("z_badge");
      head.appendChild(badge);
    }
    head.onclick = function () {
      el("#showZones").checked = true;
      showZones(true);
      map.fitBounds(circle.getBounds());
    };
    makeKeyboardAction(head);
    item.appendChild(head);
    var line = document.createElement("div");
    line.className = "muted";
    line.textContent = z.ratio === null
      ? t("z_none")
      : t("z_line", { a: z.inside_density, b: z.ring_density, r: z.ratio });
    item.appendChild(line);

    var maxR = 0;
    z.by_year.forEach(function (y) { if (y.ratio !== null && y.ratio > maxR) maxR = y.ratio; });
    if (maxR > 0) {
      var bars = document.createElement("div");
      bars.className = "yrbars";
      z.by_year.forEach(function (y) {
        var bar = document.createElement("i");
        var hgt = y.ratio === null ? 0 : Math.max(4, (y.ratio / maxR) * 100);
        bar.style.height = hgt + "%";
        bar.title = y.year + ": " + (y.ratio === null ? t("nodata") : y.ratio);
        bars.appendChild(bar);
      });
      item.appendChild(bars);
    }
    box.appendChild(item);
  });
  el("#zoneNote").textContent = res.method + " " + (res.note || "");
  el("#zoneCaveat").textContent = res.caveat;
}
function showZones(on) {
  state.zonesShown = on;
  if (on) zoneLayer.addTo(map); else map.removeLayer(zoneLayer);
}
el("#showZones").onchange = function (e) { showZones(e.target.checked); };

// ---------- вкладки ----------
function showTab(name) {
  els(".tab").forEach(function (b) {
    var active = b.getAttribute("data-tab") === name;
    b.classList.toggle("on", active);
    b.setAttribute("aria-pressed", String(active));
  });
  els(".pane").forEach(function (p) { p.hidden = p.id !== "pane-" + name; });
  animateUpdate(el("#pane-" + name), [{ opacity: 0.55, translate: "0 6px" }, { opacity: 1, translate: "0 0" }], 220);
  if (name === "data") { drawCalendar(); drawSeason(); drawKpiChart(); }
  if (name === "stages" && state.start) loadStages();
}
els(".tab").forEach(function (b) {
  b.onclick = function () { showTab(b.getAttribute("data-tab")); };
});

// ---------- общее ----------
var loadingCount = 0;
function loadAll() {
  status(t("loading"));
  document.body.classList.add("is-loading");
  loadingCount++;
  if (!el("#pane-stages").hidden) loadStages();
  return Promise.all([loadFires(), loadCalendar(), loadAnomalies(), loadSeason(), loadTop(), loadKpis()])
    .then(function () { status(""); })
    .catch(showError)
    .finally(function () {
      if (--loadingCount === 0) document.body.classList.remove("is-loading");
      document.dispatchEvent(new CustomEvent("firecal:loaded"));
    });
}
// Smoothly counts a number up to its new value (instant with reduced motion).
function countTo(node, value) {
  if (value === null) { node.textContent = "—"; node.dataset.value = ""; return; }
  var from = +node.dataset.value || 0;
  node.dataset.value = value;
  if (motionPreference.matches || from === value) { node.textContent = fmt(value); return; }
  var start = performance.now(), duration = 700;
  (function frame(now) {
    if (+node.dataset.value !== value) return;
    var k = Math.min(1, (now - start) / duration), eased = 1 - Math.pow(1 - k, 3);
    node.textContent = fmt(Math.round(from + (value - from) * eased));
    if (k < 1) requestAnimationFrame(frame);
  })(start);
}
function fillYears() {
  var sel = el("#year");
  sel.innerHTML = "";
  var y0 = +state.minDate.slice(0, 4), y1 = +state.maxDate.slice(0, 4);
  for (var y = y0; y <= y1; y++) sel.add(new Option(y, y));
  if (state.year < y0 || state.year > y1) state.year = y1;
  sel.value = state.year;
  fillComparisonOptions(y0, y1);
}

function changeDates() {
  if (!state.minDate) return;
  var start = el("#start").value, end = el("#end").value;
  if (!start || !end || start > end || start < state.minDate || end > state.maxDate) {
    status(t("date_invalid"));
    el("#start").value = state.start;
    el("#end").value = state.end;
    return;
  }
  stop();
  state.start = start;
  state.end = end;
  state.day = null;
  syncSlider();
  loadAll();
}
el("#start").onchange = el("#end").onchange = changeDates;
el("#minConf").onchange = function (e) { state.minConf = +e.target.value; loadAll(); };
el("#land").onchange = function (e) {
  state.land = e.target.value;
  chatVersion++;
  el("#chatLog").replaceChildren();
  addMsg(t("hello"), "bot");
  loadAll();
};
el("#sensor").onchange = function (e) { state.sensor = e.target.value; loadFires().catch(showError); };
el("#normalize").onchange = function (e) { state.normalize = e.target.checked; loadAll(); };
el("#year").onchange = function (e) {
  state.year = +e.target.value;
  loadCalendar().catch(showError);
  drawSeason();
};
el("#level").onchange = function (e) { state.level = e.target.value; loadAnomalies().catch(showError); };
el("#z").onchange = function (e) { state.z = +e.target.value || 2; loadAnomalies().catch(showError); };
el("#exportCsv").onclick = function () {
  el(".export-menu").open = false;
  window.location = API + "/api/export.csv?" + qs({ start: state.start, end: state.end });
};

function exportSurface(width, height) {
  var image = document.createElement("canvas"), dpr = 2;
  image.width = width * dpr;
  image.height = height * dpr;
  var context = image.getContext("2d");
  context.scale(dpr, dpr);
  context.fillStyle = cssVar("--card");
  context.fillRect(0, 0, width, height);
  context.fillStyle = cssVar("--text");
  return { image: image, context: context };
}

function downloadPng(image, name) {
  image.toBlob(function (blob) {
    if (!blob) return;
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  });
  el(".export-menu").open = false;
}

el("#exportPng").onclick = async function () {
  if (!state.year) return;
  await document.fonts.ready;
  var width = Math.max(850, calendarLeft + 54 * 15), chartHeight = calendarTop + 7 * 15;
  var result = exportSurface(width + 48, chartHeight + 199);
  var ctx = result.context;
  ctx.font = "bold 24px Inter, sans-serif";
  ctx.fillText("FireCal / " + t("cal_title") + " " + state.year, 24, 34);
  ctx.font = "13px Inter, sans-serif";
  ctx.fillStyle = cssVar("--muted");
  ctx.fillText(seriesLabel() + " · " + t("land_" + (state.land || "all")), 24, 55);
  var first = [state.year + "-01-01", state.minDate].sort().pop();
  var last = [state.year + "-12-31", state.maxDate].sort()[0];
  ctx.font = "12px 'IBM Plex Mono', monospace";
  ctx.fillText(t("export_calendar_period", { dates: first + " – " + last }), 24, 75);
  ctx.fillText(t("export_selection", { dates: state.start + " – " + state.end }), 24, 93);
  drawCalendar(ctx, 15, 24, 106);
  var legendY = chartHeight + 126;
  ctx.fillStyle = cssVar("--muted");
  ctx.fillText(t("less"), 24, legendY + 12);
  var x = 31 + ctx.measureText(t("less")).width;
  for (var i = 0; i < 5; i++) {
    ctx.fillStyle = cssVar("--c" + i);
    ctx.fillRect(x + i * 18, legendY, 14, 14);
  }
  ctx.fillStyle = cssVar("--muted");
  var tail = x + 95;
  ctx.fillText(t("more_anom"), tail, legendY + 12);
  tail += ctx.measureText(t("more_anom")).width + 18;
  ctx.strokeStyle = cssVar("--muted");
  ctx.strokeRect(tail, legendY + 1, 12, 12);
  ctx.fillText(t("out_of_coverage"), tail + 18, legendY + 12);
  ctx.fillText(t("export_archive"), 24, legendY + 39);
  downloadPng(result.image, "firecal_" + state.year + ".png");
};

el("#exportMap").onclick = function () {
  if (!state.start || (comparison.enabled && !comparison.sides.length)) return;
  var size = map.getSize(), width = Math.max(700, size.x + 32);
  var result = exportSurface(width, size.y + (comparison.enabled ? 185 : 153));
  var ctx = result.context, left = (width - size.x) / 2, top = 70;
  var date = comparison.enabled ? comparison.sides.map(function (side) { return side.year; }).join(" / ") :
    state.day ? (state.cumulative ? state.start + " – " + state.day : state.day) : state.start + " – " + state.end;
  ctx.font = "bold 23px Inter, sans-serif";
  ctx.fillText("FireCal / " + t("map_title"), 16, 31);
  ctx.font = "12px 'IBM Plex Mono', monospace";
  ctx.fillText(date + " · " + t("export_archive") + " · " + t("land_" + (state.land || "all")), 16, 52);
  ctx.save();
  ctx.translate(left, top);
  ctx.beginPath();
  ctx.rect(0, 0, size.x, size.y);
  ctx.clip();
  ctx.fillStyle = cssVar("--map-bg");
  ctx.fillRect(0, 0, size.x, size.y);
  var container = map.getContainer().getBoundingClientRect(), tiles = 0, raster = document.createElement("canvas");
  raster.width = size.x;
  raster.height = size.y;
  var rc = raster.getContext("2d");
  els("#map .leaflet-tile-loaded").forEach(function (tile) {
    if (!tile.naturalWidth || !tile.crossOrigin) return;
    var b = tile.getBoundingClientRect();
    rc.drawImage(tile, b.left - container.left, b.top - container.top, b.width, b.height);
    tiles++;
  });
  ctx.filter = document.documentElement.dataset.theme === "dark" ? "invert(1) grayscale(.9) brightness(.86)" : "none";
  ctx.drawImage(raster, 0, 0);
  ctx.filter = "none";
  function line(layer) {
    var options = layer.options, points = layer.getLatLngs();
    if (!points.length) return;
    var polygon = Array.isArray(points[0]);
    if (polygon) points = points[0];
    ctx.beginPath();
    points.forEach(function (coordinate, i) {
      var point = map.latLngToContainerPoint(coordinate);
      if (i) ctx.lineTo(point.x, point.y);
      else ctx.moveTo(point.x, point.y);
    });
    if (polygon) ctx.closePath();
    ctx.strokeStyle = options.color;
    ctx.globalAlpha = options.opacity == null ? 1 : options.opacity;
    ctx.lineWidth = options.weight;
    ctx.setLineDash(options.dashArray ? options.dashArray.split(" ").map(Number) : []);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
  }
  gridLayer.eachLayer(line);
  if (coverageLayer) line(coverageLayer);
  drawn.eachLayer(line);
  if (state.zonesShown) zoneLayer.eachLayer(function (layer) {
    var box = layer.getBounds(), center = map.latLngToContainerPoint(layer.getLatLng());
    var nw = map.latLngToContainerPoint(box.getNorthWest()), se = map.latLngToContainerPoint(box.getSouthEast());
    ctx.strokeStyle = areaStyle.color;
    ctx.setLineDash(layer.options.dashArray ? [6, 4] : []);
    ctx.beginPath();
    ctx.ellipse(center.x, center.y, Math.abs(se.x - nw.x) / 2, Math.abs(se.y - nw.y) / 2, 0, 0, 2 * Math.PI);
    ctx.stroke();
    ctx.setLineDash([]);
  });
  if (comparison.enabled) {
    paintComparison(ctx, size.x, size.y);
    var divider = size.x * swipe.value / 100;
    ctx.strokeStyle = "#8dc9ed";
    ctx.beginPath();
    ctx.moveTo(divider, 0);
    ctx.lineTo(divider, size.y);
    ctx.stroke();
  } else {
    ctx.globalAlpha = .68;
    paintPoints(ctx, visibleFires, size.x, size.y);
    ctx.globalAlpha = 1;
  }
  var scale = el(".leaflet-control-scale-line");
  if (scale && scale.offsetWidth) {
    var scaleY = size.y - 13, scaleWidth = scale.offsetWidth;
    ctx.fillStyle = cssVar("--card");
    ctx.fillRect(6, scaleY - 18, scaleWidth + 8, 25);
    ctx.fillStyle = cssVar("--text");
    ctx.strokeStyle = ctx.fillStyle;
    ctx.fillText(scale.textContent, 10, scaleY - 3);
    ctx.strokeRect(10, scaleY, scaleWidth, 1);
  }
  ctx.restore();
  ctx.fillStyle = cssVar("--muted");
  var footer = top + size.y + 20;
  var legendX = 16;
  [
    [t("observations"), cssVar("--heat"), []],
    [t("coverage_legend"), coverageStyle.color, [3, 2]],
    [t("area_title"), areaStyle.color, []]
  ].forEach(function (entry, i) {
    ctx.strokeStyle = ctx.fillStyle = entry[1];
    ctx.setLineDash(entry[2]);
    if (i === 0) {
      ctx.beginPath();
      ctx.arc(legendX + 6, footer - 4, 3, 0, Math.PI * 2);
      ctx.fill();
    } else ctx.strokeRect(legendX, footer - 8, 12, 8);
    ctx.setLineDash([]);
    ctx.fillStyle = cssVar("--muted");
    ctx.fillText(entry[0], legendX + 17, footer);
    legendX += 31 + ctx.measureText(entry[0]).width;
  });
  ctx.fillText(tiles ? "© OpenStreetMap contributors · openstreetmap.org/copyright" : t("basemap_unavailable"), 16, footer + 18);
  if (comparison.enabled) {
    comparison.sides.forEach(function (side, i) {
      ctx.fillText(comparisonLabel(side).join(" · "), 16, footer + 36 + i * 16, width - 32);
    });
  }
  var frpY = footer + (comparison.enabled ? 70 : 37);
  ctx.fillText(t("frp_unit"), 16, frpY);
  var swatchX = 26 + ctx.measureText(t("frp_unit")).width, palette = frpPalette();
  FRP_LABELS.forEach(function (label, i) {
    ctx.fillStyle = palette[i];
    ctx.fillRect(swatchX, frpY - 10, 11, 11);
    ctx.fillStyle = cssVar("--muted");
    ctx.fillText(label, swatchX + 15, frpY);
    swatchX += 26 + ctx.measureText(label).width;
  });
  try { downloadPng(result.image, "firecal_map.png"); }
  catch (error) { status(t("map_export_error")); }
};

// ---------- ссылка на текущий вид ----------
function shareParams() {
  var p = new URLSearchParams();
  if (state.bbox) p.set("b", state.bbox);
  p.set("s", state.start);
  p.set("e", state.end);
  if (state.minConf > 1) p.set("c", state.minConf);
  if (state.sensor) p.set("m", state.sensor);
  if (state.land) p.set("lc", state.land);
  p.set("n", state.normalize ? "1" : "0");
  if (state.year) p.set("y", state.year);
  if (state.level !== "day") p.set("l", state.level);
  if (state.z !== 2) p.set("z", state.z);
  if (state.day) p.set("d", state.day);
  if (state.palette !== "fire") p.set("p", state.palette);
  if (comparison.enabled) {
    p.set("cl", yearLeft.value);
    p.set("cr", yearRight.value);
    p.set("cm", monthSelect.value);
    p.set("sw", swipe.value);
  }
  p.set("lg", LANG);
  return p.toString();
}
function applyHash() {
  var h = location.hash.replace(/^#/, "");
  if (!h) return;
  var p = new URLSearchParams(h);
  if (p.get("s")) state.start = p.get("s");
  if (p.get("e")) state.end = p.get("e");
  if (p.get("c")) state.minConf = +p.get("c");
  if (p.get("m")) state.sensor = p.get("m");
  if (["forest", "cropland", "both"].includes(p.get("lc"))) state.land = p.get("lc");
  if (p.has("n")) state.normalize = p.get("n") === "1";
  else if (p.has("s") && p.has("e")) state.normalize = false;
  if (p.get("y")) state.year = +p.get("y");
  if (p.get("l")) state.level = p.get("l");
  if (p.get("z")) state.z = +p.get("z") || 2;
  if (p.get("d")) state.day = p.get("d");
  if (p.get("p") === "cb" || p.get("p") === "fire") {
    state.palette = p.get("p");
    document.documentElement.dataset.palette = state.palette;
  }
  if (p.get("b")) {
    state.bbox = p.get("b");
    var a = state.bbox.split(",").map(Number);
    if (a.length === 4 && a.every(function (v) { return !isNaN(v); })) {
      L.rectangle([[a[1], a[0]], [a[3], a[2]]], areaStyle).addTo(drawn);
    } else {
      state.bbox = null;
    }
  }
}
function syncUi() {
  el("#start").value = state.start;
  el("#end").value = state.end;
  el("#minConf").value = String(state.minConf);
  el("#sensor").value = state.sensor;
  el("#land").value = state.land;
  el("#normalize").checked = state.normalize;
  el("#level").value = state.level;
  el("#z").value = state.z;
  el("#palette").value = state.palette;
}
el("#share").onclick = function () {
  var hash = "#" + shareParams();
  history.replaceState(null, "", hash);
  var url = location.href;
  function done() {
    status(t("link_ok"));
    setTimeout(function () { status(""); }, 2500);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(done, function () { window.prompt(t("link_prompt"), url); });
  } else {
    window.prompt(t("link_prompt"), url);
  }
};

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("theme", t); } catch (err) {}
  redrawAll();
}
el("#theme").onclick = function () {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
};

function setLang(l) {
  LANG = l;
  try { localStorage.setItem("lang", l); } catch (err) {}
  var hash = new URLSearchParams(location.hash.slice(1));
  if (hash.has("lg")) {
    hash.set("lg", l);
    history.replaceState(null, "", "#" + hash);
  }
  refreshLanguage();
  if (state.start) loadAll();
}
function refreshLanguage() {
  applyStaticLang();
  renderFrpLegend();
  if (el("#land").disabled) el("#landHint").textContent = t("land_unavailable");
  syncPlayButton();
  renderDrawControl();
  el("#lang").textContent = LANG === "ru" ? "EN" : "RU";
  ++chatVersion;
  el("#chatLog").innerHTML = "";
  addMsg(t("hello"), "bot");
  syncSlider();
  if (startupFailed) status(t("startup_error"));
  if (state.minDate) el("#coverage").textContent = t("coverage", { from: state.minDate, to: state.maxDate });
  el("#landingCoverage").textContent = state.minDate ? t("coverage", { from: state.minDate, to: state.maxDate }) : "";
  fillComparisonMonths();
  if (lastRel) renderReliability(lastRel[0], lastRel[1], lastRel[2]);
  renderKpis();
  drawCalendar();
}
el("#lang").onclick = function () { setLang(LANG === "ru" ? "en" : "ru"); };


function seriesLabel() { return t(state.normalize ? "series_viirs" : "series_observed"); }

function makeKeyboardAction(node) {
  node.tabIndex = 0;
  node.setAttribute("role", "button");
  node.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    node.click();
  });
}

function syncCalendarYear(day) {
  var year = +day.slice(0, 4);
  if (year === state.year) return;
  state.year = year;
  el("#year").value = year;
  loadCalendar().catch(showError);
  drawSeason();
}

cal.addEventListener("keydown", function (event) {
  if (!state.year) return;
  var first = [state.year + "-01-01", state.minDate].sort().pop();
  var last = [state.year + "-12-31", state.maxDate].sort()[0];
  var day = state.day || (state.start.startsWith(state.year + "-") ? state.start : first);
  var step = { ArrowLeft: -7, ArrowRight: 7, ArrowUp: -1, ArrowDown: 1 }[event.key];
  if (step) day = addDays(day, step);
  else if (event.key === "Home") day = first;
  else if (event.key === "End") day = last;
  else if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  stop();
  setDay([first, [day, last].sort()[0]].sort().pop());
  var key = Object.keys(state.hit).find(function (key) { return state.hit[key] === state.day; });
  if (key) cal.parentElement.scrollLeft = Math.max(0, +key.split(",")[0] * calendarStep - cal.parentElement.clientWidth / 2);
});

var comparison = { enabled: false, version: 0, sides: [] };
var comparisonCanvas = L.DomUtil.create("canvas", "comparison-points");
el("#compareOverlay").prepend(comparisonCanvas);
var yearLeft = el("#compareLeft"), yearRight = el("#compareRight"), monthSelect = el("#compareMonth");
var swipe = el("#swipe");

function fillComparisonOptions(first, last) {
  var left = yearLeft.value || last - 1, right = yearRight.value || last;
  [yearLeft, yearRight].forEach(function (select) {
    select.replaceChildren();
    for (var year = last; year >= first; year--) select.add(new Option(year, year));
  });
  yearLeft.value = Math.max(first, Math.min(last, left));
  yearRight.value = Math.max(first, Math.min(last, right));
}

function fillComparisonMonths() {
  var month = monthSelect.value || (state.start ? String(+state.start.slice(5, 7)) : "7");
  monthSelect.replaceChildren(new Option(t("all_period"), "0"));
  t("mon").forEach(function (label, index) { monthSelect.add(new Option(label, index + 1)); });
  monthSelect.value = month;
}

function comparisonWindow(year) {
  var month = +monthSelect.value;
  var prefix = year + "-" + String(month).padStart(2, "0");
  var start = month ? prefix + "-01" : year + "-01-01";
  var end = month ? new Date(Date.UTC(year, month, 0)).toISOString().slice(0, 10) : year + "-12-31";
  // A leap day would give one side an extra day in a February or whole-year comparison.
  var leap = [yearLeft.value, yearRight.value].map(function (y) { return new Date(Date.UTC(+y, 1, 29)).getUTCMonth() === 1; });
  var skipLeapDay = leap[0] !== leap[1];
  if (month === 2 && skipLeapDay) end = prefix + "-28";
  var from = start < state.minDate ? state.minDate : start;
  var to = end > state.maxDate ? state.maxDate : end;
  return { year: year, start: start, end: end, from: from, to: to, empty: from > to, skipLeapDay: !month && skipLeapDay };
}

function comparisonLabel(side) {
  var dates = side.start + " – " + side.end;
  var labels = [side.year, dates];
  if (side.empty) labels.push(t("compare_empty"));
  else if (side.start !== side.from || side.end !== side.to) {
    var missing = [];
    if (side.start !== side.from) missing.push(side.start + " – " + addDays(side.from, -1));
    if (side.end !== side.to) missing.push(addDays(side.to, 1) + " – " + side.end);
    labels.push(t("compare_coverage", { dates: missing.join(", ") }));
  }
  if (side.rows && !side.empty && !side.rows.length && !side.outside) labels.push(t("compare_no_detections"));
  if (side.skipLeapDay) labels.push(t("leap_omitted"));
  return labels;
}

function loadComparison() {
  var version = ++comparison.version;
  var sides = [comparisonWindow(yearLeft.value), comparisonWindow(yearRight.value)];
  var area = state.bbox && state.bbox.split(",").map(Number);
  var outside = area && archiveBounds && !archiveBounds.intersects([[area[1], area[0]], [area[3], area[2]]]);
  comparison.sides = [];
  drawComparison();
  el("#compareStatus").textContent = t(outside ? "compare_outside_area" : "loading");
  renderComparisonLabels(sides);
  return Promise.all(sides.map(async function (side) {
    side.outside = outside;
    side.rows = side.empty || outside ? [] : await api("/api/fires", qs({
      start: side.from, end: side.to, sensor: state.sensor, limit: MAP_LIMIT,
      omit_leap_day: side.skipLeapDay ? 1 : null
    }));
    return side;
  })).then(function (result) {
    if (version !== comparison.version || !comparison.enabled) return;
    comparison.sides = result;
    renderComparisonLabels(result);
    drawComparison();
    el("#compareStatus").textContent = outside ? t("compare_outside_area") :
      t("compare_observed") + (result.some(function (s) { return s.rows.length === MAP_LIMIT; }) ? " " + t("compare_limit", { n: fmt(MAP_LIMIT) }) : "");
  }).catch(function (error) {
    if (version === comparison.version) el("#compareStatus").textContent = t("err") + error.message;
  });
}

function renderComparisonLabels(sides) {
  [el("#compareLabelLeft"), el("#compareLabelRight")].forEach(function (node, index) {
    node.replaceChildren();
    comparisonLabel(sides[index]).forEach(function (label, i) {
      var line = document.createElement(i === 0 ? "strong" : "span");
      line.textContent = label;
      node.append(line);
    });
  });
}

function paintPoints(context, rows, width, height, palette) {
  palette = palette || frpPalette();
  rows.forEach(function (row) {
    var power = meanFrp(row);
    var point = map.latLngToContainerPoint([row.lat, row.lon]), radius = hotspotRadius(power);
    if (point.x < -radius || point.x > width + radius || point.y < -radius || point.y > height + radius) return;
    context.fillStyle = frpColor(power, palette);
    context.beginPath();
    context.arc(point.x, point.y, radius, 0, 2 * Math.PI);
    context.fill();
  });
}

function paintComparison(context, width, height) {
  var divider = width * swipe.value / 100;
  var palette = frpPalette();
  comparison.sides.forEach(function (side, index) {
    context.save();
    context.beginPath();
    context.rect(index ? divider : 0, 0, index ? width - divider : divider, height);
    context.clip();
    if (side.empty) {
      context.fillStyle = "#11161b83";
      context.fillRect(0, 0, width, height);
      context.strokeStyle = "#9fabb048";
      for (var x = -height; x < width; x += 12) {
        context.beginPath();
        context.moveTo(x, height);
        context.lineTo(x + height, 0);
        context.stroke();
      }
    }
    context.globalAlpha = .70;
    paintPoints(context, side.rows, width, height, palette);
    context.restore();
  });
}

function drawComparison() {
  if (!comparison.enabled || el("#atlas").hidden) return;
  var size = map.getSize(), dpr = Math.min(devicePixelRatio || 1, 2);
  var width = Math.round(size.x * dpr), height = Math.round(size.y * dpr);
  if (comparisonCanvas.width !== width || comparisonCanvas.height !== height) {
    comparisonCanvas.width = width;
    comparisonCanvas.height = height;
  }
  var context = comparisonCanvas.getContext("2d");
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  context.clearRect(0, 0, size.x, size.y);
  paintComparison(context, size.x, size.y);
}

function closeComparison(reload) {
  if (!comparison.enabled) return;
  comparison.enabled = false;
  comparison.version++;
  el("#compareControls").hidden = el("#compareOverlay").hidden = true;
  el("#compareToggle").setAttribute("aria-expanded", "false");
  el("#compareToggle").textContent = t("compare_title");
  fireLayer.addTo(map);
  if (reload) loadFires().catch(showError);
}

el("#compareToggle").onclick = function () {
  if (!state.start) return;
  if (comparison.enabled) { closeComparison(true); return; }
  stop();
  firesRequest++;
  comparison.enabled = true;
  fireLayer.clearLayers();
  fireLayer.remove();
  el("#compareControls").hidden = el("#compareOverlay").hidden = false;
  el("#compareToggle").setAttribute("aria-expanded", "true");
  loadComparison();
};
[yearLeft, yearRight, monthSelect].forEach(function (select) { select.onchange = loadComparison; });
swipe.oninput = function () {
  el("#compareOverlay").style.setProperty("--swipe", swipe.value + "%");
  drawComparison();
};
var swipeDragging = false;
el(".swipe-line").addEventListener("pointerdown", function (event) {
  if (event.button !== 0) return;
  swipeDragging = true;
  event.currentTarget.setPointerCapture(event.pointerId);
  swipe.focus({ preventScroll: true });
  event.preventDefault();
});
el(".swipe-line").addEventListener("pointermove", function (event) {
  if (!swipeDragging) return;
  var bounds = el("#compareOverlay").getBoundingClientRect();
  swipe.value = Math.round((event.clientX - bounds.left) / bounds.width * 100);
  swipe.oninput();
});
el(".swipe-line").addEventListener("lostpointercapture", function () { swipeDragging = false; });
map.on("move resize", drawComparison);

var openedAtlas = false, flyingToAtlas = false;
function fitArchive() {
  if (!archiveBounds || el("#atlas").hidden) return;
  var b = drawn.getLayers()[0];
  map.options.zoomSnap = .25;
  map.fitBounds(b ? b.getBounds() : archiveBounds, { padding: [30, 30], maxZoom: 13, animate: false });
}
function openAtlas(focus) {
  el("#landing").hidden = true;
  el("#atlas").hidden = el(".work-actions").hidden = false;
  map.invalidateSize({ pan: false });
  drawCalendar();
  drawSeason();
  drawKpiChart();
  if (!openedAtlas) fitArchive();
  openedAtlas = !!archiveBounds;
  window.scrollTo(0, 0);
  if (focus) el("#mapTitle").focus({ preventScroll: true });
  if (focus && flyingToAtlas && globe.gl) {
    animateUpdate(el(".map-frame"), [{ opacity: .38 }, { opacity: 1 }], 230);
  }
  flyingToAtlas = false;
  el("#explore").disabled = false;
}
el("#explore").onclick = function () {
  if (flyingToAtlas) return;
  flyingToAtlas = true;
  el("#explore").disabled = true;
  globe.fly(function () { openAtlas(true); }, motionPreference.matches);
};
el("#home").onclick = function () {
  if (el("#atlas").hidden) return;
  stop();
  toggleSheet(false, false);
  el("#atlas").hidden = el(".work-actions").hidden = true;
  el("#landing").hidden = false;
  globe.reset();
  window.scrollTo(0, 0);
  el("#explore").focus();
};

var mobileSheet = matchMedia("(max-width: 760px)");
function toggleSheet(open, focus) {
  open = open && mobileSheet.matches;
  var panel = el("#sideControls");
  panel.classList.toggle("sheet-open", open);
  panel.inert = mobileSheet.matches && !open;
  if (open) {
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
  } else {
    panel.removeAttribute("role");
    panel.removeAttribute("aria-modal");
  }
  el("#sheetBackdrop").hidden = !open;
  document.body.classList.toggle("sheet-active", open);
  el("#openSheet").setAttribute("aria-expanded", String(open));
  if (focus) el(open ? "#closeSheet" : "#openSheet").focus();
}
el("#openSheet").onclick = function () { toggleSheet(true, true); };
el("#closeSheet").onclick = el("#sheetBackdrop").onclick = function () { toggleSheet(false, true); };
document.addEventListener("keydown", function (event) {
  if (el("#sheetBackdrop").hidden) return;
  if (event.key === "Escape") toggleSheet(false, true);
  if (event.key !== "Tab") return;
  var controls = els("#sideControls button:not(:disabled), #sideControls input:not(:disabled), #sideControls select:not(:disabled)");
  var first = controls[0], last = controls[controls.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});
mobileSheet.addEventListener("change", function () { toggleSheet(false, false); });
toggleSheet(false, false);

(async function init() {
  var saved = null;
  try { saved = localStorage.getItem("theme"); } catch (err) {}
  try {
    var sp = localStorage.getItem("palette");
    if (sp === "cb" || sp === "fire") { state.palette = sp; document.documentElement.dataset.palette = sp; }
  } catch (err) {}
  applyTheme(saved || "dark");
  refreshLanguage();
  status(t("loading"));
  try {
    var meta = await api("/api/meta", "");
    var r = meta.range;
    if (!r.min_date || !r.max_date) throw new Error("Empty archive");
    state.minDate = iso(r.min_date);
    state.maxDate = iso(r.max_date);
    var extent = [r.west, r.south, r.east, r.north];
    archiveBounds = L.latLngBounds([[r.south, r.west], [r.north, r.east]]);
    coverageLayer = L.rectangle(archiveBounds, coverageStyle).addTo(map);
    globe.setBounds(extent);
    state.start = state.minDate;
    state.end = state.maxDate;
    state.year = +state.maxDate.slice(0, 4);
    state.normalize = meta.calibrated;
    applyHash();
    el("#land").disabled = !meta.land_cover;
    if (!meta.land_cover) state.land = "";
    if (!validDate(state.start) || state.start < state.minDate) state.start = state.minDate;
    if (!validDate(state.end) || state.end > state.maxDate) state.end = state.maxDate;
    if (state.start > state.end) { state.start = state.minDate; state.end = state.maxDate; }
    if (state.day && (!validDate(state.day) || state.day < state.start || state.day > state.end)) state.day = null;
    el("#normalize").disabled = !meta.calibrated;
    if (!meta.calibrated) state.normalize = false;
    refreshLanguage();
    syncUi();
    fillYears();
    syncSlider();
    for (var field of [el("#start"), el("#end")]) {
      field.min = state.minDate;
      field.max = state.maxDate;
    }
    var view = new URLSearchParams(location.hash.slice(1));
    if (view.has("s") || view.has("b")) openAtlas(false);
    fitArchive();
    if (view.has("cl") && view.has("cr")) {
      if (yearLeft.querySelector('option[value="' + CSS.escape(view.get("cl")) + '"]')) yearLeft.value = view.get("cl");
      if (yearRight.querySelector('option[value="' + CSS.escape(view.get("cr")) + '"]')) yearRight.value = view.get("cr");
      if (/^(?:[0-9]|1[012])$/.test(view.get("cm"))) monthSelect.value = view.get("cm");
      swipe.value = view.get("sw") || 50;
      swipe.oninput();
      el("#compareToggle").click();
    }
    await loadAll();
  } catch (e) {
    startupFailed = true;
    console.error(e);
    status(t("startup_error"));
  }
})();
