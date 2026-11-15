// Hotspot outlook backtest: a separate map layer with its own issue date and horizon.
(function () {
  var button = el("#riskToggle"), controls = el("#riskControls"), date = el("#riskDate");
  var horizon = el("#riskHorizon"), mode = el("#riskMode");
  var statusNode = el("#riskStatus"), legend = el("#riskLegend"), areaNode = el("#riskArea"), skill = el("#riskSkill");
  var layer = L.layerGroup(), dots = L.layerGroup().addTo(layer), overlay = null;
  var metadata, plotted, active = false, version = 0, archiveView, holdout = new Set();
  var DEFAULT_DAY = "2025-04-12";

  // Sequential ramp (light → dark) stays ordered for colour-vision deficiencies.
  var P_STOPS = [0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.4];
  var P_COLORS = [null, "#fff1b8", "#fdd27f", "#fca95c", "#f27a3d", "#d94a2b", "#a6232f", "#5e1336"];
  // Diverging ramp for the ratio to the seasonal climatology.
  var R_STOPS = [0.5, 0.8, 1.25, 2, 4];
  var R_COLORS = ["#2b6fae", "#8fc1de", "rgba(200,200,200,0.35)", "#f9b26b", "#e8612f", "#b4202e"];

  function text(key, vars) { return t("risk_" + key, vars); }
  function pct(v) { return (v * 100).toLocaleString(LANG, { maximumFractionDigits: v < 0.01 ? 2 : 1 }); }
  function bucket(stops, v) {
    var i = stops.findIndex(function (s) { return v < s; });
    return i === -1 ? stops.length : i;
  }
  // Below this probability a cell stays transparent, so the map shows where fire is plausible.
  function minimum() { return horizon.value === "7" ? 0.01 : 0.005; }
  function colorFor(p, c) {
    if (mode.value === "ratio") {
      // Ratios between tiny probabilities are noise; show them only where fire is plausible.
      if (Math.max(p, c) < 2 * minimum()) return null;
      return R_COLORS[bucket(R_STOPS, p / Math.max(c, 1e-4))];
    }
    return p < minimum() ? null : P_COLORS[bucket(P_STOPS, p)];
  }

  function drawLegend() {
    legend.replaceChildren();
    var ratio = mode.value === "ratio", stops = ratio ? R_STOPS : P_STOPS, colors = ratio ? R_COLORS : P_COLORS;
    var label = function (v) { return ratio ? "×" + v : pct(v) + "%"; };
    colors.forEach(function (color, i) {
      var lo = i === 0 ? null : stops[i - 1], hi = i < stops.length ? stops[i] : null;
      if (!color || (!ratio && hi !== null && hi <= minimum())) return;
      var item = document.createElement("span"), swatch = document.createElement("i");
      swatch.style.background = color;
      item.append(swatch, lo === null ? "<" + label(hi) : hi === null ? "≥" + label(lo) : label(lo) + "–" + label(hi));
      legend.append(item);
    });
    legend.append(text("observed_key"), text("holdout_key"));
  }

  function mercator(lat) { return Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360)); }

  function paint() {
    var g = metadata.grid, px = 4, width = g.cols * px;
    var north = g.south + g.rows * g.step, east = g.west + g.cols * g.step;
    var perRadian = width / ((east - g.west) * Math.PI / 180);
    var top = mercator(north), height = Math.round((top - mercator(g.south)) * perRadian);
    var cvs = document.createElement("canvas");
    cvs.width = width;
    cvs.height = height;
    var ctx = cvs.getContext("2d");
    var rowY = [];
    for (var r = 0; r <= g.rows; r++) rowY.push((top - mercator(g.south + r * g.step)) * perRadian);
    var p = plotted.probability, c = plotted.climatology;
    for (var row = 0; row < g.rows; row++) {
      var y0 = Math.floor(rowY[row + 1]), y1 = Math.ceil(rowY[row]);
      for (var col = 0; col < g.cols; col++) {
        var i = row * g.cols + col, color = colorFor(p[i] / 1e4, c[i] / 1e4);
        if (!color) continue;
        ctx.fillStyle = color;
        ctx.fillRect(col * px, y0, px, y1 - y0);
      }
    }
    // Dashed outlines around 4°×4° blocks that never entered training.
    var block = 16, done = {}, rects = [];
    holdout.forEach(function (i) {
      var br = Math.floor(Math.floor(i / g.cols) / block), bc = Math.floor((i % g.cols) / block);
      if (done[br + "," + bc]) return;
      done[br + "," + bc] = true;
      var r0 = br * block, r1 = Math.min(g.rows, r0 + block), c0 = bc * block, c1 = Math.min(g.cols, c0 + block);
      rects.push([c0 * px + 1.5, rowY[r1] + 1.5, (c1 - c0) * px - 3, rowY[r0] - rowY[r1] - 3]);
    });
    // A dark halo under a light dash keeps the outline visible on both basemap themes.
    [["rgba(20,24,28,0.8)", 3, []], ["rgba(245,248,250,0.95)", 1.5, [6, 4]]].forEach(function (s) {
      ctx.strokeStyle = s[0];
      ctx.lineWidth = s[1];
      ctx.setLineDash(s[2]);
      rects.forEach(function (r) { ctx.strokeRect(r[0], r[1], r[2], r[3]); });
    });
    if (overlay) overlay.remove();
    overlay = L.imageOverlay(cvs.toDataURL(), [[g.south, g.west], [north, east]],
      { opacity: 0.82, interactive: false, className: "risk-overlay" }).addTo(layer);
  }

  function render() {
    drawLegend();
    renderSkill();
    if (!plotted) return;
    paint();
    dots.clearLayers();
    var g = metadata.grid;
    (plotted.observed || []).forEach(function (i) {
      var row = Math.floor(i / g.cols), col = i % g.cols;
      L.circleMarker([g.south + (row + 0.5) * g.step, g.west + (col + 0.5) * g.step], {
        radius: 2.6, weight: 1, color: "#1b2126", fillColor: "#ffffff", fillOpacity: 1, interactive: false,
      }).addTo(dots);
    });
    var windowText = plotted.window[0] === plotted.window[1] ? plotted.window[0] : plotted.window[0] + " – " + plotted.window[1];
    statusNode.textContent = plotted.valid
      ? text("count", { window: windowText, count: plotted.observed.length })
      : text("unknown", { window: windowText });
    var a = plotted.area;
    areaNode.textContent = a ? text(a.observed === null ? "area_nodata" : "area", {
      e: fmt(a.expected), n: fmt(a.normal), o: a.observed === null ? "" : fmt(a.observed),
    }) : text("area_hint");
  }

  function renderSkill() {
    if (!metadata) return;
    var target = "y" + horizon.value, known = metadata.test[target].known;
    var rows = Object.keys(metadata.models).map(function (name) {
      var s = known[name], chosen = name === metadata.selected;
      return "<tr" + (chosen ? ' class="chosen"' : "") + "><td>" + text("m_" + name) + (chosen ? " ★" : "") +
        "</td><td>" + s.average_precision.toFixed(3) + "</td><td>" + Math.round(s.top10_capture * 100) +
        "%</td><td>" + s.brier_skill_vs_climatology.toFixed(2) + "</td></tr>";
    }).join("");
    var held = metadata.test[target].holdout[metadata.selected], forest = metadata.test[target].forest[metadata.selected];
    skill.innerHTML = '<table class="risk-table"><thead><tr><th>' + text("col_model") + "</th><th>AP</th><th>" +
      text("col_top") + "</th><th>BSS</th></tr></thead><tbody>" + rows + "</tbody></table>" +
      "<p>" + text("skill_known", { days: known.days, pos: fmt(known.positives) }) + "</p>" +
      "<p>" + text("skill_holdout", { ap: held.average_precision.toFixed(3), top: Math.round(held.top10_capture * 100) }) + "</p>" +
      "<p>" + text("skill_forest", { ap: forest.average_precision.toFixed(3), top: Math.round(forest.top10_capture * 100) }) + "</p>" +
      "<p>" + text("skill_note") + "</p>";
  }

  function onClick(e) {
    if (!active || !plotted) return;
    var g = metadata.grid;
    var row = Math.floor((e.latlng.lat - g.south) / g.step), col = Math.floor((e.latlng.lng - g.west) / g.step);
    if (row < 0 || col < 0 || row >= g.rows || col >= g.cols) return;
    var i = row * g.cols + col, p = plotted.probability[i] / 1e4, c = plotted.climatology[i] / 1e4;
    var box = document.createElement("div");
    var lines = [text("probability", { value: pct(p) }), text("normal", { value: pct(c) })];
    if (c > 0) lines.push(text("ratio", { value: (p / c).toLocaleString(LANG, { maximumFractionDigits: 1 }) }));
    lines.push(!plotted.valid ? text("unknown_cell") : plotted.observed.indexOf(i) >= 0 ? text("seen") : text("not_seen"));
    if (holdout.has(i)) lines.push(text("holdout"));
    lines.forEach(function (line, n) {
      if (n) box.append(document.createElement("br"));
      box.append(line);
    });
    L.popup().setLatLng(e.latlng).setContent(box).openOn(map);
  }

  async function load(fit) {
    var request = ++version;
    statusNode.textContent = text("loading");
    try {
      if (!metadata) {
        metadata = await api("/api/fire-risk");
        holdout = new Set(metadata.holdout);
        date.min = metadata.dates[0];
        date.max = metadata.dates[1];
        if (!date.value) date.value = DEFAULT_DAY >= date.min && DEFAULT_DAY <= date.max ? DEFAULT_DAY : date.max;
      }
      if (!active || request !== version) return;
      if (fit) {
        var g = metadata.grid;
        map.fitBounds([[g.south, g.west], [g.south + g.rows * g.step, g.west + g.cols * g.step]],
          { padding: [12, 12], animate: false });
      }
      var params = new URLSearchParams({ day: date.value, horizon: horizon.value });
      if (state.bbox) params.set("bbox", state.bbox);
      var result = await api("/api/fire-risk", params);
      if (active && request === version) { plotted = result; render(); }
    } catch (error) {
      if (active && request === version) {
        plotted = null;
        if (overlay) { overlay.remove(); overlay = null; }
        dots.clearLayers();
        statusNode.textContent = text("unavailable");
      }
    }
  }

  function toggle(show) {
    active = show;
    version++;
    controls.hidden = !show;
    button.setAttribute("aria-expanded", String(show));
    archivePane.hidden = show;
    el("#frpLegend").hidden = show;
    el(".timelapse").hidden = show;
    el("#exportMap").disabled = show;
    if (show) {
      archiveView = { center: map.getCenter(), zoom: map.getZoom() };
      if (comparison.enabled) closeComparison(true);
      stop();
      layer.addTo(map);
      map.on("click", onClick);
      drawLegend();
      load(true);
    } else {
      layer.remove();
      map.off("click", onClick);
      map.closePopup();
      map.setView(archiveView.center, archiveView.zoom, { animate: false });
    }
  }
  button.onclick = function () { toggle(!active); };
  date.onchange = function () { load(false); };
  horizon.onchange = function () { drawLegend(); load(false); };
  mode.onchange = function () { if (plotted) render(); else drawLegend(); };
  map.on(L.Draw.Event.CREATED + " " + L.Draw.Event.EDITED + " " + L.Draw.Event.DELETED, function () {
    if (active) setTimeout(function () { load(false); }, 0);
  });
  el("#compareToggle").addEventListener("click", function () {
    if (active) toggle(false);
  }, true);
  new MutationObserver(function () {
    if (!active) return;
    if (plotted) render();
    else load();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["lang"] });
})();
