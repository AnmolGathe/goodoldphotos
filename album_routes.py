
from __future__ import annotations

import json
import queue
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from database import SessionLocal, User, Album
from download_manager import (
    download_chunk as download_verified_chunk,
    download_and_extract_chunk,
    get_album_chunk,
    get_album_download_plan,
    list_album_chunks,
)


router = APIRouter(
    prefix="/api/albums",
    tags=["albums"],
)


# ============================================================
# Download progress state
# ============================================================

_DOWNLOAD_PROGRESS: dict[str, dict] = {}
_DOWNLOAD_QUEUES: dict[str, queue.Queue] = {}
_DOWNLOAD_LOCK = threading.Lock()
_DOWNLOAD_TTL_SECONDS = 15 * 60


def _new_download_job() -> str:
    token = uuid.uuid4().hex
    with _DOWNLOAD_LOCK:
        _DOWNLOAD_PROGRESS[token] = {
            "status": "starting",
            "operation": "starting",
            "current": 0,
            "current_total": 0,
            "percent": 0.0,
            "filename": "",
            "message": "Preparing download...",
            "updated_at": time.time(),
        }
        _DOWNLOAD_QUEUES[token] = queue.Queue()
    return token


def _publish_download(token: str, **updates) -> None:
    payload = dict(updates)
    payload["updated_at"] = time.time()

    with _DOWNLOAD_LOCK:
        if token not in _DOWNLOAD_PROGRESS:
            return
        _DOWNLOAD_PROGRESS[token].update(payload)
        q = _DOWNLOAD_QUEUES.get(token)

    if q is not None:
        try:
            q.put_nowait(dict(_DOWNLOAD_PROGRESS[token]))
        except Exception:
            pass


def _finish_download_job(token: str, status: str, message: str) -> None:
    _publish_download(
        token,
        status=status,
        operation="complete" if status == "completed" else "failed",
        message=message,
    )


def _cleanup_download_job(token: str) -> None:
    with _DOWNLOAD_LOCK:
        _DOWNLOAD_PROGRESS.pop(token, None)
        _DOWNLOAD_QUEUES.pop(token, None)


# ============================================================
# Authentication / ownership
# ============================================================

def get_current_user(
    request: Request,
) -> User:
    user_id = request.session.get(
        "user_id"
    )

    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="You must sign in with Google.",
        )

    db = SessionLocal()

    try:
        user = db.get(
            User,
            int(user_id),
        )

        if not user:
            raise HTTPException(
                status_code=401,
                detail="User account not found.",
            )

        # Detach before closing the DB session.
        db.expunge(
            user
        )

        return user

    finally:
        db.close()


def get_owned_album(
    user: User,
    album_id: int,
) -> Album:
    db = SessionLocal()

    try:
        album = db.get(
            Album,
            album_id,
        )

        if not album:
            raise HTTPException(
                status_code=404,
                detail="Album not found.",
            )

        if album.user_id != user.id:
            raise HTTPException(
                status_code=403,
                detail="Access denied.",
            )

        db.expunge(
            album
        )

        return album

    finally:
        db.close()


# ============================================================
# Albums
# ============================================================

@router.get("")
def list_albums(
    request: Request,
):
    user = get_current_user(
        request
    )

    db = SessionLocal()

    try:
        albums = (
            db.query(Album)
            .filter(
                Album.user_id
                == user.id
            )
            .order_by(
                Album.id.desc()
            )
            .all()
        )

        return {
            "albums": [
                {
                    "id": album.id,
                    "name": album.name,
                }
                for album in albums
            ]
        }

    finally:
        db.close()


@router.get("/{album_id}")
def get_album(
    request: Request,
    album_id: int,
):
    user = get_current_user(
        request
    )

    album = get_owned_album(
        user,
        album_id,
    )

    try:
        plan = get_album_download_plan(
            user,
            album,
        )

        return plan

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


# ============================================================
# Chunks
# ============================================================

@router.get("/{album_id}/chunks")
def get_chunks(
    request: Request,
    album_id: int,
):
    user = get_current_user(
        request
    )

    album = get_owned_album(
        user,
        album_id,
    )

    try:
        assets = list_album_chunks(
            user,
            album,
        )

        return {
            "album_id": album.id,
            "album_name": album.name,
            "chunks": [
                {
                    "id": asset["id"],
                    "name": asset["name"],
                    "size": int(
                        asset.get(
                            "size",
                            0,
                        )
                    ),
                    "part_number": (
                        int(
                            asset["name"].split(
                                "Part ",
                                1,
                            )[1].split(
                                " ",
                                1,
                            )[0]
                        )
                        if "Part " in asset.get(
                            "name",
                            "",
                        )
                        else None
                    ),
                }
                for asset in assets
            ],
        }

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


