"""Build the cell-level lookup from the 2021 ESA WorldCover mosaic."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from time import sleep

import duckdb
import h3
import numpy as np
import pandas as pd
import rasterio
from rasterio.errors import RasterioIOError
from rasterio.io import MemoryFile
from rasterio.transform import rowcol
import requests
from tifffile import TiffFile


ROOT = Path(__file__).resolve().parents[1]
TILE_ROOT = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map"


def read_tile(key, positions, lat, lon):
    south, west = key
    tile = f"{'N' if south >= 0 else 'S'}{abs(south):02}{'E' if west >= 0 else 'W'}{abs(west):03}"
    url = f"{TILE_ROOT}/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
    try:
        data = bytearray(read_range(url, 0, 32767))
        with TiffFile(BytesIO(data)) as image:
            overview = image.pages[-1]
            end = max(offset + size for offset, size in zip(overview.dataoffsets, overview.databytecounts))
        while len(data) < end:
            data.extend(read_range(url, len(data), min(end, len(data) + 16384) - 1))
        with MemoryFile(bytes(data)) as raster, raster.open(overview_level=5) as src:
            values = src.read(1)
            rows, cols = rowcol(src.transform, lon[positions], lat[positions])
    except (requests.RequestException, RasterioIOError) as error:
        raise RuntimeError(f"Unable to read ESA WorldCover tile {tile}") from error
    return positions, values[rows, cols]


def read_range(url, start, end):
    hosts = ["https://s3.eu-central-1.amazonaws.com/esa-worldcover/v200/2021/map",
             "https://esa-worldcover.s3.amazonaws.com/v200/2021/map", TILE_ROOT]
    for attempt, host in enumerate(hosts * 2):
        try:
            with requests.get(url.replace(TILE_ROOT, host), params={"bytes": f"{start}-{end}"},
                              headers={"Range": f"bytes={start}-{end}"}, stream=True, timeout=7) as response:
                response.raise_for_status()
                if response.status_code != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {start}-{end}/"):
                    raise requests.RequestException(f"Expected a partial response for {url}")
                data = response.content
                if len(data) != end - start + 1:
                    raise requests.RequestException(f"Incomplete range for {url}")
                return data
        except requests.RequestException:
            if attempt == len(hosts) * 2 - 1:
                raise
            sleep(.2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    with duckdb.connect(str(ROOT / "data/firecal.duckdb"), read_only=True) as connection:
        cells = connection.execute("SELECT DISTINCT cell FROM fires ORDER BY cell").fetchnumpy()["cell"]
    lat, lon = np.array([h3.cell_to_latlng(cell) for cell in cells]).T

    indices = pd.DataFrame({"south": np.floor(lat / 3).astype(int) * 3,
                            "west": np.floor(lon / 3).astype(int) * 3})
    cover = np.zeros(len(cells), dtype=np.uint8)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(read_tile, key, positions, lat, lon)
                for key, positions in indices.groupby(["south", "west"]).indices.items()]
        for job in as_completed(jobs):
            positions, values = job.result()
            cover[positions] = values

    labels = pd.DataFrame({
        "cell": cells,
        "land_type": np.select(
            [cover == 10, cover == 40, cover == 0],
            ["forest", "cropland", "unknown"], default="other",
        ),
    })
    output = ROOT / "data/fire_land_cover.parquet"
    temporary = output.with_suffix(".tmp")
    with duckdb.connect() as connection:
        connection.register("labels", labels)
        connection.execute("COPY (SELECT * FROM labels) TO ? (FORMAT PARQUET)", [str(temporary)])
    temporary.replace(output)
    print(labels["land_type"].value_counts().to_string())


if __name__ == "__main__":
    main()
