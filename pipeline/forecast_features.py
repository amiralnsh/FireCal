# AI-assisted: developed with help from the Claude AI assistant (Anthropic); see README, 'Use of AI tools'.
"""Build daily feature grids and targets for the FireCal hotspot outlook.

For every issue date t (end of UTC day t) and every 0.25° cell the script writes
the features available at that time plus weather for t+1..t+7, and two targets:
  y1  at least one VIIRS hotspot in the cell on day t+1
  y7  at least one VIIRS hotspot in the cell during days t+1..t+7

Run after pipeline/forecast_weather.py:
    python pipeline/forecast_features.py

Outputs in data/forecast/:
  features.npy  float16 memmap (issue days, features, cells)
  targets.npz   y1, y7 (uint8, issue days × cells), valid1, valid7, dates
  grid.npz      feature names and groups, hold-out mask, land fractions
"""

import duckdb
import numpy as np
import pandas as pd
from scipy.ndimage import uniform_filter

import fwi
from forecast_common import (
    CLIM_HARMONIZED_YEARS, CLIM_VIIRS_YEARS, DB, LAND_COVER, NCELL, NCOL, NROW, OUT, SOUTH, STEP, WEST,
    archive_dates, cell_index, holdout_mask, issue_dates,
)
from forecast_weather import WEATHER

WEEKS = 52  # the last bin also takes days 358–366
OVERLAP_START = pd.Timestamp("2012-02-01")

HISTORY = ["fire_today", "fire_7d", "fire_30d", "fire_7d_3x3", "fire_30d_5x5", "fire_7d_9x9",
           "days_since_fire", "last_year_week_3x3",
           "clim_harmonized", "clim_harmonized_3x3", "clim_viirs", "clim_viirs_3x3", "clim_annual",
           "share_forest", "share_cropland", "season_sin", "season_cos"]
PAST_WEATHER = ["fwi", "ffmc", "dmc", "dc", "rain_7d", "rain_30d", "days_since_rain", "soil_top", "soil_root"]
FORECAST_WEATHER = ["next7_tmax", "next7_rh", "next7_wind_max", "next7_rain", "next7_fwi",
                    "next1_fwi", "next1_rain", "next1_rh"]
FEATURES = HISTORY + PAST_WEATHER + FORECAST_WEATHER


def box_sum(grid, size):
    """Sum over a size×size window on the (NROW, NCOL) grid, zero outside."""
    return uniform_filter(grid.astype(np.float32), size=size, mode="constant") * size * size


def load_fires():
    with duckdb.connect(str(DB), read_only=True) as db:
        k = dict(db.execute("SELECT moy, k FROM calib_moy").fetchall())
        rows = db.execute("""
            SELECT f.date, f.lat, f.lon, f.n_viirs > 0 AS v, f.n_modis > 0 AS m,
                   coalesce(l.land_type, 'unknown') AS land
            FROM fires f LEFT JOIN read_parquet(?) l USING (cell)
        """, [str(LAND_COVER)]).fetchnumpy()
    dates = pd.DatetimeIndex(rows["date"])
    r, c = cell_index(rows["lat"], rows["lon"])
    return {"date": dates, "cell": r * NCOL + c, "v": rows["v"].astype(bool), "m": rows["m"].astype(bool),
            "land": np.asarray(rows["land"]).astype(str)}, k


