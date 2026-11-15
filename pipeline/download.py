"""Скачивание NASA FIRMS (MODIS_SP и VIIRS_SNPP_SP) окнами по 10 дней.

Окна по 5 дней (больше API не принимает), на год около 73 запросов на прибор.

Запуск из корня проекта:
    python pipeline/download.py              годы START_YEAR..END_YEAR
    python pipeline/download.py 2018 2020    только эти годы

Ключ: создай в корне проекта файл .env с одной строкой
    FIRMS_KEY=твой_ключ
Скрипт можно прерывать (Ctrl+C) и запускать снова: готовые окна пропускаются.
"""
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

BBOX = "46,40,93,56"  # запад,юг,восток,север: весь Казахстан + Алтай + Хакасия
START_YEAR, END_YEAR = 2000, 2011
WINDOW = 5  # максимум для одного запроса: API принимает от 1 до 5 дней

# с какой даты у прибора вообще есть данные
SOURCES = {
    "MODIS_SP": date(2000, 11, 1),
    "VIIRS_SNPP_SP": date(2012, 1, 20),
}
RAW = Path("data/raw")
CACHE = RAW / "cache"


def get_key():
    key = os.environ.get("FIRMS_KEY")
    env = Path(".env")
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("FIRMS_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        sys.exit("Нет ключа. Создай файл .env в корне проекта со строкой FIRMS_KEY=твой_ключ")
    return key


def fetch(key, src, start, days):
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{src}/{BBOX}/{days}/{start}"
    text = ""
    for attempt in range(5):
        try:
            text = requests.get(url, timeout=120).text
        except requests.RequestException as e:
            text = "сетевая ошибка: " + str(e)[:80]
        if text.startswith("latitude"):
            return text
        if "invalid" in text.lower():  # ошибка в параметрах: повторять бесполезно
            break
        if "exceed" in text.lower() or "limit" in text.lower():
            print("  лимит NASA, пауза 60 с...")
            time.sleep(60)
        else:
            time.sleep(3 * (attempt + 1))
    print("  ОШИБКА:", text[:150].replace(key, "*"))
    return None


def merge(src, year, files):
    out = RAW / f"{src}_{year}.csv"
    header, total = None, 0
    with open(out, "w", encoding="utf-8") as f:
        for p in files:
            lines = p.read_text(encoding="utf-8").strip().split("\n")
            if header is None:
                header = lines[0]
                f.write(header + "\n")
            body = lines[1:]
            if body:
                f.write("\n".join(body) + "\n")
            total += len(body)
    print(f"Сохранено {out}: {total} точек")


def main():
    key = get_key()
    y0, y1 = START_YEAR, END_YEAR
    if len(sys.argv) >= 3:
        y0, y1 = int(sys.argv[1]), int(sys.argv[2])
    elif len(sys.argv) == 2:
        y0 = y1 = int(sys.argv[1])
    today = date.today()

    for src, src_start in SOURCES.items():
        (CACHE / src).mkdir(parents=True, exist_ok=True)
        for year in range(y0, y1 + 1):
            d, last = date(year, 1, 1), date(year, 12, 31)
            files, failed = [], 0
            while d <= last:
                days = min(WINDOW, (last - d).days + 1)
                end = d + timedelta(days=days - 1)
                if end >= src_start and d < today:
                    path = CACHE / src / f"{d}.csv"
                    if not path.exists():
                        text = fetch(key, src, d, days)
                        if text is None:
                            failed += 1
                        elif end < today:  # неполные окна не кэшируем
                            path.write_text(text.strip() + "\n", encoding="utf-8")
                            print(src, d, "ок, точек:", len(text.strip().split("\n")) - 1)
                        time.sleep(0.5)
                    if path.exists():
                        files.append(path)
                d += timedelta(days=days)
            if failed:
                print(f"{src} {year}: не скачано окон: {failed}. Запусти скрипт ещё раз.")
            elif files:
                merge(src, year, files)


if __name__ == "__main__":
    main()