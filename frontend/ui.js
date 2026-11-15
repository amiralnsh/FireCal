// AI-assisted: developed with help from the Claude AI assistant (Anthropic); see README, 'Use of AI tools'.
// FireCal UI layer: starfield, mode switch, quick start, period presets and the year × month calendar.
// Loaded after app.js and forecast.js; uses their globals (state, map, drawControl, t, el, ...).
(function () {
  var reduced = motionPreference.matches;
  document.documentElement.classList.add("js-ui");

  // ---------- starfield ----------
  var sky = el("#stars"), skyCtx = sky.getContext("2d"), stars = [], lastFrame = 0;
  function seedStars() {
    var w = innerWidth, h = innerHeight, dpr = Math.min(2, devicePixelRatio || 1);
    sky.width = w * dpr;
    sky.height = h * dpr;
    skyCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    var count = Math.round(w * h / 7000);
    stars = [];
    for (var i = 0; i < count; i++) {
      stars.push({
        x: Math.random() * w, y: Math.random() * h, r: Math.random() * 1.1 + 0.25,
        a: Math.random() * 0.55 + 0.15, phase: Math.random() * Math.PI * 2,
        speed: Math.random() < 0.25 ? 0.6 + Math.random() * 1.4 : 0,
        hue: Math.random() < 0.15 ? "255,214,170" : "200,222,255",
      });
    }
    drawStars(performance.now());
  }
  function drawStars(now) {
    skyCtx.clearRect(0, 0, innerWidth, innerHeight);
    stars.forEach(function (s) {
      var alpha = s.speed ? s.a * (0.55 + 0.45 * Math.sin(s.phase + now / 1000 * s.speed)) : s.a;
      skyCtx.fillStyle = "rgba(" + s.hue + "," + alpha.toFixed(3) + ")";
      skyCtx.beginPath();
      skyCtx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      skyCtx.fill();
    });
  }
  function twinkle(now) {
    if (!document.hidden && document.documentElement.dataset.theme === "dark" && now - lastFrame > 80) {
      lastFrame = now;
      drawStars(now);
    }
    requestAnimationFrame(twinkle);
  }
  seedStars();
  addEventListener("resize", function () { clearTimeout(seedStars.timer); seedStars.timer = setTimeout(seedStars, 200); });
  if (!reduced) requestAnimationFrame(twinkle);

  // ---------- reveal panels as they scroll into view ----------
  var revealer = "IntersectionObserver" in window && !reduced ? new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) { entry.target.classList.add("in-view"); revealer.unobserve(entry.target); }
    });
  }, { rootMargin: "0px 0px -40px 0px" }) : null;
  els(".panel").forEach(function (node, i) {
    node.style.setProperty("--reveal-delay", Math.min(i, 6) * 45 + "ms");
    if (revealer) revealer.observe(node); else node.classList.add("in-view");
  });

  // ---------- map mode switch ----------
  var switcher = el(".mode-switch"), modes = { archive: el("#modeArchive"), compare: el("#compareToggle"), risk: el("#riskToggle") };
  function activeMode() {
    if (modes.compare.getAttribute("aria-expanded") === "true") return "compare";
    if (modes.risk.getAttribute("aria-expanded") === "true") return "risk";
    return "archive";
  }
  function syncModes() {
    var active = activeMode();
    Object.keys(modes).forEach(function (key) {
      modes[key].classList.toggle("on", key === active);
      modes[key].setAttribute("aria-pressed", String(key === active));
    });
    var b = modes[active];
    if (b.offsetWidth) {
      switcher.style.setProperty("--x", b.offsetLeft + "px");
      switcher.style.setProperty("--w", b.offsetWidth + "px");
    }
    document.body.dataset.mode = active;
    syncHint();
  }
  modes.archive.onclick = function () {
    if (activeMode() === "compare") modes.compare.click();
    if (activeMode() === "risk") modes.risk.click();
  };
  new MutationObserver(syncModes).observe(modes.compare, { attributes: true, attributeFilter: ["aria-expanded"] });
  new MutationObserver(syncModes).observe(modes.risk, { attributes: true, attributeFilter: ["aria-expanded"] });
  new ResizeObserver(syncModes).observe(switcher);

  // ---------- drawing an area ----------
  var drawing = false;
  function startDraw() {
    toggleSheet(false, false);
    if (activeMode() === "compare") modes.compare.click();
    el(".map-frame").scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
    new L.Draw.Rectangle(map, drawControl.options.draw.rectangle).enable();
  }
  el("#drawArea").onclick = startDraw;
  el("#mapHint").onclick = startDraw;
  map.on("draw:drawstart", function () { drawing = true; syncHint(); });
  map.on("draw:drawstop", function () { drawing = false; syncHint(); });
  function syncHint() {
    el("#mapHint").hidden = !!state.bbox || drawing || activeMode() !== "archive" || !state.start;
    el("#clearArea").disabled = !state.bbox;
  }

  // ---------- period presets ----------
  function presetRange(name) {
    if (!state.minDate) return null;
    var y = +state.maxDate.slice(0, 4);
    var clip = function (d) { return d < state.minDate ? state.minDate : d > state.maxDate ? state.maxDate : d; };
    var ranges = {
      all: [state.minDate, state.maxDate],
      last5: [(y - 4) + "-01-01", state.maxDate],
      last: [y + "-01-01", state.maxDate],
      spring: [y + "-03-01", y + "-05-31"],
    };
    var r = ranges[name];
    return r && [clip(r[0]), clip(r[1])];
  }
  function syncPresets() {
    var y = state.maxDate ? state.maxDate.slice(0, 4) : "";
    el("#presetLast").textContent = y;
    el("#presetSpring").textContent = t("preset_spring", { y: y });
    els(".preset").forEach(function (b) {
      var r = presetRange(b.dataset.preset);
      var on = !!r && r[0] === state.start && r[1] === state.end;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    });
  }
  els(".preset").forEach(function (b) {
    b.onclick = function () {
      var r = presetRange(b.dataset.preset);
      if (!r) return;
      el("#start").value = r[0];
      el("#end").value = r[1];
      changeDates();
      syncPresets();
    };
  });

  // ---------- quick start ----------
  var guide = el("#quickstart");
  function guideSeen() { try { return localStorage.getItem("firecal-guide") === "done"; } catch (e) { return false; } }
  function showGuide(show) {
    guide.hidden = !show;
    if (!show) { try { localStorage.setItem("firecal-guide", "done"); } catch (e) {} }
  }
  function flash(node) {
    node.classList.remove("flash");
    void node.offsetWidth;
    node.classList.add("flash");
  }
  el("#quickstartClose").onclick = function () { showGuide(false); };
  el("#helpButton").onclick = function () {
    showGuide(guide.hidden);
    if (!guide.hidden) guide.scrollIntoView({ block: "nearest", behavior: reduced ? "auto" : "smooth" });
  };
  els("#quickstart [data-step]").forEach(function (b) {
    b.onclick = function () {
      var step = b.dataset.step;
      if (step === "area") startDraw();
      if (step === "period") {
        if (mobileSheet.matches) toggleSheet(true, false);
        var target = el("#stepPeriod");
        target.scrollIntoView({ block: "nearest", behavior: reduced ? "auto" : "smooth" });
        flash(target);
        el(".preset").focus({ preventScroll: true });
      }
      if (step === "calendar") {
        var card = el("#calendarCard");
        card.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" });
        flash(card);
      }
    };
  });
  if (!guideSeen()) guide.hidden = false;

  // ---------- calendar: years × months ----------
  var calView = "matrix", matrixMode = "count";
  var matrix = el("#calMatrix");

  function median(values) {
    if (!values.length) return null;
    var s = values.slice().sort(function (a, b) { return a - b; }), m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }
  function countLevel(v, q) {
    if (!v) return 0;
    return v <= q[0] ? 1 : v <= q[1] ? 2 : v <= q[2] ? 3 : 4;
  }
  function ratioLevel(v, med) {
    if (med === null) return null;
    if (!med) return v > 0 ? 4 : 2;
    var r = v / med;
    return r < 0.5 ? 0 : r < 0.8 ? 1 : r <= 1.25 ? 2 : r <= 2 ? 3 : 4;
  }
  function inSelection(y, m) {
    if (!state.start || state.start === state.minDate && state.end === state.maxDate) return false;
    var first = y + "-" + String(m + 1).padStart(2, "0") + "-01";
    var last = new Date(Date.UTC(y, m + 1, 0)).toISOString().slice(0, 10);
    return first <= state.end && last >= state.start && daysBetween(state.start, state.end) <= 400;
  }

  function renderMatrix() {
    var d = state.season;
    matrix.replaceChildren();
    matrix.classList.toggle("skeleton", !d);
    if (!d) return;
    if (!d.years || !Object.keys(d.years).length) {
      var empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = t("k_none");
      matrix.append(empty);
      return;
    }
    var years = Object.keys(d.years).sort(), months = t("mon");
    var positive = [];
    years.forEach(function (y) { d.years[y].forEach(function (v) { if (v > 0) positive.push(v); }); });
    positive.sort(function (a, b) { return a - b; });
    var q = [0.4, 0.7, 0.9].map(function (p) { return positive[Math.floor(p * (positive.length - 1))] || 0; });
    var medians = months.map(function (_, m) {
      return years.map(function (y) {
        var others = years.filter(function (o) { return o !== y && d.years[o][m] !== null; })
          .map(function (o) { return d.years[o][m]; });
        return others.length >= 3 ? median(others) : null;
      });
    });
    var totals = years.map(function (y) {
      return d.years[y].reduce(function (s, v) { return s + (v || 0); }, 0);
    });
    var maxTotal = Math.max.apply(null, totals.concat([1]));

    var head = document.createElement("div");
    head.className = "mx-row mx-head";
    head.setAttribute("role", "row");
    head.append(document.createElement("span"));
    months.forEach(function (m) {
      var s = document.createElement("span");
      s.textContent = m;
      s.setAttribute("role", "columnheader");
      head.append(s);
    });
    var tot = document.createElement("span");
    tot.textContent = t("mx_total");
    tot.className = "mx-total-head";
    head.append(tot);
    matrix.append(head);

    years.forEach(function (y, yi) {
      var row = document.createElement("div");
      row.className = "mx-row";
      row.setAttribute("role", "row");
      row.style.setProperty("--i", yi);
      var label = document.createElement("span");
      label.className = "mx-year";
      label.setAttribute("role", "rowheader");
      label.textContent = y;
      row.append(label);
      d.years[y].forEach(function (v, m) {
        var cell = document.createElement("button");
        cell.className = "mx-cell";
        cell.tabIndex = -1;
        cell.setAttribute("role", "gridcell");
        var med = medians[m][yi];
        if (v === null) {
          cell.classList.add("mx-empty");
          cell.disabled = true;
          cell.setAttribute("aria-label", t("mx_none", { m: months[m], y: y }));
        } else {
          var level = matrixMode === "anomaly" ? ratioLevel(v, med) : countLevel(v, q);
          cell.classList.add(level === null ? "mx-na" : (matrixMode === "anomaly" ? "d" : "c") + level);
          var text = t("mx_tip", { m: months[m], y: y, v: fmt(Math.round(v)) });
          if (med) text += t("mx_ratio", { r: (v / med).toLocaleString(LANG, { maximumFractionDigits: 1 }) });
          cell.dataset.tip = text;
          cell.setAttribute("aria-label", text);
          cell.dataset.month = y + "-" + String(m + 1).padStart(2, "0") + "-01";
          if (inSelection(+y, m)) cell.classList.add("mx-selected");
        }
        row.append(cell);
      });
      var bar = document.createElement("span");
      bar.className = "mx-total";
      bar.title = fmt(Math.round(totals[yi]));
      var fill = document.createElement("i");
      fill.style.width = (totals[yi] / maxTotal * 100).toFixed(1) + "%";
      bar.append(fill);
      row.append(bar);
      matrix.append(row);
    });
    var first = matrix.querySelector(".mx-selected") || matrix.querySelector(".mx-cell:not(:disabled)");
    if (first) first.tabIndex = 0;
  }

  matrix.addEventListener("click", function (e) {
    var cell = e.target.closest(".mx-cell");
    if (!cell || cell.disabled) return;
    setMonth(cell.dataset.month);
  });
  matrix.addEventListener("mousemove", function (e) {
    var cell = e.target.closest(".mx-cell");
    if (!cell || !cell.dataset.tip) { tip.style.display = "none"; return; }
    tip.textContent = cell.dataset.tip;
    tip.style.display = "block";
    tip.style.left = Math.min(e.clientX + 12, innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = Math.min(e.clientY + 12, innerHeight - tip.offsetHeight - 8) + "px";
  });
  matrix.addEventListener("mouseleave", function () { tip.style.display = "none"; });
  matrix.addEventListener("keydown", function (e) {
    var cells = els("#calMatrix .mx-cell"), i = cells.indexOf(document.activeElement);
    if (i < 0) return;
    var step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -12, ArrowDown: 12 }[e.key];
    if (!step) return;
    e.preventDefault();
    var j = i + step;
    while (cells[j] && cells[j].disabled) j += step;
    if (!cells[j]) return;
    cells[i].tabIndex = -1;
    cells[j].tabIndex = 0;
    cells[j].focus();
  });

  function renderScale() {
    var box = el("#calScale");
    box.replaceChildren();
    var add = function (text) { var s = document.createElement("span"); s.textContent = text; box.append(s); };
    var swatch = function (cls) { var i = document.createElement("i"); i.className = cls; box.append(i); };
    if (calView === "matrix" && matrixMode === "anomaly") {
      add(t("below_norm"));
      ["d0", "d1", "d2", "d3", "d4"].forEach(swatch);
      add(t("above_norm"));
    } else {
      add(t("less"));
      ["c0", "c1", "c2", "c3", "c4"].forEach(swatch);
      add(calView === "daily" ? t("more_anom") : t("more"));
    }
  }
  function setHint() {
    var key = calView === "daily" ? "cal_hint_daily" : matrixMode === "anomaly" ? "cal_hint_anomaly" : "cal_hint_matrix";
    var hint = el("#calHint");
    hint.setAttribute("data-i18n", key);
    hint.textContent = t(key);
  }
  function setCalView(view) {
    calView = view;
    els("[data-calview]").forEach(function (b) {
      b.classList.toggle("on", b.dataset.calview === view);
      b.setAttribute("aria-pressed", String(b.dataset.calview === view));
    });
    matrix.hidden = view !== "matrix";
    el("#matrixModes").hidden = view !== "matrix";
    el(".calwrap").hidden = el("#year").hidden = view !== "daily";
    if (view === "daily") drawCalendar(); else renderMatrix();
    renderScale();
    setHint();
  }
  els("[data-calview]").forEach(function (b) { b.onclick = function () { setCalView(b.dataset.calview); }; });
  els("[data-matrixmode]").forEach(function (b) {
    b.onclick = function () {
      matrixMode = b.dataset.matrixmode;
      els("[data-matrixmode]").forEach(function (o) {
        o.classList.toggle("on", o === b);
        o.setAttribute("aria-pressed", String(o === b));
      });
      renderMatrix();
      renderScale();
      setHint();
    };
  });

  // ---------- landing statistics ----------
  function landingStats() {
    api("/api/meta", "").then(function (meta) {
      var r = meta.range, box = el("#landingStats");
      var years = Math.round((parse(iso(r.max_date)) - parse(iso(r.min_date))) / (365.25 * 864e5));
      var items = [[years, "stat_years"], [r.cells, "stat_cells"], [2, "stat_sensors"]];
      box.replaceChildren();
      items.forEach(function (item) {
        var li = document.createElement("li"), b = document.createElement("b"), s = document.createElement("span");
        s.setAttribute("data-i18n", item[1]);
        s.textContent = t(item[1]);
        li.append(b, s);
        box.append(li);
        countTo(b, item[0]);
      });
    }).catch(function () {});
  }

  // ---------- hooks into app.js ----------
  var baseOpen = openAtlas;
  openAtlas = function (focus) {
    baseOpen(focus);
    syncModes();
  };
  var baseRefresh = refreshLanguage;
  refreshLanguage = function () {
    baseRefresh();
    syncPresets();
    syncModes();
    if (calView === "matrix") renderMatrix();
    renderScale();
    setHint();
  };
  document.addEventListener("firecal:loaded", function () {
    syncPresets();
    syncHint();
    if (calView === "matrix") renderMatrix();
  });
  setCalView("matrix");
  syncModes();
  landingStats();
})();
