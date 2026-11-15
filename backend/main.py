"""FireCal backend v2 (FastAPI + DuckDB).

Таблица fires: cell, date, lat, lon, frp, conf (1-3), n_points, sensors,
               n_modis, n_viirs  (одна строка = ячейка H3 за одни сутки).
Таблица calib_moy (месяц года -> коэффициент VIIRS/MODIS) делает calibrate.py.

Запуск из КОРНЯ проекта:
    uvicorn backend.main:app --reload
Документация API:  http://127.0.0.1:8000/docs
"""
import json
import math
import os
import tempfile
from datetime import date as Date
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Optional

import duckdb
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "firecal.duckdb"
LAND_COVER_PATH = ROOT / "data" / "fire_land_cover.parquet"
FORECAST_DIR = ROOT / "data" / "forecast"
FORECAST_PATH = FORECAST_DIR / "predictions.npz"
FORECAST_METRICS = FORECAST_DIR / "metrics.json"
FRONTEND = ROOT / "frontend"
OVERLAP_START = pd.Timestamp("2012-02-01")  # с этой даты работают оба прибора

DB_EXPORT = ROOT / "data" / "db"


def ensure_database():
    """Build data/firecal.duckdb from the Parquet copy in data/db/ (first start after git clone)."""
    if DB_PATH.exists() or not (DB_EXPORT / "fires.parquet").is_file():
        return
    building = DB_PATH.with_suffix(".building")
    for stale in (building, building.with_suffix(".building.wal")):
        stale.unlink(missing_ok=True)
    with duckdb.connect(str(building)) as con:
        for path in sorted(DB_EXPORT.glob("*.parquet")):
            if path.stem.isidentifier():
                con.execute(f'CREATE TABLE "{path.stem}" AS SELECT * FROM read_parquet(?)', [str(path)])
    building.replace(DB_PATH)


ensure_database()

app = FastAPI(title="FireCal API", version="0.2")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
# JSON, JS and CSS shrink 5–10× with gzip, which matters behind slow tunnels and hosts.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


@app.middleware("http")
async def cache_static(request, call_next):
    """Let browsers keep libraries, fonts and images for a day instead of re-asking each visit."""
    response = await call_next(request)
    if request.url.path.startswith(("/vendor/", "/assets/")):
        response.headers["Cache-Control"] = "public, max-age=86400"
    return response

_M = "n_modis > 0"
_V = "n_viirs > 0"
SEEN_BY = (
    f"CASE WHEN {_M} AND {_V} THEN 'both' WHEN {_V} THEN 'VIIRS' ELSE 'MODIS' END"
)
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
          "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
CAVEAT = (
    "Корреляция не равна причинности. Разница плотности пожаров рядом с районами "
    "и вдали от них может объясняться ландшафтом (тайга и степь горят по-разному), "
    "сельхозпалами, населёнными пунктами и грозами, а не падением ступеней. "
    "Круги ориентировочные, даты запусков в анализ не входят."
)

MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
METHOD_RU = "Ячейки-сутки с пожаром по VIIRS на 1000 км2: внутри круга и в кольце от 2R до 4R."
METHOD_EN = "VIIRS fire cell-days per 1000 km2: inside the circle vs in the ring from 2R to 4R."
CAVEAT_EN = (
    "Correlation is not causation. A difference in fire density near these areas and away "
    "from them can be explained by terrain (taiga and steppe burn differently), agricultural "
    "burning, settlements and lightning rather than by falling rocket stages. "
    "The circles are approximate and launch dates are not used in the analysis."
)


# ---------- служебное ----------
# On small hosts (e.g. 512 MB containers) cap DuckDB so large scans spill to disk instead of
# exhausting memory: FIRECAL_DUCKDB_MEMORY=256MB FIRECAL_DUCKDB_THREADS=1
DUCKDB_MEMORY = os.environ.get("FIRECAL_DUCKDB_MEMORY")
DUCKDB_THREADS = os.environ.get("FIRECAL_DUCKDB_THREADS")


def connect():
    if not DB_PATH.exists():
        raise HTTPException(500, f"База не найдена: {DB_PATH}")
    config = {}
    if DUCKDB_MEMORY:
        config["memory_limit"] = DUCKDB_MEMORY
        config["temp_directory"] = tempfile.gettempdir()
    if DUCKDB_THREADS:
        config["threads"] = int(DUCKDB_THREADS)
    return duckdb.connect(str(DB_PATH), read_only=True, config=config)


