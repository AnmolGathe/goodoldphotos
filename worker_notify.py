from __future__ import annotations

import os
import socket
from pathlib import Path

SOCKET_PATH = os.getenv("WORKER_SOCKET_PATH", "/tmp/mygoodoldphotos-worker.sock")

def notify_worker(job_id: int | None = None) -> bool:
    payload = str(job_id if job_id is not None else "*").encode("utf-8")
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.25)
            sock.sendto(payload, SOCKET_PATH)
        finally:
            sock.close()
        return True
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        return False
