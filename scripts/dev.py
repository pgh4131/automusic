"""Run the local API, durable generation worker, and Next.js with one command."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
children = []


def stop(*_):
    # npm may forward an additional interrupt while descendants are shutting down.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for child in reversed(children):
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for child in children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def launch(command, cwd):
    child = subprocess.Popen(command, cwd=cwd, start_new_session=True)
    children.append(child)
    return child


def main():
    if not PYTHON.exists():
        sys.exit("먼저 README의 Python 가상환경 설치를 진행해 주세요.")
    api = launch([str(PYTHON), "-m", "uvicorn", "app.main:create_app", "--factory",
                  "--host", "127.0.0.1", "--port", "8000"], ROOT / "services/backend")
    # Start the worker only after initial schema creation, avoiding a startup DDL race.
    for _ in range(60):
        if api.poll() is not None:
            sys.exit("API가 시작되지 않았습니다. 위 로그를 확인해 주세요.")
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/health", timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(0.25)
    else:
        sys.exit("API 시작 시간이 초과됐습니다.")
    launch([str(PYTHON), "-m", "app.worker"], ROOT / "services/backend")
    launch([str(PYTHON), "-m", "app.analysis_jobs"], ROOT / "services/backend")
    launch(["npm", "run", "dev:web"], ROOT)
    print("\nAutoMusic → http://127.0.0.1:3000\n", flush=True)
    while all(child.poll() is None for child in children):
        time.sleep(0.5)
    sys.exit("개발 프로세스가 종료됐습니다. 위 로그를 확인해 주세요.")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        stop()
