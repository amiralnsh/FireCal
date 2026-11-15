"""Нормализация по перекрытию: коэффициент VIIRS/MODIS.

С 2012 года работают оба прибора. Считаем, во сколько раз VIIRS видит больше
ячеек-суток с пожаром, чем MODIS, по календарным месяцам (у пожарного сезона
своя сезонность). Этим коэффициентом можно привести период 2000-2012,
где был только MODIS, к шкале VIIRS.

Запуск после load_db.py:
    python pipeline/calibrate.py
Создаёт в базе таблицы calib_month и calib_moy.
"""
import duckdb

MIN_CELLS = 30  # меньше - месяц слишком пустой для надёжного отношения

con = duckdb.connect("data/firecal.duckdb")

con.execute("""
    CREATE OR REPLACE TABLE calib_month AS
    SELECT CAST(date_trunc('month', date) AS DATE) AS month,
           sum(CASE WHEN n_modis > 0 THEN 1 ELSE 0 END) AS modis_cells,
           sum(CASE WHEN n_viirs > 0 THEN 1 ELSE 0 END) AS viirs_cells,
           sum(CASE WHEN n_modis > 0 AND n_viirs > 0 THEN 1 ELSE 0 END) AS both_cells
    FROM fires
    GROUP BY 1 ORDER BY 1
""")

# период перекрытия: оба прибора, достаточно данных
con.execute(f"""
    CREATE OR REPLACE TABLE calib_moy AS
    SELECT month(month) AS moy,
           median(viirs_cells * 1.0 / modis_cells) AS k,
           count(*) AS n_months
    FROM calib_month
    WHERE month >= DATE '2012-02-01' AND modis_cells >= {MIN_CELLS} AND viirs_cells > 0
    GROUP BY 1 ORDER BY 1
""")

print("Коэффициент VIIRS/MODIS по календарным месяцам (медиана по годам):")
print(con.sql("SELECT moy AS month_of_year, round(k, 2) AS k, n_months FROM calib_moy"))

tot = con.sql(f"""
    SELECT sum(viirs_cells) AS viirs, sum(modis_cells) AS modis, sum(both_cells) AS both_
    FROM calib_month WHERE month >= DATE '2012-02-01'
""").fetchone()
if tot and tot[1]:
    print(f"Всего за период перекрытия: VIIRS {tot[0]}, MODIS {tot[1]}, оба {tot[2]}")
    print(f"Общий коэффициент VIIRS/MODIS: {tot[0] / tot[1]:.2f}")
    print(f"Доля MODIS-событий, которые подтвердил и VIIRS: {tot[2] / tot[1]:.0%}")
con.close()