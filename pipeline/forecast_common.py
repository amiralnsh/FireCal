"""Shared grid, periods and paths for the FireCal hotspot outlook."""

from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "forecast"
DB = DATA / "firecal.duckdb"
LAND_COVER = DATA / "fire_land_cover.parquet"

# Analysis grid: 0.25° cells over the archive footprint (south-west corner at 40°N, 46°E).
SOUTH, NORTH, WEST, EAST = 40.0, 56.0, 46.0, 93.0
STEP = 0.25
NROW = round((NORTH - SOUTH) / STEP)  # 64
NCOL = round((EAST - WEST) / STEP)    # 188
NCELL = NROW * NCOL

# Daily archive used by the outlook. VIIRS (Suomi NPP) starts on 2012-01-20.
WEATHER_START = pd.Timestamp("2011-01-01")  # one year of spin-up for the fire weather codes
VIIRS_START = pd.Timestamp("2012-01-20")
ARCHIVE_END = pd.Timestamp("2025-12-31")
HORIZON = 7

# Forecast issue dates. A forecast issued at the end of day t covers t+1 (next day) and t+1..t+7.
ISSUE_START = pd.Timestamp("2013-01-01")
ISSUE_END = ARCHIVE_END - pd.Timedelta(days=HORIZON)
TRAIN_YEARS = range(2013, 2022)   # 2013–2021
VALID_YEAR = 2022                 # early stopping and model selection
TEST_YEARS = range(2023, 2026)    # 2023–2025, never used for fitting or selection

# Harmonized climatology pools. Test years are always excluded.
CLIM_HARMONIZED_YEARS = range(2001, 2023)
CLIM_VIIRS_YEARS = range(2013, 2023)

# Spatial hold-out: 4°×4° blocks (16×16 cells) chosen once with a fixed seed.
BLOCK = 16
HOLDOUT_FRACTION = 0.2
SEED = 42


def issue_dates():
    return pd.date_range(ISSUE_START, ISSUE_END, freq="D")


def archive_dates():
    return pd.date_range(VIIRS_START.replace(day=1), ARCHIVE_END, freq="D")


def cell_index(lat, lon):
    """Row/column of the 0.25° cell containing each point (clipped to the grid)."""
    row = np.clip(np.floor((np.asarray(lat) - SOUTH) / STEP).astype(int), 0, NROW - 1)
    col = np.clip(np.floor((np.asarray(lon) - WEST) / STEP).astype(int), 0, NCOL - 1)
    return row, col


def holdout_mask():
    """Boolean (NROW, NCOL) grid of cells that never enter training."""
    rows, cols = -(-NROW // BLOCK), -(-NCOL // BLOCK)
    rng = np.random.default_rng(SEED)
    blocks = rng.permutation(rows * cols)[: round(rows * cols * HOLDOUT_FRACTION)]
    chosen = np.zeros(rows * cols, dtype=bool)
    chosen[blocks] = True
    grid = np.repeat(np.repeat(chosen.reshape(rows, cols), BLOCK, 0), BLOCK, 1)
    return grid[:NROW, :NCOL]
