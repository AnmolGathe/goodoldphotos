from __future__ import annotations

import os
import socket
import time
import traceback
import socket
from worker_notify import SOCKET_PATH

from dotenv import load_dotenv
from sqlalchemy import select

from backup_engine import run_backup_job
from database import BackupJob, SessionLocal, init_db, recover_stale_jobs, utcnow


# ============================================================
# Environment
# ============================================================

load_dotenv()


# ============================================================
# Configuration
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# PostgreSQL supports row locking with SKIP LOCKED.
# SQLite does not, so it uses the simpler local-db path below.
USE_POSTGRES_LOCKING = DATABASE_URL.startswith(
    ("postgresql://", "postgres://")
)


# ============================================================
# Logging helpers
# ============================================================

WORKER_NAME = (
    os.getenv("WORKER_NAME")
    or socket.gethostname()
    or "worker"
)


def log(message: str):
    print(
        f"[worker-{WORKER_NAME}] {message}",
        flush=True,
    )


# ============================================================
# Job claiming
# ============================================================


def claim_next_job():
    """
    Atomically claim one queued job.

    PostgreSQL:
        SELECT ... FOR UPDATE SKIP LOCKED

    SQLite:
        Single-worker/simple local development path.
    """

    db = SessionLocal()

    try:
        if USE_POSTGRES_LOCKING:
            job = (
                db.execute(
                    select(BackupJob)
                    .where(
                        BackupJob.status == "queued"
                    )
                    .order_by(
                        BackupJob.id.asc()
                    )
                    .with_for_update(
                        skip_locked=True
                    )
                    .limit(1)
                )
                .scalars()
                .first()
            )

            if not job:
                db.rollback()
                return None

            job.status = "running"
            job.error = None
            job.started_at = utcnow()
            db.commit()

            return {
                "job_id": job.id,
                "picker_session_id": job.picker_session_id,
            }

        # --------------------------------------------------------
        # SQLite / non-PostgreSQL fallback.
        # --------------------------------------------------------
        job = (
            db.query(BackupJob)
            .filter(
                BackupJob.status == "queued"
            )
            .order_by(
                BackupJob.id.asc()
            )
            .first()
        )

        if not job:
            return None

        job.status = "running"
        job.error = None
        job.started_at = utcnow()
        db.commit()

        return {
            "job_id": job.id,
            "picker_session_id": job.picker_session_id,
        }

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


# ============================================================
# Worker loop
# ============================================================


def process_queued_jobs():
    while True:
        claimed_job = claim_next_job()
        if not claimed_job:
            return
        job_id = claimed_job["job_id"]
        picker_session_id = claimed_job["picker_session_id"]
        log(f"Processing Job #{job_id}" + (" (local upload)" if picker_session_id is None else " (Google Photos)"))
        try:
            result = run_backup_job(job_id, picker_session_id)
            log(f"Job #{job_id} completed: {result.get('status', 'unknown')}")
        except Exception as exc:
            log(f"Job #{job_id} failed; see traceback below.")
            traceback.print_exc()
            db = SessionLocal()
            try:
                job = db.get(BackupJob, job_id)
                if job and job.status == "running":
                    job.status = "failed"
                    job.error = str(exc)
                    job.user_message = "Backup failed. Please check the server logs for details."
                    db.commit()
            except Exception:
                db.rollback(); traceback.print_exc()
            finally:
                db.close()


def wait_for_notification(sock):
    while True:
        data = sock.recv(1024)
        if not data:
            continue
        process_queued_jobs()

def main():
    if os.getenv("SKIP_DB_INIT", "").lower() != "true":
        init_db()
        
    recovered = recover_stale_jobs(int(os.getenv("STALE_JOB_SECONDS", "3600")))
    log("MyGoodOldPhotos worker started.")
    if recovered:
        log(f"Recovered {recovered} stale job(s).")

    process_queued_jobs()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        try: os.unlink(SOCKET_PATH)
        except FileNotFoundError: pass
        sock.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        log(f"Waiting for job notifications on {SOCKET_PATH}")
        wait_for_notification(sock)
    except KeyboardInterrupt:
        log("Worker stopped.")
    finally:
        sock.close()
        try: os.unlink(SOCKET_PATH)
        except FileNotFoundError: pass

if __name__ == "__main__":
    main()
