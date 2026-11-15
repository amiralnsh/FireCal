"""Publish FireCal to a public GitHub repository without installing Git.

Uses the GitHub REST API: creates the repository on the first run and replaces
its contents with a new commit on later runs.

    .venv\\Scripts\\python.exe deploy\\push_github.py              # repository "FireCal"
    .venv\\Scripts\\python.exe deploy\\push_github.py --name Other

The script asks for a GitHub token (classic, scope "repo"), typed by you and never
saved: https://github.com/settings/tokens/new?scopes=repo&description=FireCal
Only the files listed below are uploaded; .env, raw downloads and caches stay local.
"""

import argparse
import base64
import fnmatch
import getpass
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
INCLUDE = [
    "README.md", ".gitignore", ".dockerignore", "Dockerfile", "requirements.txt", "requirements-forecast.txt",
    "start_local.bat", "start_public.bat",
    "backend/*.py", "pipeline/*.py", "deploy/*.py", "docs/*",
    "frontend/*", "frontend/assets/*", "frontend/vendor/*",
    "data/db/*.parquet", "data/fire_land_cover.parquet",
    "data/forecast/predictions.npz", "data/forecast/metrics.json", "data/forecast/grid.npz",
    "data/forecast/targets.npz", "data/forecast/cnn_full.pt", "data/forecast/*.txt",
]
EXCLUDE = [".env", "*/.env", "*__pycache__*", "*.pyc", "*.log"]
DESCRIPTION = ("FireCal: harmonized MODIS + VIIRS burning activity calendar and hotspot outlook "
               "(NASA Space Apps Challenge 2026)")


def wanted(rel):
    if any(fnmatch.fnmatch(rel, pattern) for pattern in EXCLUDE):
        return False
    for pattern in INCLUDE:
        if pattern.endswith("/*") and "vendor" in pattern:
            if rel.startswith(pattern[:-1]):
                return True
        elif fnmatch.fnmatch(rel, pattern) and rel.count("/") == pattern.count("/"):
            return True
    return False


def files():
    out = []
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT).as_posix()
        if path.is_file() and not rel.startswith(".venv/") and wanted(rel):
            out.append((rel, path))
    return out


class GitHub:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "firecal-publisher"})

    def call(self, method, path, **kwargs):
        for attempt in range(5):
            try:
                response = self.session.request(method, API + path, timeout=600, **kwargs)
            except requests.RequestException as error:
                if attempt == 4:
                    raise
                print(f"  network error ({error.__class__.__name__}), retrying…", flush=True)
                time.sleep(5 * (attempt + 1))
                continue
            if response.status_code in (502, 503, 504) and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            return response
        return response


LOG = Path(__file__).with_name("push.log")


class Tee:
    """Mirror console output into deploy/push.log (the token is never printed)."""

    def __init__(self, stream):
        self.stream = stream
        self.file = LOG.open("w", encoding="utf-8")

    def write(self, text):
        self.stream.write(text)
        self.file.write(text)
        self.file.flush()

    def flush(self):
        self.stream.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="FireCal")
    args = parser.parse_args()
    sys.stdout = Tee(sys.stdout)
    sys.stderr = sys.stdout

    token = os.environ.get("GITHUB_TOKEN") or getpass.getpass("Вставьте токен GitHub и нажмите Enter (символы не видны): ").strip()
    gh = GitHub(token)
    me = gh.call("GET", "/user")
    if me.status_code != 200:
        sys.exit(f"Токен не подошёл (GitHub ответил {me.status_code}). Создайте новый со scope «repo».")
    login = me.json()["login"]
    repo = f"{login}/{args.name}"
    print(f"Аккаунт: {login}")

    info = gh.call("GET", f"/repos/{repo}")
    if info.status_code == 404:
        created = gh.call("POST", "/user/repos", json={"name": args.name, "description": DESCRIPTION,
                                                        "private": False, "auto_init": True})
        if created.status_code != 201:
            sys.exit(f"Не удалось создать репозиторий: {created.status_code} {created.text[:300]}")
        print(f"Создан репозиторий {repo}")
        time.sleep(3)
        info = gh.call("GET", f"/repos/{repo}")
    branch = info.json()["default_branch"]
    head = gh.call("GET", f"/repos/{repo}/git/ref/heads/{branch}").json()["object"]["sha"]

    entries, items = [], files()
    total = sum(p.stat().st_size for _, p in items)
    sent = 0
    print(f"Загружаю {len(items)} файлов, {total / 2**20:.1f} МБ")
    for rel, path in items:
        data = path.read_bytes()
        blob = gh.call("POST", f"/repos/{repo}/git/blobs",
                       json={"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
        if blob.status_code != 201:
            sys.exit(f"Ошибка загрузки {rel}: {blob.status_code} {blob.text[:300]}")
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]})
        sent += len(data)
        if len(data) > 2**20 or len(entries) % 20 == 0:
            print(f"  {sent / total:6.1%}  {rel}", flush=True)

    tree = gh.call("POST", f"/repos/{repo}/git/trees", json={"tree": entries})
    if tree.status_code != 201:
        sys.exit(f"Ошибка дерева файлов: {tree.status_code} {tree.text[:300]}")
    commit = gh.call("POST", f"/repos/{repo}/git/commits", json={
        "message": "Publish FireCal", "tree": tree.json()["sha"], "parents": [head]})
    if commit.status_code != 201:
        sys.exit(f"Ошибка коммита: {commit.status_code} {commit.text[:300]}")
    update = gh.call("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": commit.json()["sha"]})
    if update.status_code != 200:
        sys.exit(f"Ошибка обновления ветки: {update.status_code} {update.text[:300]}")
    gh.call("PATCH", f"/repos/{repo}", json={"description": DESCRIPTION})
    gh.call("PUT", f"/repos/{repo}/topics", json={"names": [
        "nasa-space-apps", "wildfire", "modis", "viirs", "firms", "remote-sensing", "fastapi", "pytorch"]})
    print(f"\nГОТОВО: https://github.com/{repo}")


if __name__ == "__main__":
    main()