@router.get(
    "/{album_id}/chunks/{asset_id}",
)
def get_chunk(
    request: Request,
    album_id: int,
    asset_id: int,
):
    user = get_current_user(
        request
    )

    album = get_owned_album(
        user,
        album_id,
    )

    try:
        asset = get_album_chunk(
            user,
            album,
            asset_id,
        )

        return {
            "id": asset["id"],
            "name": asset["name"],
            "size": int(
                asset.get(
                    "size",
                    0,
                )
            ),
            "content_type": asset.get(
                "content_type",
                "application/octet-stream",
            ),
        }

    except RuntimeError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc


# ============================================================
# Download one verified archive
# ============================================================

@router.post(
    "/{album_id}/chunks/{asset_id}/download/start",
)
def start_chunk_download(
    request: Request,
    album_id: int,
    asset_id: int,
):
    """Create a download-progress token. The actual download starts separately."""
    user = get_current_user(request)
    album = get_owned_album(user, album_id)

    try:
        # Validate ownership and asset association before issuing a token.
        get_album_chunk(user, album, asset_id)
        token = _new_download_job()
        return {"token": token}
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="Download could not be started.") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Download could not be started.") from exc


@router.get(
    "/download-progress/{token}",
)
def download_progress(
    request: Request,
    token: str,
):
    """Server-sent events for one active archive download."""
    get_current_user(request)

    with _DOWNLOAD_LOCK:
        q = _DOWNLOAD_QUEUES.get(token)
        current = _DOWNLOAD_PROGRESS.get(token)

    if q is None or current is None:
        raise HTTPException(status_code=404, detail="Download progress session not found.")

    def event_stream():
        # Immediately emit the current state.
        yield f"data: {json.dumps(current)}\n\n"

        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue

            yield f"data: {json.dumps(item)}\n\n"

            if item.get("status") in {"completed", "failed"}:
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/{album_id}/chunks/{asset_id}/download",
)
def download_chunk(
    request: Request,
    album_id: int,
    asset_id: int,
    token: str | None = None,
):
    """
    Verify the GitHub archive, then stream it to the browser while publishing
    live progress. The page remains open because the frontend starts this
    request through a hidden iframe.
    """
    user = get_current_user(request)
    album = get_owned_album(user, album_id)

    if token:
        with _DOWNLOAD_LOCK:
            if token not in _DOWNLOAD_PROGRESS:
                raise HTTPException(status_code=404, detail="Download progress session not found.")

    working_directory: Path | None = None

    try:
        asset = get_album_chunk(user, album, asset_id)

        if token:
            _publish_download(
                token,
                status="running",
                operation="fetching",
                filename=asset["name"],
                current=0,
                current_total=int(asset.get("size", 0) or 0),
                percent=0.0,
                message="Fetching archive from storage...",
            )

        def on_fetch(current: int, total: int | None):
            percent = (
                (current / total * 100.0)
                if total and total > 0
                else 0.0
            )
            if token:
                _publish_download(
                    token,
                    status="running",
                    operation="fetching",
                    filename=asset["name"],
                    current=current,
                    current_total=total or 0,
                    percent=percent,
                    message="Downloading archive from storage...",
                )

        archive_path, working_directory = download_verified_chunk(
            user,
            album,
            asset,
            progress_callback=on_fetch if token else None,
        )

        total_bytes = archive_path.stat().st_size

        if token:
            _publish_download(
                token,
                status="running",
                operation="sending",
                filename=asset["name"],
                current=0,
                current_total=total_bytes,
                percent=0.0,
                message="Sending archive to your browser...",
            )

        def stream_archive():
            sent = 0
            try:
                with archive_path.open("rb") as source:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break

                        yield block
                        sent += len(block)

                        if token:
                            percent = (
                                sent / total_bytes * 100.0
                                if total_bytes
                                else 100.0
                            )
                            _publish_download(
                                token,
                                status="running",
                                operation="sending",
                                filename=asset["name"],
                                current=sent,
                                current_total=total_bytes,
                                percent=percent,
                                message="Sending archive to your browser...",
                            )

                if token:
                    _finish_download_job(
                        token,
                        "completed",
                        "Download complete.",
                    )
            except Exception:
                if token:
                    _finish_download_job(
                        token,
                        "failed",
                        "Download failed.",
                    )
                raise
            finally:
                if working_directory:
                    shutil.rmtree(
                        working_directory,
                        ignore_errors=True,
                    )
                if token:
                    # Leave the final state briefly so the SSE client receives it.
                    threading.Timer(
                        10.0,
                        _cleanup_download_job,
                        args=(token,),
                    ).start()

        return StreamingResponse(
            stream_archive(),
            media_type="application/x-tar",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{asset["name"].replace(chr(34), "")}"'
                ),
                "Content-Length": str(total_bytes),
                "Cache-Control": "no-store",
            },
        )

    except HTTPException:
        if working_directory:
            shutil.rmtree(working_directory, ignore_errors=True)
        raise

    except RuntimeError as exc:
        if working_directory:
            shutil.rmtree(working_directory, ignore_errors=True)
        if token:
            _finish_download_job(token, "failed", "Download failed.")
        raise HTTPException(status_code=404, detail="Download failed.") from exc

    except Exception as exc:
        if working_directory:
            shutil.rmtree(working_directory, ignore_errors=True)
        if token:
            _finish_download_job(token, "failed", "Download failed.")
        raise HTTPException(status_code=502, detail="Download failed.") from exc


