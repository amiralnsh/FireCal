"""Загрузка всех data/harmonized/fires_*.csv в DuckDB (таблица fires).

ВАЖНО: останови сервер (Ctrl+C в его терминале) перед запуском.
Шкала conf в базе: 1 = low, 2 = nominal, 3 = high (так ждут backend и сайт).
"""
import glob
import sys

import duckdb

files = sorted(glob.glob("data/harmonized/fires_*.csv"))
if not files:
    sys.exit("Нет файлов data/harmonized/fires_*.csv. Сначала запусти harmonize.py")

con = duckdb.connect("data/firecal.duckdb")
con.execute("""
    CREATE OR REPLACE TABLE fires AS
    SELECT cell,
           CAST(date AS DATE) AS date,
           lat, lon, frp,
           conf + 1 AS conf,
           n_points, sensors, n_modis, n_viirs
    FROM read_csv_auto('data/harmonized/fires_*.csv', types={'cell': 'VARCHAR'})
""")
print("Файлов:", len(files))
print(con.sql("SELECT count(*) AS rows, min(date) AS first_day, max(date) AS last_day FROM fires"))
print(con.sql("SELECT sensors, count(*) AS n FROM fires GROUP BY sensors ORDER BY n DESC"))
print(con.sql("SELECT conf, count(*) AS n FROM fires GROUP BY conf ORDER BY conf"))
con.close()