from __future__ import annotations
from pathlib import Path
import os
import signal
import subprocess
import sys
import time


def main() -> int:
    """
    Render entrypoint.

    Runs FastAPI and the backup worker in the same Render service so both
    processes share the same ephemeral filesystem for temporary uploads.
    """

    env = os.environ.copy()
    env["SKIP_DB_INIT"] = "true"
    
    # Keep the worker and web process in the same application directory.
    project_dir = Path(__file__).resolve().parent
    os.chdir(project_dir)

    worker = None
    web = None
    shutting_down = False

    def terminate_children(signum, _frame):
        nonlocal shutting_down

        if shutting_down:
            return

        shutting_down = True
        print(
            f"[render] Received signal {signum}; stopping services.",
            flush=True,
        )

        for process in (web, worker):
            if process and process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    pass

    signal.signal(signal.SIGTERM, terminate_children)
    signal.signal(signal.SIGINT, terminate_children)

    print("[render] Starting MyGoodOldPhotos worker.", flush=True)

    worker = subprocess.Popen(
        [sys.executable, "worker.py"],
        cwd=str(project_dir),
        env=env,
    )

    # Give the worker a moment to create its Unix socket before web requests
    # can enqueue jobs. This is only startup synchronization, not polling.
    time.sleep(1.0)

    if worker.poll() is not None:
        print(
            "[render] Worker exited during startup.",
            flush=True,
        )
        return worker.returncode or 1

    port = os.getenv("PORT", "8000")

    print(
        f"[render] Starting FastAPI on 0.0.0.0:{port}.",
        flush=True,
    )

    web = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "0.0.0.0",
            "--port",
            port,
        ],
        cwd=str(project_dir),
        env=env,
    )

    try:
        while True:
            if web.poll() is not None:
                print(
                    f"[render] Web process exited with code "
                    f"{web.returncode}.",
                    flush=True,
                )
                return web.returncode or 1

            if worker.poll() is not None:
                print(
                    f"[render] Worker process exited with code "
                    f"{worker.returncode}.",
                    flush=True,
                )
                return worker.returncode or 1

            time.sleep(1.0)

    except KeyboardInterrupt:
        terminate_children(signal.SIGINT, None)
        return 0

    finally:
        terminate_children(signal.SIGTERM, None)

        for process in (web, worker):
            if not process:
                continue

            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except Exception:
                    pass

        print("[render] MyGoodOldPhotos stopped.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