def prepare(con, sql: str):
    if "classified_fires" not in sql:
        return
    if LAND_COVER_PATH.is_file():
        con.register("land_cover", con.read_parquet(str(LAND_COVER_PATH)))
        con.execute("""CREATE TEMP VIEW classified_fires AS
            SELECT f.*, coalesce(l.land_type, 'unknown') AS land_type
            FROM fires f LEFT JOIN land_cover l USING (cell)""")
    else:
        con.execute("""CREATE TEMP VIEW classified_fires AS
            SELECT *, 'unknown' AS land_type FROM fires""")


def run(sql: str, params: Optional[list] = None) -> pd.DataFrame:
    con = connect()
    try:
        prepare(con, sql)
        return con.execute(sql, params or []).fetchdf()
    finally:
        con.close()


LAND_TYPES = {"forest": ("forest",), "cropland": ("cropland",), "both": ("forest", "cropland")}


def where(start=None, end=None, bbox=None, min_conf=1, sensor=None, land=None):
    """Собирает WHERE. bbox = 'west,south,east,north'."""
    conds, params = ["1=1"], []
    if start:
        conds.append("date >= CAST(? AS DATE)")
        params.append(start)
    if end:
        conds.append("date <= CAST(? AS DATE)")
        params.append(end)
    if bbox:
        try:
            w, s, e, n = [float(x) for x in bbox.split(",")]
        except ValueError:
            raise HTTPException(400, "bbox: нужно 'west,south,east,north'")
        conds.append("lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?")
        params += [w, e, s, n]
    if min_conf > 1:
        conds.append("conf >= ?")
        params.append(min_conf)
    if sensor == "modis":
        conds.append(_M)
    elif sensor == "viirs":
        conds.append(_V)
    elif sensor == "both":
        conds.append(f"({_M} AND {_V})")
    if land is not None:
        if land not in LAND_TYPES:
            raise HTTPException(422, "land: forest, cropland or both")
        if not LAND_COVER_PATH.is_file():
            raise HTTPException(503, "Land cover lookup is unavailable")
        values = LAND_TYPES[land]
        conds.append(f"land_type IN ({', '.join('?' for _ in values)})")
        params.extend(values)
    return " AND ".join(conds), params


def fire_source(land):
    return "classified_fires" if land is not None else "fires"


def to_records(df: pd.DataFrame):
    df = df.astype(object).where(pd.notnull(df), None)
    for c in df.columns:
        if str(df[c].dtype) == "object":
            df[c] = df[c].map(lambda v: v.isoformat() if hasattr(v, "isoformat") else v)
    return df.to_dict(orient="records")


def k_by_month():
    """Коэффициенты VIIRS/MODIS по месяцам года из calib_moy."""
    try:
        df = run("SELECT moy, k FROM calib_moy")
    except Exception:
        return {}, 2.2
    ks = {int(r.moy): float(r.k) for r in df.itertuples()}
    default = float(df["k"].median()) if len(df) else 2.2
    return ks, default


def daily(w, p, normalize=False, land=None):
    """Сутки: число ячеек с пожаром. normalize = в шкале VIIRS.

    До 2012-02 был только MODIS: его ячейки умножаем на коэффициент месяца.
    После - считаем ячейки, где пожар видел VIIRS."""
    df = run(
        f"""SELECT date,
               count(*) AS any_cells,
               count(*) FILTER (WHERE n_modis > 0) AS modis_cells,
               count(*) FILTER (WHERE n_viirs > 0) AS viirs_cells,
               count(*) FILTER (WHERE n_modis > 0 AND n_viirs > 0) AS both_cells,
               sum(n_points) AS points,
               round(sum(frp), 1) AS frp
            FROM {fire_source(land)} WHERE {w} GROUP BY date ORDER BY date""",
        p,
    )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    if normalize:
        ks, dk = k_by_month()
        kk = df["date"].dt.month.map(lambda m: ks.get(int(m), dk))
        df["cells"] = (df["modis_cells"] * kk).where(df["date"] < OVERLAP_START, df["viirs_cells"])
    else:
        df["cells"] = df["any_cells"]
    return df


def full_series(df, start=None, end=None):
    """Ряд по дням, пустые дни = 0 (иначе норма завышена)."""
    s = df.set_index("date")["cells"].astype(float)
    return s.reindex(pd.date_range(start if start is not None else s.index.min(),
                                  end if end is not None else s.index.max(), freq="D"), fill_value=0.0)


