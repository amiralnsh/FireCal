"""Гармонизация: MODIS + VIIRS -> одна схема, ячейки H3, дедупликация.

Запуск из корня проекта:
    python pipeline/harmonize.py             все найденные годы
    python pipeline/harmonize.py 2018 2020   только эти годы

Результат: data/harmonized/fires_ГОД.csv
Колонки: cell, date, lat, lon, frp, conf (0 low, 1 nominal, 2 high),
         n_points, sensors (MODIS / VIIRS / both), n_modis, n_viirs
"""
import sys
from pathlib import Path

import h3
import numpy as np
import pandas as pd

RES = 7  # размер ячейки сетки (~5 км2)
RAW = Path("data/raw")
OUT = Path("data/harmonized")

_cell = h3.latlng_to_cell if hasattr(h3, "latlng_to_cell") else h3.geo_to_h3


def conf_level(series):
    """Единая шкала: 0 = low (<30), 1 = nominal (30-80), 2 = high (>80).
    MODIS: число 0-100; VIIRS: буквы l / n / h."""
    letters = series.astype(str).str.lower().str.strip().map({"l": 0, "n": 1, "h": 2})
    num = pd.to_numeric(series, errors="coerce")
    by_num = pd.Series(np.where(num < 30, 0, np.where(num <= 80, 1, 2)), index=series.index)
    return letters.fillna(by_num.where(num.notna())).fillna(1).astype(int)


def load(path, sensor):
    df = pd.read_csv(path)
    if df.empty:
        return None
    # type: 0 = растительный пожар; 2 = промышленные источники (факелы нефтегаза и т.п.)
    if "type" in df.columns:
        df = df[df["type"] == 0]
    return pd.DataFrame({
        "lat": df["latitude"],
        "lon": df["longitude"],
        "date": df["acq_date"],
        "sensor": sensor,
        "frp": df["frp"],
        "conf": conf_level(df["confidence"]),
    })


def process(year):
    parts = []
    for src, name in (("MODIS_SP", "MODIS"), ("VIIRS_SNPP_SP", "VIIRS")):
        path = RAW / f"{src}_{year}.csv"
        if path.exists():
            d = load(path, name)
            if d is not None:
                parts.append(d)
                print(f"{year} {name}: {len(d)} точек")
    if not parts:
        print(year, "нет данных, пропускаю")
        return
    df = pd.concat(parts, ignore_index=True)
    df["cell"] = [_cell(a, b, RES) for a, b in zip(df["lat"], df["lon"])]
    df["is_modis"] = (df["sensor"] == "MODIS").astype(int)
    df["is_viirs"] = (df["sensor"] == "VIIRS").astype(int)

    # одна ячейка + один день = одно событие
    ev = df.groupby(["cell", "date"]).agg(
        lat=("lat", "mean"),
        lon=("lon", "mean"),
        frp=("frp", "max"),
        conf=("conf", "max"),
        n_points=("lat", "count"),
        n_modis=("is_modis", "sum"),
        n_viirs=("is_viirs", "sum"),
    ).reset_index()
    ev["sensors"] = np.where(
        (ev["n_modis"] > 0) & (ev["n_viirs"] > 0), "both",
        np.where(ev["n_viirs"] > 0, "VIIRS", "MODIS"),
    )
    OUT.mkdir(parents=True, exist_ok=True)
    ev.to_csv(OUT / f"fires_{year}.csv", index=False)
    counts = {k: int(v) for k, v in ev["sensors"].value_counts().items()}
    print(f"{year}: событий {len(ev)}", counts)


def main():
    if len(sys.argv) >= 3:
        years = range(int(sys.argv[1]), int(sys.argv[2]) + 1)
    elif len(sys.argv) == 2:
        years = [int(sys.argv[1])]
    else:
        found = {p.stem.rsplit("_", 1)[1] for p in RAW.glob("*_SP_????.csv")}
        years = sorted(int(y) for y in found)
    for y in years:
        process(y)

if __name__ == "__main__":
    main()
