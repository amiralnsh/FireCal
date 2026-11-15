"""Export data/firecal.duckdb to compressed Parquet files in data/db/.

The Parquet copy is small enough for GitHub (the DuckDB file is over 100 MB).
The backend rebuilds data/firecal.duckdb from these files on first start.

Run from the project root after load_db.py and calibrate.py:
    python pipeline/export_db.py
"""
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "firecal.duckdb"
OUT = ROOT / "data" / "db"
ORDER = {"fires": "date, cell", "calib_month": "month", "calib_moy": "moy"}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(DB), read_only=True) as con:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()]
        for table in tables:
            path = OUT / f"{table}.parquet"
            order = ORDER.get(table, "ALL")
            con.execute(f'COPY (SELECT * FROM "{table}" ORDER BY {order}) TO \'{path.as_posix()}\' '
                        "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 19)")
            print(f"{table}: {path.stat().st_size / 2**20:.1f} MB")


if __name__ == "__main__":
    main()