def seasonal_z(s):
    """z-score относительно того же календарного месяца в другие годы."""
    months = s.index.month
    years = s.index.year

    def other_years(values):
        return values.groupby(months).transform("sum") - values.groupby([months, years]).transform("sum")

    count = other_years(pd.Series(1, index=s.index)).replace(0, float("nan"))
    mean = other_years(s) / count
    variance = other_years(s.pow(2)) / count - mean.pow(2)
    std = variance.clip(lower=0).pow(0.5)
    year_count = pd.Series(years, index=s.index).groupby(months).transform("nunique")
    z = ((s - mean) / std.where(std > 0)).where(year_count >= 4)
    return z, mean, std


def load_zones():
    f = FRONTEND / "zones.json"
    if not f.exists():
        return {"note": "", "zones": []}
    return json.loads(f.read_text(encoding="utf-8"))


def num(x):
    return f"{x:,.0f}".replace(",", " ")


# ---------- эндпоинты ----------
@app.get("/api/meta")
def meta():
    rng = run(
        "SELECT min(date) AS min_date, max(date) AS max_date, count(*) AS cells, "
        "min(lat) AS south, max(lat) AS north, min(lon) AS west, max(lon) AS east "
        "FROM fires"
    )
    by = run(f"SELECT {SEEN_BY} AS seen_by, count(*) AS cells FROM fires GROUP BY 1")
    try:
        run("SELECT 1 FROM calib_moy LIMIT 1")
        calibrated = True
    except Exception:
        calibrated = False
    return {"range": to_records(rng)[0], "seen_by": to_records(by), "calibrated": calibrated,
            "land_cover": LAND_COVER_PATH.is_file()}


@app.get("/api/fires")
def fires(
    start: Optional[str] = None,
    end: Optional[str] = None,
    bbox: Optional[str] = None,
    min_conf: int = Query(1, ge=1, le=3),
    sensor: Optional[str] = Query(None, pattern="^(modis|viirs|both)$"),
    limit: int = Query(20000, le=100000),
    omit_leap_day: bool = False,
    land: Optional[str] = None,
):
    """Ячейки H3 за период. Для таймлапса: start=end=нужный день."""
    w, p = where(start, end, bbox, min_conf, sensor, land)
    if omit_leap_day:
        w += " AND NOT (month(date) = 2 AND day(date) = 29)"
    sql = f"""
        SELECT cell, avg(lat) AS lat, avg(lon) AS lon,
               count(DISTINCT date) AS days, sum(n_points) AS points,
               round(sum(frp), 1) AS frp, max(conf) AS conf, min(land_type) AS land_type,
               CASE WHEN max(n_modis) > 0 AND max(n_viirs) > 0 THEN 'both'
                    WHEN max(n_viirs) > 0 THEN 'VIIRS' ELSE 'MODIS' END AS seen_by
        FROM classified_fires WHERE {w}
        GROUP BY cell ORDER BY frp DESC LIMIT {int(limit)}
    """
    return to_records(run(sql, p))


@app.get("/api/calendar")
def calendar(
    year: Optional[int] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    bbox: Optional[str] = None,
    min_conf: int = Query(1, ge=1, le=3),
    normalize: int = Query(0, ge=0, le=1),
    land: Optional[str] = None,
):
    """Календарь-heatmap: по дням число ячеек с пожарами."""
    if year:
        start, end = f"{year}-01-01", f"{year}-12-31"
    w, p = where(start, end, bbox, min_conf, land=land)
    df = daily(w, p, bool(normalize), land)
    out = []
    for r in df.itertuples():
        out.append({
            "date": r.date.strftime("%Y-%m-%d"),
            "cells": round(float(r.cells), 1),
            "points": int(r.points),
            "frp": float(r.frp),
            "confirmed_both": int(r.both_cells),
        })
    return out


