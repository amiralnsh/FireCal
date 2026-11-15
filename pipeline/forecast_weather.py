# AI-assisted: developed with help from the Claude AI assistant (Anthropic); see README, 'Use of AI tools'.
"""Download daily NASA POWER (MERRA-2) weather for the whole archive footprint.

NASA POWER serves MERRA-2 on its native 0.5° × 0.625° grid without an API key.
The regional endpoint accepts one parameter and at most 366 days per request,
so the footprint is split into ten boxes and requested year by year. Responses
are cached in data/forecast/.power-cache, so the script can be interrupted and
resumed.

    python pipeline/forecast_weather.py [--workers 4]

Output: data/forecast/weather.npz with arrays of shape (days, 33 lat, 75 lon).
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import sleep

import numpy as np
import pandas as pd
import requests

from forecast_common import ARCHIVE_END, OUT, WEATHER_START

URL = "https://power.larc.nasa.gov/api/temporal/daily/regional"
PARAMETERS = ("T2M_MAX", "T2MDEW", "PRECTOTCORR", "WS10M", "GWETTOP", "GWETROOT")
LAT_BANDS = ((40, 48), (48, 56))
LON_BANDS = ((46, 56), (56, 66), (66, 76), (76, 86), (86, 93))
LATS = np.round(np.arange(40.0, 56.0 + 1e-9, 0.5), 3)
LONS = np.round(np.arange(46.25, 93.0, 0.625), 3)
CACHE = OUT / ".power-cache"
WEATHER = OUT / "weather.npz"


def fetch(parameter, year, lat_band, lon_band):
    path = CACHE / f"{parameter}_{year}_{lat_band[0]}_{lon_band[0]}.npz"
    if path.is_file():
        return path
    start = max(pd.Timestamp(year, 1, 1), WEATHER_START)
    end = min(pd.Timestamp(year, 12, 31), ARCHIVE_END)
    params = {
        "parameters": parameter, "community": "AG", "format": "JSON",
        "latitude-min": lat_band[0], "latitude-max": lat_band[1],
        "longitude-min": lon_band[0], "longitude-max": lon_band[1],
        "start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
    }
    for attempt in range(6):
        try:
            response = requests.get(URL, params=params, timeout=300)
            if response.status_code == 429:
                sleep(30 * (attempt + 1))
                continue
            response.raise_for_status()
            features = response.json()["features"]
            days = pd.date_range(start, end).strftime("%Y%m%d")
            lon = np.array([f["geometry"]["coordinates"][0] for f in features])
            lat = np.array([f["geometry"]["coordinates"][1] for f in features])
            values = np.array([[f["properties"]["parameter"][parameter].get(d, -999.0) for d in days]
                               for f in features], dtype=np.float32)
            values[values <= -998] = np.nan
            tmp = path.with_suffix(".tmp")
            with tmp.open("wb") as file:
                np.savez_compressed(file, lat=lat, lon=lon, values=values, start=str(start.date()))
            tmp.replace(path)
            return path
        except (requests.RequestException, KeyError, ValueError) as error:
            if attempt == 5:
                raise RuntimeError(f"POWER request failed: {parameter} {year} {lat_band} {lon_band}") from error
            sleep(5 * (attempt + 1))


def assemble():
    dates = pd.date_range(WEATHER_START, ARCHIVE_END)
    grids = {p: np.full((len(dates), len(LATS), len(LONS)), np.nan, dtype=np.float32) for p in PARAMETERS}
    for path in sorted(CACHE.glob("*.npz")):
        parameter = path.stem.rsplit("_", 3)[0]
        if parameter not in grids:
            continue
        with np.load(path) as part:
            offset = (pd.Timestamp(str(part["start"])) - dates[0]).days
            i = np.round((part["lat"] - LATS[0]) / 0.5).astype(int)
            j = np.round((part["lon"] - LONS[0]) / 0.625).astype(int)
            keep = (i >= 0) & (i < len(LATS)) & (j >= 0) & (j < len(LONS))
            values = part["values"][keep]
            grids[parameter][offset:offset + values.shape[1], i[keep], j[keep]] = values.T
    for parameter, grid in grids.items():
        print(f"{parameter}: {np.isfinite(grid).mean():.2%} present")
    tmp = WEATHER.with_suffix(".tmp")
    with tmp.open("wb") as file:
        np.savez_compressed(file, dates=np.asarray(dates.strftime("%Y-%m-%d"), dtype="U10"),
                            lats=LATS, lons=LONS, **grids)
    tmp.replace(WEATHER)
    print(f"Saved {WEATHER} ({len(dates)} days × {len(LATS)} × {len(LONS)})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    jobs = [(p, y, la, lo) for y in range(WEATHER_START.year, ARCHIVE_END.year + 1)
            for p in PARAMETERS for la in LAT_BANDS for lo in LON_BANDS]
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(fetch, *job) for job in jobs]):
            future.result()
            done += 1
            if done % 25 == 0 or done == len(jobs):
                print(f"{done}/{len(jobs)} requests cached", flush=True)
    assemble()


if __name__ == "__main__":
    main()
