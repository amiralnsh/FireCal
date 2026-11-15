"""Run FireCal on this computer and share it through a free public HTTPS link.

    .venv\\Scripts\\python.exe deploy\\run_public.py                 # localhost.run tunnel
    .venv\\Scripts\\python.exe deploy\\run_public.py --tunnel serveo # alternative tunnel

Starts the web server, opens an SSH tunnel (built into Windows, no account needed),
prints the public link, shows a QR code and saves it as a PNG. The link works while
this window stays open; press Ctrl+C to stop. Free tunnels give a new link on every
start, so the QR code is regenerated each time.
"""

import argparse
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import segno

ROOT = Path(__file__).resolve().parents[1]
QR_FILE = ROOT.parent / "Демо" / "FireCal-QR.png"
if not QR_FILE.parent.is_dir():
    QR_FILE = ROOT / "FireCal-QR.png"

TUNNELS = {
    "lhr": (["-R", "80:127.0.0.1:{port}", "nokey@localhost.run"], r"https://[a-z0-9.-]+\.lhr\.life"),
    "serveo": (["-R", "80:127.0.0.1:{port}", "serveo.net"], r"https://[a-z0-9.-]+\.serveo(?:usercontent)?\.(?:net|com)"),
}


def free_port(preferred=8000):
    for port in range(preferred, preferred + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit("Нет свободного порта 8000–8019")


def start_server(port):
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"], cwd=ROOT, env=env)
    print("Запускаю сайт… (при первом запуске сборка базы занимает до минуты)")
    for _ in range(180):
        if server.poll() is not None:
            raise SystemExit("Сервер не запустился, смотрите сообщения выше.")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=5) as r:
                if r.status == 200:
                    print(f"Сайт работает на этом ноутбуке: http://127.0.0.1:{port}/")
                    return server
        except OSError:
            time.sleep(1)
    server.terminate()
    raise SystemExit("Сервер не ответил за 3 минуты.")


def announce(url):
    qr = segno.make(url, error="m")
    target = QR_FILE
    try:
        qr.save(target, scale=14, border=3, dark="#0b1220", light="#ffffff")
    except OSError:
        # The previous image may still be open in a viewer, which locks the file on Windows.
        target = QR_FILE.with_name(f"FireCal-QR-{time.strftime('%H%M%S')}.png")
        qr.save(target, scale=14, border=3, dark="#0b1220", light="#ffffff")
    line = "=" * 64
    print(f"\n{line}\n  ПУБЛИЧНАЯ ССЫЛКА:  {url}\n{line}")
    print("  Откройте её на любом устройстве с интернетом.")
    print(f"  QR-код сохранён: {target}")
    print("  Окно не закрывайте: пока оно открыто, ссылка работает. Ctrl+C — остановить.\n")
    try:
        qr.terminal(compact=True)
    except Exception:
        pass
    try:
        os.startfile(target)
    except OSError:
        pass


CURRENT = {}


def run_tunnel(name, port, stop):
    args, pattern = TUNNELS[name]
    command = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
               "-o", "ServerAliveCountMax=3", "-o", "ExitOnForwardFailure=yes",
               *[a.format(port=port) for a in args]]
    while not stop.is_set():
        print(f"Открываю туннель ({name})…")
        tunnel = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        CURRENT["tunnel"] = tunnel
        shown = None
        for line in tunnel.stdout:
            match = re.search(pattern, line)
            if match and match.group(0) != shown:
                shown = match.group(0)
                announce(shown)
            if stop.is_set():
                break
        tunnel.terminate()
        if stop.is_set():
            return
        print("Туннель оборвался, переподключаюсь через 5 секунд (ссылка изменится)…")
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tunnel", choices=sorted(TUNNELS), default="lhr")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    port = free_port(args.port)
    server = start_server(port)
    stop = threading.Event()
    worker = threading.Thread(target=run_tunnel, args=(args.tunnel, port, stop), daemon=True)
    worker.start()
    try:
        while server.poll() is None:
            time.sleep(1)
        print("Сервер остановился.")
    except KeyboardInterrupt:
        print("\nОстанавливаю…")
    finally:
        stop.set()
        if CURRENT.get("tunnel"):
            CURRENT["tunnel"].terminate()
        server.terminate()


if __name__ == "__main__":
    main()