# ============================================================
# Restore one chunk as a ZIP
# ============================================================

@router.post(
    "/{album_id}/chunks/{asset_id}/restore/start",
)
def start_restore_download(
    request: Request,
    album_id: int,
    asset_id: int,
):
    """Create a progress token for one Restore ZIP operation."""
    user = get_current_user(request)
    album = get_owned_album(user, album_id)

    try:
        get_album_chunk(
            user,
            album,
            asset_id,
        )

        token = _new_download_job()

        return {
            "token": token,
        }

    except RuntimeError as exc:
        raise HTTPException(
            status_code=404,
            detail="Restore could not be started.",
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Restore could not be started.",
        ) from exc


@router.get(
    "/{album_id}/chunks/{asset_id}/restore",
)
def restore_chunk(
    request: Request,
    album_id: int,
    asset_id: int,
    token: str | None = None,
):
    """
    Download and verify the TAR, safely extract it, create a STORE ZIP,
    then stream the ZIP to the browser while publishing live progress.
    """
    user = get_current_user(request)
    album = get_owned_album(
        user,
        album_id,
    )

    if token:
        with _DOWNLOAD_LOCK:
            if (
                token not in _DOWNLOAD_PROGRESS
                or token not in _DOWNLOAD_QUEUES
            ):
                raise HTTPException(
                    status_code=404,
                    detail="Download progress session not found.",
                )

    working_directory: Path | None = None
    output_directory: Path | None = None

    try:
        asset = get_album_chunk(
            user,
            album,
            asset_id,
        )

        expected_archive_size = int(
            asset.get("size", 0) or 0
        )

        if token:
            _publish_download(
                token,
                status="running",
                operation="fetching",
                filename=asset["name"],
                current=0,
                current_total=expected_archive_size,
                percent=0.0,
                message="Downloading archive from storage...",
            )

        def on_fetch(
            current: int,
            total: int | None,
        ):
            total_value = int(
                total or expected_archive_size or 0
            )

            percent = (
                current / total_value * 100.0
                if total_value > 0
                else 0.0
            )

            if token:
                _publish_download(
                    token,
                    status="running",
                    operation="fetching",
                    filename=asset["name"],
                    current=current,
                    current_total=total_value,
                    percent=percent,
                    message="Downloading archive from storage...",
                )

        # IMPORTANT:
        # Use the progress-aware download_chunk(), not the old
        # download_and_extract_chunk() helper.
        archive_path, working_directory = download_verified_chunk(
            user,
            album,
            asset,
            progress_callback=(
                on_fetch if token else None
            ),
        )

        # Verify/extract using the existing safe extraction implementation.
        from download_manager import extract_chunk

        if token:
            _publish_download(
                token,
                status="running",
                operation="verifying",
                filename="manifest.json",
                current=0,
                current_total=1,
                percent=0.0,
                message="Verifying archive...",
            )

        extraction_directory, manifest = extract_chunk(
            archive_path,
            working_directory,
        )

        if token:
            _publish_download(
                token,
                status="running",
                operation="verifying",
                filename="manifest.json",
                current=1,
                current_total=1,
                percent=100.0,
                message="Archive verified. Preparing ZIP...",
            )

        media_directory = (
            extraction_directory / "media"
        )

        if not media_directory.is_dir():
            raise RuntimeError(
                "Backup chunk does not contain a media directory."
            )

        output_directory = Path(
            tempfile.mkdtemp(
                prefix="mygoodoldphotos_zip_"
            )
        )

        zip_path = (
            output_directory
            / (
                Path(asset["name"]).stem
                + ".zip"
            )
        )

        media_files = [
            path
            for path in media_directory.rglob("*")
            if path.is_file()
        ]

        total_zip_items = max(
            1,
            len(media_files),
        )

        with zipfile.ZipFile(
            zip_path,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:

            manifest_path = (
                extraction_directory
                / "manifest.json"
            )

            if manifest_path.is_file():
                archive.write(
                    manifest_path,
                    "manifest.json",
                )

            for index, file_path in enumerate(
                media_files,
                start=1,
            ):
                relative_path = (
                    file_path
                    .relative_to(
                        extraction_directory
                    )
                )

                archive.write(
                    file_path,
                    str(relative_path),
                )

                if token:
                    _publish_download(
                        token,
                        status="running",
                        operation="creating_zip",
                        filename=file_path.name,
                        current=index,
                        current_total=total_zip_items,
                        percent=(
                            index
                            / total_zip_items
                            * 100.0
                        ),
                        message="Creating ZIP archive...",
                    )

        zip_size = zip_path.stat().st_size

        if token:
            _publish_download(
                token,
                status="running",
                operation="sending",
                filename=zip_path.name,
                current=0,
                current_total=zip_size,
                percent=0.0,
                message="Sending ZIP to your browser...",
            )

        def stream_zip():
            sent = 0

            try:
                with zip_path.open("rb") as source:
                    while True:
                        block = source.read(
                            1024 * 1024
                        )

                        if not block:
                            break

                        yield block

                        sent += len(block)

                        if token:
                            percent = (
                                sent
                                / zip_size
                                * 100.0
                                if zip_size > 0
                                else 100.0
                            )

                            _publish_download(
                                token,
                                status="running",
                                operation="sending",
                                filename=zip_path.name,
                                current=sent,
                                current_total=zip_size,
                                percent=percent,
                                message="Sending ZIP to your browser...",
                            )

                if token:
                    _publish_download(
                        token,
                        status="completed",
                        operation="complete",
                        filename=zip_path.name,
                        current=zip_size,
                        current_total=zip_size,
                        percent=100.0,
                        message="Download complete.",
                    )

            except Exception:
                if token:
                    _finish_download_job(
                        token,
                        "failed",
                        "Download failed.",
                    )
                raise

            finally:
                if working_directory:
                    shutil.rmtree(
                        working_directory,
                        ignore_errors=True,
                    )

                if output_directory:
                    shutil.rmtree(
                        output_directory,
                        ignore_errors=True,
                    )

                if token:
                    threading.Timer(
                        10.0,
                        _cleanup_download_job,
                        args=(token,),
                    ).start()

        return StreamingResponse(
            stream_zip(),
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    'attachment; filename="'
                    + zip_path.name.replace(
                        '"',
                        "",
                    )
                    + '"'
                ),
                "Content-Length": str(zip_size),
                "Cache-Control": "no-store",
            },
        )

    except HTTPException:
        if working_directory:
            shutil.rmtree(
                working_directory,
                ignore_errors=True,
            )

        if output_directory:
            shutil.rmtree(
                output_directory,
                ignore_errors=True,
            )

        raise

    except RuntimeError as exc:
        if working_directory:
            shutil.rmtree(
                working_directory,
                ignore_errors=True,
            )

        if output_directory:
            shutil.rmtree(
                output_directory,
                ignore_errors=True,
            )

        if token:
            _finish_download_job(
                token,
                "failed",
                "Download failed.",
            )

        raise HTTPException(
            status_code=404,
            detail="Download failed.",
        ) from exc

    except Exception as exc:
        if working_directory:
            shutil.rmtree(
                working_directory,
                ignore_errors=True,
            )

        if output_directory:
            shutil.rmtree(
                output_directory,
                ignore_errors=True,
            )

        if token:
            _finish_download_job(
                token,
                "failed",
                "Download failed.",
            )

        raise HTTPException(
            status_code=502,
            detail="Download failed.",
        ) from exc


def _cleanup_paths(
    output_directory: Path,
    working_directory: Path | None,
):
    shutil.rmtree(
        output_directory,
        ignore_errors=True,
    )

    if working_directory:
        shutil.rmtree(
            working_directory,
            ignore_errors=True,
        )


# ============================================================
# Whole-album download plan
# ============================================================

@router.get(
    "/{album_id}/download",
)
def download_album(
    request: Request,
    album_id: int,
):
    """
    Return the complete album download plan.

    A complete album can contain many ~1.8 GB chunks, so the
    API does not attempt to build one enormous archive inside
    a normal request. The browser can download each chunk
    independently using the per-chunk endpoint.
    """
    user = get_current_user(
        request
    )

    album = get_owned_album(
        user,
        album_id,
    )

    try:
        return get_album_download_plan(
            user,
            album,
        )

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc
