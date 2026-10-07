from __future__ import annotations

import os
import secrets
import sys
import tempfile
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765


def _configure_private_session_secret(data_dir: Path) -> None:
    secret_path = data_dir / "session_secret"
    try:
        descriptor = os.open(
            secret_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        secret = secret_path.read_text(encoding="ascii").strip()
        if len(secret) < 32:
            raise RuntimeError("The local session signing key is invalid.")
    else:
        secret = secrets.token_urlsafe(48)
        with os.fdopen(descriptor, "w", encoding="ascii") as secret_file:
            secret_file.write(secret)
            secret_file.flush()
            os.fsync(secret_file.fileno())

    os.environ["APP_SECRET_KEY"] = secret


def start_server(
    private_data_dir: str,
    private_cache_dir: str,
    web_root_dir: str,
) -> None:
    data_dir = Path(private_data_dir).resolve()
    cache_dir = Path(private_cache_dir).resolve()
    web_root = Path(web_root_dir).resolve()
    database_path = data_dir / "mygoodoldphotos.db"
    upload_dir = cache_dir / "uploads"
    temp_dir = cache_dir / "tmp"

    for directory in (data_dir, upload_dir, temp_dir):
        directory.mkdir(parents=True, exist_ok=True)

    os.environ["HOME"] = str(data_dir)
    os.environ["DATABASE_URL"] = f"sqlite:///{database_path}"
    os.environ["UPLOAD_TEMP_DIR"] = str(upload_dir)
    os.environ["TMPDIR"] = str(temp_dir)
    os.environ["TEMP"] = str(temp_dir)
    tempfile.tempdir = str(temp_dir)
    _configure_private_session_secret(data_dir)

    if not (web_root / "templates").is_dir() or not (web_root / "static").is_dir():
        raise FileNotFoundError(
            f"Expected templates/ and static/ under Android web root: {web_root}"
        )
    os.chdir(web_root)

    app_source_dir = Path(__file__).resolve().parent
    if str(app_source_dir) not in sys.path:
        sys.path.insert(0, str(app_source_dir))

    from app import app
    import uvicorn

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
        access_log=False,
    )
