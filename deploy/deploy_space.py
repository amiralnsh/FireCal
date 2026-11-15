"""Publish FireCal to a Hugging Face Space (Docker) and print its public URL.

One-time login (stores a token on this computer, typed by you):
    .venv\\Scripts\\hf.exe auth login

Then, from the project root:
    .venv\\Scripts\\python.exe deploy\\deploy_space.py            # creates/updates <user>/firecal
    .venv\\Scripts\\python.exe deploy\\deploy_space.py --name xyz # another Space name

Only the files the web app needs are uploaded. The .env file, raw downloads,
build caches and the virtual environment never leave this computer.
"""

import argparse
from pathlib import Path

from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parents[1]
ALLOW = [
    "Dockerfile", ".dockerignore", "requirements.txt", "requirements-forecast.txt",
    "backend/*.py", "pipeline/*.py", "frontend/**",
    "data/firecal.duckdb", "data/fire_land_cover.parquet",
    "data/forecast/predictions.npz", "data/forecast/metrics.json", "data/forecast/grid.npz",
    "data/forecast/targets.npz", "data/forecast/cnn_full.pt", "data/forecast/*.txt",
]
IGNORE = [".env", "**/.env", "**/__pycache__/**", "**/*.pyc"]
FRONT_MATTER = """---
title: FireCal
emoji: 🔥
colorFrom: indigo
colorTo: red
sdk: docker
app_port: 7860
pinned: true
short_description: Harmonized MODIS + VIIRS burning activity calendar
---

"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="firecal", help="Space name (default: firecal)")
    args = parser.parse_args()

    api = HfApi()
    user = api.whoami()["name"]
    repo_id = f"{user}/{args.name}"
    api.create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True)

    readme = FRONT_MATTER + (ROOT / "README.md").read_text(encoding="utf-8")
    api.upload_file(path_or_fileobj=readme.encode("utf-8"), path_in_repo="README.md",
                    repo_id=repo_id, repo_type="space", commit_message="Space README")
    api.upload_folder(folder_path=ROOT, repo_id=repo_id, repo_type="space",
                      allow_patterns=ALLOW, ignore_patterns=IGNORE,
                      commit_message="Deploy FireCal")

    host = f"{user}-{args.name}".lower().replace("_", "-").replace(".", "-")
    print(f"Space page: https://huggingface.co/spaces/{repo_id}")
    print(f"App URL:    https://{host}.hf.space")


if __name__ == "__main__":
    main()