def fire_grids(fires, k):
    days = archive_dates()
    day = (fires["date"] - days[0]).days.to_numpy()
    inside = (day >= 0) & (day < len(days))
    viirs = np.zeros((len(days), NCELL), dtype=np.uint8)
    take = inside & fires["v"]
    viirs[day[take], fires["cell"][take]] = 1

    # Days with MODIS detections but no VIIRS record anywhere are instrument gaps, not quiet days.
    viirs_total = np.bincount(day[take], minlength=len(days))
    modis_total = np.bincount(day[inside & fires["m"]], minlength=len(days))
    missing = (viirs_total == 0) & (modis_total >= 5)

    year = fires["date"].year.to_numpy()
    week = np.minimum((fires["date"].dayofyear.to_numpy() - 1) // 7, WEEKS - 1)

    # Harmonized weekly counts on the VIIRS scale: VIIRS cell-days from February 2012,
    # earlier MODIS cell-days multiplied by the calendar-month VIIRS/MODIS ratio.
    pool = np.array(CLIM_HARMONIZED_YEARS)
    in_pool = (year >= pool[0]) & (year <= pool[-1])
    after = np.asarray(fires["date"] >= OVERLAP_START)
    month_k = np.array([k.get(m, np.median(list(k.values()))) for m in range(1, 13)])
    weight = np.where(after, fires["v"], fires["m"] * month_k[fires["date"].month.to_numpy() - 1])
    flat = ((year - pool[0]) * WEEKS + week) * NCELL + fires["cell"]
    harmonized = np.bincount(flat[in_pool], weights=weight[in_pool],
                             minlength=len(pool) * WEEKS * NCELL).astype(np.float32)
    harmonized = harmonized.reshape(len(pool), WEEKS, NCELL)

    vpool = np.array(CLIM_VIIRS_YEARS)
    in_vpool = (year >= vpool[0]) & (year <= vpool[-1]) & fires["v"]
    flat = ((year - vpool[0]) * WEEKS + week) * NCELL + fires["cell"]
    occurrence = np.bincount(flat[in_vpool], minlength=len(vpool) * WEEKS * NCELL) > 0
    occurrence = occurrence.reshape(len(vpool), WEEKS, NCELL).astype(np.float32)

    counts = {t: np.bincount(fires["cell"][fires["land"] == t], minlength=NCELL) for t in ("forest", "cropland")}
    total = np.maximum(np.bincount(fires["cell"], minlength=NCELL), 1)
    shares = {t: (v / total).astype(np.float32) for t, v in counts.items()}
    return days, viirs, missing, (pool, harmonized), (vpool, occurrence), shares


def climatology(pool, by_year, year):
    """Mean over pool years, leaving out `year` when it belongs to the pool."""
    total = by_year.sum(axis=0)
    if pool[0] <= year <= pool[-1]:
        return (total - by_year[year - pool[0]]) / (len(pool) - 1)
    return total / len(pool)


def smooth_week(clim, week):
    return 0.25 * clim[(week - 1) % WEEKS] + 0.5 * clim[week] + 0.25 * clim[(week + 1) % WEEKS]


def weather_grids():
    with np.load(WEATHER) as w:
        dates = pd.DatetimeIndex(w["dates"].astype(str))
        lats, lons = w["lats"], w["lons"]
        tmax, dew, rain = w["T2M_MAX"], w["T2MDEW"], w["PRECTOTCORR"]
        wind, top, root = w["WS10M"] * 3.6, w["GWETTOP"], w["GWETROOT"]
    shape = tmax.shape
    flat = lambda a: a.reshape(shape[0], -1)
    rh = fwi.relative_humidity(tmax, dew).astype(np.float32)
    codes = fwi.run(flat(tmax), flat(rh), flat(wind), flat(rain), dates.month.to_numpy())
    rain_f = np.nan_to_num(flat(rain))
    since = np.zeros_like(rain_f)
    for d in range(1, len(dates)):
        since[d] = np.where(rain_f[d] > 1.0, 0, np.minimum(since[d - 1] + 1, 60))
    grids = {"tmax": flat(tmax), "rh": flat(rh), "wind": flat(wind), "rain": rain_f,
             "rain_cum": np.vstack([np.zeros((1, rain_f.shape[1]), np.float32), np.cumsum(rain_f, 0)]),
             "since": since, "top": flat(top), "root": flat(root),
             "fwi": codes["fwi"], "ffmc": codes["ffmc"], "dmc": codes["dmc"], "dc": codes["dc"]}

    # Nearest MERRA-2 grid point for every 0.25° cell centre.
    rows = SOUTH + (np.arange(NROW) + 0.5) * STEP
    cols = WEST + (np.arange(NCOL) + 0.5) * STEP
    i = np.clip(np.round((rows - lats[0]) / 0.5).astype(int), 0, len(lats) - 1)
    j = np.clip(np.round((cols - lons[0]) / 0.625).astype(int), 0, len(lons) - 1)
    index = (i[:, None] * len(lons) + j[None, :]).ravel()
    return dates, grids, index


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print("Loading fires…", flush=True)
    fires, k = load_fires()
    days, viirs, missing, (hpool, harmonized), (vpool, occurrence), shares = fire_grids(fires, k)
    del fires
    print(f"VIIRS gap days: {int(missing.sum())}", flush=True)
    cum = np.vstack([np.zeros((1, NCELL), np.int16), np.cumsum(viirs, axis=0, dtype=np.int16)])

    print("Computing fire weather…", flush=True)
    wdates, wx, windex = weather_grids()

    issues = issue_dates()
    names = FEATURES
    features = np.lib.format.open_memmap(OUT / "features.npy", mode="w+", dtype=np.float16,
                                         shape=(len(issues), len(names), NCELL))
    y1 = np.zeros((len(issues), NCELL), dtype=np.uint8)
    y7 = np.zeros_like(y1)
    valid1 = np.zeros(len(issues), dtype=bool)
    valid7 = np.zeros(len(issues), dtype=bool)

    last_fire = np.full(NCELL, -10_000, dtype=np.int32)
    first_issue = (issues[0] - days[0]).days
    for a in range(first_issue):
        last_fire[viirs[a] > 0] = a

    clim_cache = {}
    grid = lambda v: v.reshape(NROW, NCOL)
    for n, t in enumerate(issues):
        a = (t - days[0]).days
        last_fire[viirs[a] > 0] = a
        d = (t - wdates[0]).days
        mid = t + pd.Timedelta(days=4)
        week = min((mid.dayofyear - 1) // 7, WEEKS - 1)
        if mid.year not in clim_cache:
            clim_cache.clear()
            clim_cache[mid.year] = (climatology(hpool, harmonized, mid.year),
                                    climatology(vpool, occurrence, mid.year))
        ch_all, cv_all = clim_cache[mid.year]
        ch, cv = smooth_week(ch_all, week), smooth_week(cv_all, week)

        n7 = (cum[a + 1] - cum[a - 6]).astype(np.float32)
        n30 = (cum[a + 1] - cum[a - 29]).astype(np.float32)
        last_year = (cum[a + 8 - 364] - cum[a + 1 - 364]).astype(np.float32)
        phase = 2 * np.pi * mid.dayofyear / 365.25
        take = lambda name, offset=0: wx[name][d + offset, windex]
        ahead = slice(d + 1, d + 8)
        values = [
            viirs[a], n7, n30, box_sum(grid(n7), 3).ravel(), box_sum(grid(n30), 5).ravel(),
            box_sum(grid(n7), 9).ravel(), np.minimum(a - last_fire, 365),
            box_sum(grid(last_year), 3).ravel(),
            ch, box_sum(grid(ch), 3).ravel() / 9, cv, box_sum(grid(cv), 3).ravel() / 9,
            np.log1p(ch_all.sum(axis=0)),
            shares["forest"], shares["cropland"],
            np.full(NCELL, np.sin(phase)), np.full(NCELL, np.cos(phase)),
            take("fwi"), take("ffmc"), take("dmc"), take("dc"),
            (wx["rain_cum"][d + 1] - wx["rain_cum"][d - 6])[windex],
            (wx["rain_cum"][d + 1] - wx["rain_cum"][d - 29])[windex],
            take("since"), take("top"), take("root"),
            wx["tmax"][ahead].mean(0)[windex], wx["rh"][ahead].mean(0)[windex],
            wx["wind"][ahead].max(0)[windex], (wx["rain_cum"][d + 8] - wx["rain_cum"][d + 1])[windex],
            wx["fwi"][ahead].mean(0)[windex], take("fwi", 1), take("rain", 1), take("rh", 1),
        ]
        features[n] = np.stack(values).astype(np.float16)
        y1[n] = viirs[a + 1]
        y7[n] = (cum[a + 8] - cum[a + 1]) > 0
        valid1[n] = not missing[a + 1]
        valid7[n] = not missing[a + 1:a + 8].any()
        if n % 500 == 0:
            print(f"{t.date()} ({n + 1}/{len(issues)})", flush=True)
    features.flush()
    del features

    np.savez_compressed(OUT / "targets.npz", y1=y1, y7=y7, valid1=valid1, valid7=valid7,
                        dates=np.asarray(issues.strftime("%Y-%m-%d"), dtype="U10"))
    np.savez_compressed(OUT / "grid.npz", features=np.array(names), history=np.array(HISTORY),
                        past_weather=np.array(PAST_WEATHER), forecast_weather=np.array(FORECAST_WEATHER),
                        holdout=holdout_mask().ravel(), share_forest=shares["forest"],
                        share_cropland=shares["cropland"],
                        bounds=np.array([SOUTH, WEST, STEP, NROW, NCOL], dtype=np.float64))
    print(f"Positive rate: next day {y1[valid1].mean():.4f}, next 7 days {y7[valid7].mean():.4f}")


if __name__ == "__main__":
    main()