@app.get("/api/anomalies")
def anomalies(
    level: str = Query("day", pattern="^(day|month)$"),
    z: float = 2.0,
    start: Optional[str] = None,
    end: Optional[str] = None,
    bbox: Optional[str] = None,
    min_conf: int = Query(1, ge=1, le=3),
    normalize: int = Query(0, ge=0, le=1),
    land: Optional[str] = None,
):
    """Дни/месяцы, где пожаров сильно больше нормы ТОГО ЖЕ календарного месяца
    в другие годы (z-score). Лето не считается аномалией само по себе."""
    w, p = where(None, None, bbox, min_conf, land=land)
    df = daily(w, p, bool(normalize), land)
    base = "тот же календарный месяц в другие годы"
    if df.empty:
        return {"baseline": base, "mean": None, "std": None, "years": 0,
                "threshold_z": z, "items": []}
    archive = run("SELECT min(date) AS a, max(date) AS b FROM fires").iloc[0]
    s = full_series(df, archive.a, archive.b)
    selected_start = pd.Timestamp(start) if start else s.index.min()
    selected_end = pd.Timestamp(end) if end else s.index.max()
    if level == "month":
        monthly = s.resample("MS")
        sums = monthly.sum()
        s = sums[monthly.size() == sums.index.days_in_month]
    zs, mean, std = seasonal_z(s)
    selected = (s.index >= selected_start) & (s.index <= selected_end)
    if level == "month":
        selected &= s.index + pd.offsets.MonthEnd() <= selected_end
    flagged = s.index[selected & (zs >= z).values]
    items = [
        {"date": d.strftime("%Y-%m-%d"), "n": round(float(s[d]), 1),
         "z": round(float(zs[d]), 2), "expected": round(float(mean[d]), 1)}
        for d in flagged
    ]
    items.sort(key=lambda it: it["z"], reverse=True)
    return {
        "level": level, "baseline": base,
        "mean": round(float(s.mean()), 2), "std": round(float(s.std(ddof=0)), 2),
        "years": int(s.index.year.nunique()), "threshold_z": z, "items": items,
    }


@app.get("/api/seasons")
def seasons(
    start: Optional[str] = None,
    end: Optional[str] = None,
    bbox: Optional[str] = None,
    min_conf: int = Query(1, ge=1, le=3),
    normalize: int = Query(0, ge=0, le=1),
    land: Optional[str] = None,
):
    """Суммы по месяцам для каждого года и среднее по годам (график сезона)."""
    w, p = where(start, end, bbox, min_conf, land=land)
    df = daily(w, p, bool(normalize), land)
    if df.empty:
        return {"years": {}, "mean": [None] * 12}
    m = full_series(df).resample("MS").sum()
    pv = (pd.DataFrame({"y": m.index.year, "m": m.index.month, "v": m.values})
          .pivot(index="y", columns="m", values="v").reindex(columns=range(1, 13)))

    def clean(v):
        return None if pd.isna(v) else round(float(v), 1)

    return {
        "years": {str(int(y)): [clean(v) for v in row] for y, row in pv.iterrows()},
        "mean": [clean(v) for v in pv.mean()],
    }


@app.get("/api/top")
def top(
    start: Optional[str] = None,
    end: Optional[str] = None,
    bbox: Optional[str] = None,
    min_conf: int = Query(1, ge=1, le=3),
    n: int = Query(10, ge=1, le=50),
    land: Optional[str] = None,
):
    """Самые «горячие» ячейки: больше всего дней с пожаром."""
    w, p = where(start, end, bbox, min_conf, land=land)
    df = run(
        f"""SELECT cell, avg(lat) AS lat, avg(lon) AS lon,
                   count(DISTINCT date) AS days, round(sum(frp), 1) AS frp
            FROM {fire_source(land)} WHERE {w} GROUP BY cell
            ORDER BY days DESC, frp DESC LIMIT {int(n)}""",
        p,
    )
    return to_records(df)


@app.get("/api/kpis")
def kpis(
    start: Optional[str] = None,
    end: Optional[str] = None,
    bbox: Optional[str] = None,
    min_conf: int = Query(1, ge=1, le=3),
    normalize: int = Query(0, ge=0, le=1),
    land: Optional[str] = None,
):
    """Ключевые показатели периода и сравнение с тем же периодом в другие годы."""
    w, p = where(None, None, bbox, min_conf, land=land)
    df = daily(w, p, bool(normalize), land)
    if df.empty:
        return {"empty": True}
    rng = run("SELECT min(date) AS a, max(date) AS b FROM fires")
    gmin, gmax = pd.Timestamp(rng["a"].iloc[0]), pd.Timestamp(rng["b"].iloc[0])
    idx = pd.date_range(gmin, gmax, freq="D")
    d = df.set_index("date")
    cells = d["cells"].astype(float).reindex(idx, fill_value=0.0)
    points = d["points"].astype(float).reindex(idx, fill_value=0.0)
    t0 = max(pd.Timestamp(start) if start else gmin, gmin)
    t1 = min(pd.Timestamp(end) if end else gmax, gmax)
    if t1 < t0:
        return {"empty": True}
    cur = cells[t0:t1]
    out = {
        "empty": False,
        "window": {"start": t0.strftime("%Y-%m-%d"), "end": t1.strftime("%Y-%m-%d"),
                   "days": int((t1 - t0).days + 1)},
        "current": {"cells": round(float(cur.sum()), 1), "points": int(points[t0:t1].sum()),
                    "active_days": int((cur > 0).sum())},
        "current_year": int(t0.year), "history": [], "mean": None, "std": None,
        "median": None, "pct_vs_median": None, "pct_vs_mean": None,
        "z": None, "rank": None, "n_years": 0, "n_other": 0,
    }
    if out["window"]["days"] > 366:  # длинный период: только суммы по годам
        out["mode"] = "long"
        yearly = cur.groupby(cur.index.year).sum()
        out["history"] = [{"year": int(y), "cells": round(float(v), 1)} for y, v in yearly.items()]
        return out
    out["mode"] = "window"
    hist = []
    for y in range(gmin.year - 1, gmax.year + 2):
        off = pd.DateOffset(years=int(y - t0.year))
        ys, ye = t0 + off, t1 + off
        if ys >= gmin and ye <= gmax:  # окно целиком внутри данных
            hist.append({"year": int(y), "cells": round(float(cells[ys:ye].sum()), 1)})
    out["history"] = hist
    others = [h["cells"] for h in hist if h["year"] != t0.year]
    out["n_years"], out["n_other"] = len(hist), len(others)
    if others:
        mean = sum(others) / len(others)
        typical = median(others)
        out["median"] = round(typical, 1)
        if typical > 0:
            out["pct_vs_median"] = int(round((out["current"]["cells"] - typical) / typical * 100))
        out["mean"] = round(mean, 1)
        if mean > 0:
            out["pct_vs_mean"] = int(round((out["current"]["cells"] - mean) / mean * 100))
        if len(others) >= 3:
            std = (sum((v - mean) ** 2 for v in others) / len(others)) ** 0.5
            out["std"] = round(std, 1)
            if std > 0:
                out["z"] = round((out["current"]["cells"] - mean) / std, 2)
        out["rank"] = 1 + sum(1 for v in others if v > out["current"]["cells"])
    return out


@app.get("/api/zones")
def zones():
    return load_zones()


@app.get("/api/stages")
def stages(
    start: Optional[str] = None,
    end: Optional[str] = None,
    min_conf: int = Query(1, ge=1, le=3),
    lang: str = "ru",
    land: Optional[str] = None,
):
    """Плотность пожаров внутри круга района и в кольце вокруг него (2R..4R).

    Берём только VIIRS и только с 2012-02, чтобы прибор не менялся."""
    en = lang == "en"
    data = load_zones()
    w, p = where(start, end, None, min_conf, land=land)
    w += f" AND n_viirs > 0 AND date >= DATE '{OVERLAP_START.strftime('%Y-%m-%d')}'"
    out = []
    for z in data.get("zones", []):
        lat0, lon0, r = float(z["lat"]), float(z["lon"]), float(z["radius_km"])
        cosl = math.cos(math.radians(lat0))
        dist = f"(111.32 * sqrt(pow(lat - {lat0:.6f}, 2) + pow((lon - {lon0:.6f}) * {cosl:.6f}, 2)))"
        df = run(
            f"""SELECT y,
                   count(*) FILTER (WHERE d <= {r:.3f}) AS inside,
                   count(*) FILTER (WHERE d > {2 * r:.3f}) AS ring
                FROM (SELECT year(date) AS y, {dist} AS d FROM {fire_source(land)} WHERE {w})
                WHERE d <= {4 * r:.3f} GROUP BY y ORDER BY y""",
            p,
        )
        a_in, a_ring = math.pi * r * r, 12 * math.pi * r * r  # км2
        inside = int(df["inside"].sum()) if len(df) else 0
        ring = int(df["ring"].sum()) if len(df) else 0
        d_in, d_ring = inside / a_in * 1000, ring / a_ring * 1000
        by_year = []
        for row in df.itertuples():
            di, dr = row.inside / a_in, row.ring / a_ring
            by_year.append({"year": int(row.y), "ratio": round(di / dr, 2) if dr > 0 else None})

        def pick(key, default=""):
            if en and z.get(key + "_en"):
                return z[key + "_en"]
            return z.get(key, default)

        out.append({
            "id": pick("id"), "name": pick("name", z["id"]), "lat": lat0, "lon": lon0,
            "radius_km": r, "approx": bool(z.get("approx", True)),
            "source": pick("source"), "note": pick("note"),
            "inside_cells": inside, "ring_cells": ring,
            "inside_density": round(d_in, 2), "ring_density": round(d_ring, 2),
            "ratio": round(d_in / d_ring, 2) if ring > 0 else None,
            "by_year": by_year,
        })
    return {
        "method": METHOD_EN if en else METHOD_RU,
        "caveat": CAVEAT_EN if en else CAVEAT,
        "note": (data.get("note_en") if en else data.get("note")) or "",
        "zones": out,
    }


@app.get("/api/ask")
def ask(
    q: str = "",
    start: Optional[str] = None,
    end: Optional[str] = None,
    bbox: Optional[str] = None,
    min_conf: int = Query(1, ge=1, le=3),
    normalize: int = Query(0, ge=0, le=1),
    lang: str = "ru",
    land: Optional[str] = None,
):
    """Помощник: отвечает по шаблонам на основе данных выбранной области.
    Это не нейросеть: каждый ответ собран из чисел в базе.
    lang = ru или en (язык ответа). Ключевые слова понимаются на обоих языках."""
    en = lang == "en"

    def T(ru, eng):
        return eng if en else ru

    months = MONTHS_EN if en else MONTHS
    ql = q.lower()
    norm = bool(normalize)

    def has(*words):
        return any(wd in ql for wd in words)

    if has("помощ", "умеешь", "что можешь", "help"):
        return {"intent": "help", "answer": T(
            "Я отвечаю по данным выбранной области и периода: общая картина, "
            "пик сезона, аномалии, сравнение с прошлым годом, надёжность "
            "(MODIS и VIIRS) и сравнение пожаров рядом с районами падения ступеней.",
            "I answer from the data of the selected area and period: overview, "
            "season peak, anomalies, comparison with last year, reliability "
            "(MODIS vs VIIRS) and fire density near rocket-stage drop areas.")}

    if has("ступен", "ракет", "байконур", "падени", "stage", "rocket", "drop", "baikonur"):
        res = stages(start=start, end=end, min_conf=min_conf, lang=lang, land=land)
        if not res["zones"]:
            return {"intent": "stages", "answer": T("Список районов пуст (файл frontend/zones.json).",
                                                   "The list of areas is empty (frontend/zones.json).")}
        lines = []
        for z in res["zones"]:
            if z["ratio"] is None:
                lines.append(T(f"{z['id']}: недостаточно данных для сравнения.",
                               f"{z['id']}: not enough data to compare."))
            else:
                lines.append(T(f"{z['id']}: плотность пожаров внутри круга относится к кольцу как {z['ratio']}.",
                               f"{z['id']}: fire density inside the circle vs the ring is {z['ratio']}."))
        return {"intent": "stages", "answer": " ".join(lines) + " " + res["caveat"]}

    w, p = where(start, end, bbox, min_conf, land=land)
    df = daily(w, p, norm, land)
    if df.empty:
        return {"intent": "empty", "answer": T(
            "В выбранной области и периоде данных нет. Расширь область или даты.",
            "No data for the selected area and period. Widen the area or the dates.")}
    s = full_series(df)
    d0, d1 = s.index.min(), s.index.max()
    total = float(s.sum())
    scale = T(" (в шкале VIIRS)", " (VIIRS scale)") if norm else ""

    if has("аномал", "всплеск", "необычн", "выброс", "anomal", "unusual", "spike", "outlier"):
        comparison = anomalies("day", 2, start, end, bbox, min_conf, normalize, land)
        if comparison["years"] < 4:
            return {"intent": "anomalies", "answer": T(
                "Для сравнения с сезонной нормой нужны данные хотя бы за три других года.",
                "The seasonal baseline needs data from at least three other years.")}
        top3 = comparison["items"][:3]
        if not top3:
            return {"intent": "anomalies", "answer": T(
                "Дней с z-score от 2 и выше для этого периода и области нет.",
                "No days with z-score of 2 or higher for this period and area.")}
        parts = [T(f"{row['date']}: {row['n']:.0f} ячеек при норме {row['expected']:.0f} (z = {row['z']:.1f})",
                   f"{row['date']}: {row['n']:.0f} cells vs norm {row['expected']:.0f} (z = {row['z']:.1f})")
                 for row in top3]
        return {"intent": "anomalies", "answer": T("Самые сильные аномальные дни", "Strongest anomalous days")
                + scale + ": " + "; ".join(parts) + "."}

    if has("прошл", "сравн", "динамик", "тренд", "last year", "compar", "trend", "previous"):
        if (d1 - d0).days > 370:
            return {"intent": "compare", "answer": T(
                "Сравнение год к году удобно для периода до года. Выбери даты одного сезона.",
                "Year-over-year comparison works best for periods up to a year. Pick the dates of one season.")}
        ps = (d0 - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
        pe = (d1 - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
        w2, p2 = where(ps, pe, bbox, min_conf, land=land)
        df2 = daily(w2, p2, norm, land)
        if df2.empty:
            return {"intent": "compare", "answer": T("За тот же период годом раньше данных нет.",
                                                    "No data for the same period a year earlier.")}
        t2 = float(df2["cells"].sum())
        if t2 <= 0:
            return {"intent": "compare", "answer": T("Годом раньше пожаров в этой области не было.",
                                                    "There were no fires in this area a year earlier.")}
        ch = (total - t2) / t2 * 100
        return {"intent": "compare", "answer": T(
            f"{d0.strftime('%Y-%m-%d')} - {d1.strftime('%Y-%m-%d')}: {num(total)} ячеек-суток{scale}. "
            f"Годом раньше ({ps} - {pe}): {num(t2)}. Сейчас на {abs(ch):.0f}% {'больше' if ch >= 0 else 'меньше'}.",
            f"{d0.strftime('%Y-%m-%d')} - {d1.strftime('%Y-%m-%d')}: {num(total)} cell-days{scale}. "
            f"A year earlier ({ps} - {pe}): {num(t2)}. Now {abs(ch):.0f}% {'higher' if ch >= 0 else 'lower'}.")}

    if has("надёж", "надеж", "прибор", "viirs", "modis", "подтвер", "достовер",
           "reliab", "confirm", "instrument", "sensor", "trust"):
        mod, vi, both = float(df["modis_cells"].sum()), float(df["viirs_cells"].sum()), float(df["both_cells"].sum())
        if mod <= 0:
            return {"intent": "reliability", "answer": T(
                "В этом периоде MODIS пожаров не видел, подтверждение двумя приборами посчитать нельзя.",
                "MODIS saw no fires in this period, so confirmation by two instruments cannot be computed.")}
        return {"intent": "reliability", "answer": T(
            f"MODIS видел {num(mod)} ячеек-суток, VIIRS {num(vi)}, оба прибора {num(both)}. "
            f"VIIRS подтвердил {both / mod * 100:.0f}% событий MODIS. Событие, замеченное двумя "
            "приборами, надёжнее.",
            f"MODIS saw {num(mod)} cell-days, VIIRS {num(vi)}, both instruments {num(both)}. "
            f"VIIRS confirmed {both / mod * 100:.0f}% of MODIS events. An event seen by both "
            "instruments is more reliable.")}

    m = s.resample("MS").sum()
    top_m = m.sort_values(ascending=False).head(3)
    if has("пик", "сезон", "когда", "больше всего", "самый", "горяч", "peak", "season", "when", "most", "hottest"):
        mm = "; ".join(f"{months[d.month - 1]} {d.year}: {num(v)}" for d, v in top_m.items())
        pk = s.idxmax()
        return {"intent": "peak", "answer": T(
            f"Самые активные месяцы{scale}: {mm}. Самый активный день: "
            f"{pk.strftime('%Y-%m-%d')} ({s[pk]:.0f} ячеек).",
            f"Most active months{scale}: {mm}. Most active day: "
            f"{pk.strftime('%Y-%m-%d')} ({s[pk]:.0f} cells).")}

    pk = s.idxmax()
    return {"intent": "summary", "answer": T(
        f"{d0.strftime('%Y-%m-%d')} - {d1.strftime('%Y-%m-%d')}: {num(total)} ячеек-суток с пожаром{scale}, "
        f"суммарный FRP {num(float(df['frp'].sum()))} МВт, дней с пожарами {int((s > 0).sum())} из {len(s)}. "
        f"Самый активный день: {pk.strftime('%Y-%m-%d')} ({s[pk]:.0f} ячеек), месяц: "
        f"{months[top_m.index[0].month - 1]} {top_m.index[0].year}.",
        f"{d0.strftime('%Y-%m-%d')} - {d1.strftime('%Y-%m-%d')}: {num(total)} fire cell-days{scale}, "
        f"total FRP {num(float(df['frp'].sum()))} MW, {int((s > 0).sum())} of {len(s)} days with fires. "
        f"Most active day: {pk.strftime('%Y-%m-%d')} ({s[pk]:.0f} cells), month: "
        f"{months[top_m.index[0].month - 1]} {top_m.index[0].year}.")}


@app.get("/api/export.csv")
def export_csv(
    start: Optional[str] = None,
    end: Optional[str] = None,
    bbox: Optional[str] = None,
    min_conf: int = Query(1, ge=1, le=3),
    land: Optional[str] = None,
):
    w, p = where(start, end, bbox, min_conf, land=land)
    sql = (f"SELECT cell, date, lat, lon, frp, conf, n_points, {SEEN_BY} AS seen_by, land_type "
           f"FROM classified_fires WHERE {w} ORDER BY date, cell")
    con = connect()
    try:
        prepare(con, sql)
        con.execute(sql, p)
    except Exception:
        con.close()
        raise

    def rows():
        """Stream the result in chunks so a whole-archive export never sits in memory."""
        try:
            header = True
            while True:
                chunk = con.fetch_df_chunk(25)
                if chunk.empty:
                    break
                yield chunk.to_csv(index=False, header=header)
                header = False
        finally:
            con.close()

    return StreamingResponse(
        rows(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=firecal_export.csv"},
    )


@lru_cache(maxsize=1)
def read_backtest(version: int):
    """Outlook backtest written by pipeline/train_forecast.py (test years only)."""
    with np.load(FORECAST_PATH) as data:
        arrays = {key: data[key] for key in data.files}
    metrics = json.loads(FORECAST_METRICS.read_text(encoding="utf-8"))
    arrays["index"] = {d: i for i, d in enumerate(arrays["dates"].tolist())}
    return arrays, metrics


def bbox_cells(bbox: str, grid: dict):
    try:
        w, s, e, n = [float(x) for x in bbox.split(",")]
    except ValueError:
        raise HTTPException(400, "bbox: нужно 'west,south,east,north'")
    lat = grid["south"] + (np.arange(grid["rows"]) + 0.5) * grid["step"]
    lon = grid["west"] + (np.arange(grid["cols"]) + 0.5) * grid["step"]
    inside = ((lat[:, None] >= s) & (lat[:, None] <= n)) & ((lon[None, :] >= w) & (lon[None, :] <= e))
    return np.flatnonzero(inside.ravel())


@app.get("/api/fire-risk")
def fire_risk(day: Optional[Date] = None, horizon: int = 7, bbox: Optional[str] = None):
    """Hotspot outlook backtest: probability of ≥1 VIIRS hotspot per 0.25° cell.

    Without `day`: grid, available dates and evaluation metrics.
    With `day` (issue date, end of UTC day): probabilities for the next day
    (horizon=1) or the next 7 days (horizon=7), the climatology baseline and
    the detections that followed. Probabilities are integers in 1/10000.
    """
    if horizon not in (1, 7):
        raise HTTPException(422, "horizon: 1 or 7")
    try:
        arrays, metrics = read_backtest(FORECAST_PATH.stat().st_mtime_ns)
    except FileNotFoundError:
        raise HTTPException(404, "The forecast backtest is not installed") from None
    if day is None:
        dates = arrays["dates"].tolist()
        return {**metrics, "dates": [dates[0], dates[-1]],
                "holdout": [int(i) for i in arrays["holdout"].nonzero()[0]]}
    i = arrays["index"].get(day.isoformat())
    if i is None:
        raise HTTPException(404, "No outlook for that date")
    h = str(horizon)
    probability, baseline = arrays["p" + h][i], arrays["c" + h][i]
    observed, valid = arrays["y" + h][i], bool(arrays["valid" + h][i])
    start = pd.Timestamp(day) + pd.Timedelta(days=1)
    out = {
        "day": day.isoformat(), "horizon": horizon, "valid": valid,
        "window": [start.strftime("%Y-%m-%d"), (start + pd.Timedelta(days=horizon - 1)).strftime("%Y-%m-%d")],
        "probability": (probability.astype("int64") * 10000 // 65535).tolist(),
        "climatology": (baseline.astype("int64") * 10000 // 65535).tolist(),
        "observed": [int(c) for c in observed.nonzero()[0]] if valid else None,
    }
    if bbox:
        cells = bbox_cells(bbox, metrics["grid"])
        out["area"] = {
            "cells": int(len(cells)),
            "expected": round(float(probability[cells].sum()) / 65535, 1),
            "normal": round(float(baseline[cells].sum()) / 65535, 1),
            "observed": int(observed[cells].sum()) if valid else None,
        }
    return out


# Фронтенд раздаём с того же сервера: http://127.0.0.1:8000/
# (монтируем последним, чтобы не перекрыть /api)
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
