"""Web application. Requests authenticate, validate and queue; worker.py moves bytes."""
from __future__ import annotations
import hashlib, os, shutil, tempfile, uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from auth import create_authorization_url, encrypt_credentials, encrypt_string, exchange_code_for_credentials, get_google_user_info
from backup_engine import GITHUB_MAX_ASSET_BYTES, estimate_tar_size, sanitize_album_name, sanitize_filename
from database import Album, BackupJob, Chunk, MediaItem, SessionLocal, User, init_db
from album_routes import router as album_api_router
from worker_notify import notify_worker

load_dotenv(); APP_NAME = "MyGoodOldPhotos"
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "").strip() or "development-only-change-me"
PLATFORM_STORAGE_DISPLAY_NAME = os.getenv("PLATFORM_STORAGE_DISPLAY_NAME", "GoodOldStorage")
UPLOAD_ROOT = Path(os.getenv("UPLOAD_TEMP_DIR", tempfile.gettempdir())) / "mygoodoldphotos-uploads"; UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = int(os.getenv("MAX_LOCAL_FILE_BYTES", str(GITHUB_MAX_ASSET_BYTES - 2_000_000)))
PICKER_LIMIT = min(2000, max(1, int(os.getenv("PICKER_SELECTION_LIMIT", "2000"))))
app = FastAPI(title=APP_NAME, version="2.0.0")
app.add_middleware(SessionMiddleware, secret_key=APP_SECRET_KEY, session_cookie="mygoodoldphotos_session", max_age=2592000, same_site="lax", https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
app.include_router(album_api_router)

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse
import logging
logger = logging.getLogger("mygoodoldphotos")

@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    logger.exception("Unhandled request error: %s %s", request.method, request.url.path)
    return JSONResponse({"detail":"The request could not be completed."}, status_code=500)

@app.on_event("startup")
def startup(): init_db()

def current_user(request: Request) -> User:
    user_id = request.session.get("user_id")
    if not user_id: raise HTTPException(401, "Please sign in with Google first.")
    db = SessionLocal()
    try:
        user = db.get(User, int(user_id))
        if not user:
            request.session.clear(); raise HTTPException(401, "Your sign-in has expired. Please sign in again.")
        db.expunge(user); return user
    finally: db.close()

def owned_album(user: User, album_id: int) -> Album:
    db = SessionLocal()
    try:
        album = db.query(Album).filter(Album.id == album_id, Album.user_id == user.id).first()
        if not album: raise HTTPException(404, "Album not found.")
        db.expunge(album); return album
    finally: db.close()

def user_albums(user: User) -> list[Album]:
    db = SessionLocal()
    try:
        albums = (
            db.query(Album)
            .filter(Album.user_id == user.id)
            .order_by(Album.name.asc(), Album.id.asc())
            .all()
        )
        for album in albums:
            db.expunge(album)
        return albums
    finally:
        db.close()

def render(request: Request, name: str, **context):
    context.update(app_name=APP_NAME, user=None)
    try: context["user"] = current_user(request)
    except HTTPException: pass
    return templates.TemplateResponse(request, name, context)


def public_storage_label(user: User) -> str:
    if user.storage_mode == "platform":
        username = user.email_prefix or "user"
        return f"{PLATFORM_STORAGE_DISPLAY_NAME}/{username}"
    if user.github_username and user.github_repo_name:
        return f"{user.github_username}/{user.github_repo_name}"
    return "Not configured yet"

def safe_http_error(message: str, status_code: int) -> HTTPException:
    return HTTPException(status_code, message)

@app.get("/health")
def health(): return {"status": "ok"}

@app.get("/")
def dashboard(request: Request):
    if not request.session.get("user_id"):
        return render(request, "landing.html")
    user = current_user(request); db = SessionLocal()
    try:
        jobs = db.query(BackupJob).filter_by(user_id=user.id).order_by(BackupJob.id.desc()).limit(12).all()
        storage = public_storage_label(user)
        return render(request, "dashboard.html", jobs=jobs, album_count=db.query(Album).filter_by(user_id=user.id).count(), storage=storage)
    finally: db.close()

@app.get("/backup")
def backup_page(request: Request):
    user = current_user(request)
    albums = user_albums(user)

    selected_album_id = None
    selected_album_name = None
    raw_album_id = request.query_params.get("album_id")

    if raw_album_id:
        try:
            requested_album_id = int(raw_album_id)
        except (TypeError, ValueError):
            requested_album_id = None

        if requested_album_id is not None:
            selected_album = next(
                (album for album in albums if album.id == requested_album_id),
                None,
            )
            if selected_album is not None:
                selected_album_id = selected_album.id
                selected_album_name = selected_album.name

    return render(
        request,
        "backup.html",
        picker_limit=PICKER_LIMIT,
        max_upload_bytes=MAX_UPLOAD_BYTES,
        albums=albums,
        selected_album_id=selected_album_id,
        selected_album_name=selected_album_name,
    )

@app.get("/auth/google")
def google_login(request: Request):
    """Start OAuth on the same loopback host registered as the callback.

    Browsers keep session cookies host-only. Without this normalization, a
    flow started at 127.0.0.1 and returned to localhost (or the reverse)
    loses its one-time OAuth state before the callback.
    """
    configured = urlsplit(os.getenv("GOOGLE_REDIRECT_URI", ""))
    requested = request.url
    local_hosts = {"localhost", "127.0.0.1"}
    if (configured.hostname in local_hosts and requested.hostname in local_hosts
            and configured.hostname != requested.hostname):
        start_url = urlunsplit((configured.scheme, configured.netloc, requested.path, requested.query, ""))
        return RedirectResponse(start_url, status_code=307)
    return RedirectResponse(create_authorization_url(request))

@app.get("/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = ""):
    if not code or not state: raise HTTPException(400, "Google did not return an authorization response.")
    credentials = exchange_code_for_credentials(request, code, state); info = get_google_user_info(credentials)
    subject, email = info.get("subject_id"), info.get("email")
    if not subject or not email: raise HTTPException(400, "Google did not return a usable account identity.")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.google_subject_id == subject).first()
        if not user:
            user = User(google_subject_id=subject, email=email, email_prefix=email.split("@", 1)[0], display_name=info.get("name") or email, storage_mode="platform"); db.add(user)
        else: user.email, user.display_name = email, info.get("name") or user.display_name
        user.encrypted_google_token = encrypt_credentials(credentials); db.commit(); db.refresh(user)
        request.session.clear(); request.session["user_id"] = user.id
    finally: db.close()
    return RedirectResponse("/", 303)

@app.post("/backup/local")
def queue_local_backup(
    request: Request,
    album_name: str = Form(""),
    album_id: int | None = Form(None),
    files: list[UploadFile] = File(...),
):
    user = current_user(request)

    if album_id is not None:
        album = owned_album(user, album_id)
    else:
        try:
            album_name = sanitize_album_name(album_name)
        except ValueError as exc:
            raise HTTPException(
                400,
                "Enter an album name for a new album.",
            ) from exc

        db = SessionLocal()
        try:
            album = Album(
                user_id=user.id,
                name=album_name,
            )
            db.add(album)
            db.commit()
            db.refresh(album)

            # The album must be committed before this session closes.
            # The backup job is created in a separate transaction below.
            db.expunge(album)
        finally:
            db.close()

    if not files:
        raise HTTPException(
            400,
            "Choose at least one non-empty file.",
        )

    folder = (
        UPLOAD_ROOT
        / uuid.uuid4().hex
    )
    folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    db = SessionLocal()

    try:
        job = BackupJob(
            user_id=user.id,
            album_id=album.id,
            source="local",
            status="queued",
            total_items=len(files),
        )
        db.add(job)
        db.flush()

        for index, upload in enumerate(
            files,
            1,
        ):
            filename = sanitize_filename(
                upload.filename
                or f"file-{index}"
            )

            destination = (
                folder
                / f"{index:06d}_{filename}"
            )

            digest = hashlib.sha256()
            size = 0

            with destination.open("xb") as target:
                while block := upload.file.read(
                    1024 * 1024
                ):
                    size += len(block)

                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            413,
                            f"{filename} exceeds the GitHub asset limit.",
                        )

                    target.write(block)
                    digest.update(block)

            if not size:
                raise HTTPException(
                    400,
                    f"{filename} is empty and cannot be backed up.",
                )

            if estimate_tar_size(
                [{"size": size}]
            ) >= GITHUB_MAX_ASSET_BYTES:
                raise HTTPException(
                    413,
                    f"{filename} cannot safely fit in a GitHub release asset.",
                )

            db.add(
                MediaItem(
                    job_id=job.id,
                    google_media_id=(
                        f"local:{job.id}:"
                        f"{uuid.uuid4().hex}"
                    ),
                    filename=filename,
                    mime_type=(
                        upload.content_type
                        or "application/octet-stream"
                    ),
                    size=size,
                    sha256=digest.hexdigest(),
                    local_path=str(destination),
                    status="pending",
                )
            )

        db.commit()
        notify_worker(job.id)

        return JSONResponse(
            {
                "status": "queued",
                "job_id": job.id,
                "album_id": album.id,
            },
            status_code=202,
        )

    except Exception:
        db.rollback()

        # If a new album was created in this request, remove the orphan
        # album after rolling back the job transaction.
        if album_id is None:
            cleanup_db = SessionLocal()
            try:
                new_album = cleanup_db.get(
                    Album,
                    album.id,
                )
                if new_album:
                    cleanup_db.delete(new_album)
                    cleanup_db.commit()
            except Exception:
                cleanup_db.rollback()
            finally:
                cleanup_db.close()

        shutil.rmtree(
            folder,
            ignore_errors=True,
        )
        raise

    finally:
        for upload in files:
            upload.file.close()

        db.close()


@app.post("/backup/google")
def begin_google_picker(
    request: Request,
    album_name: str = Form(""),
    album_id: int | None = Form(None),
):
    user = current_user(request)

    if album_id is not None:
        album = owned_album(
            user,
            album_id,
        )
    else:
        try:
            album_name = sanitize_album_name(
                album_name
            )
        except ValueError as exc:
            raise HTTPException(
                400,
                "Enter an album name for a new album.",
            ) from exc

        db = SessionLocal()
        try:
            album = Album(
                user_id=user.id,
                name=album_name,
            )
            db.add(album)
            db.commit()
            db.refresh(album)
            db.expunge(album)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    if not user.encrypted_google_token:
        raise HTTPException(
            400,
            "Google authorization has expired. Please sign in again.",
        )

    from backup_engine import get_google_client_for_user

    try:
        picker = (
            get_google_client_for_user(user)
            .create_picker_session(
                PICKER_LIMIT
            )
        )
    except Exception:
        # Do not leave an empty album behind if Picker creation fails.
        if album_id is None:
            cleanup_db = SessionLocal()
            try:
                new_album = cleanup_db.get(
                    Album,
                    album.id,
                )
                if new_album:
                    cleanup_db.delete(new_album)
                    cleanup_db.commit()
            except Exception:
                cleanup_db.rollback()
            finally:
                cleanup_db.close()

        raise HTTPException(
            502,
            "Could not start Google Photos backup. Check the server logs.",
        )

    db = SessionLocal()
    try:
        job = BackupJob(
            user_id=user.id,
            album_id=album.id,
            source="google",
            status="awaiting_selection",
            picker_session_id=picker["id"],
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        return {
            "job_id": job.id,
            "album_id": album.id,
            "picker_uri": picker.get(
                "pickerUri"
            ),
            "status": job.status,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@app.post("/backup/google/{job_id}/queue")
def queue_google_backup(request: Request, job_id: int):
    user = current_user(request); db = SessionLocal()
    try:
        job = db.query(BackupJob).filter_by(id=job_id, user_id=user.id, source="google").first()
        if not job: raise HTTPException(404, "Backup job not found.")
        if job.status != "awaiting_selection": raise HTTPException(409, "This selection has already been queued.")
        job.status = "queued"; db.commit(); notify_worker(job.id); return {"status":"queued", "job_id":job.id, "album_id":job.album_id}
    finally: db.close()

@app.post("/backup/google/{job_id}/selection-status")
def queue_google_when_selection_finishes(request: Request, job_id: int):
    """Turn a completed Picker selection into a worker job.

    Picker does not redirect back to an application callback. The browser
    makes this small request only while its Picker window is open; no worker
    slot is held and no media is downloaded until the selection is complete.
    """
    user = current_user(request); db = SessionLocal()
    try:
        job = db.query(BackupJob).filter_by(id=job_id, user_id=user.id, source="google").first()
        if not job: raise HTTPException(404, "Backup job not found.")
        if job.status != "awaiting_selection": return {"status": job.status, "job_id": job.id}
        from backup_engine import get_google_client_for_user
        session = get_google_client_for_user(user).get_picker_session(job.picker_session_id)
        if session.get("mediaItemsSet"):
            job.status = "queued"; db.commit(); notify_worker(job.id)
        return {"status": job.status, "job_id": job.id, "album_id": job.album_id}
    finally: db.close()

@app.get("/api/jobs")
def jobs_api(request: Request):
    user = current_user(request); db = SessionLocal()
    try:
        rows = db.query(BackupJob, Album).join(Album).filter(BackupJob.user_id == user.id).order_by(BackupJob.id.desc()).limit(30).all()
        payload=[]
        for j,a in rows:
            total = int(j.total_bytes or 0)
            processed = int(j.processed_bytes or 0)
            current_total = int(j.current_total_bytes or 0)
            current = int(j.current_bytes or 0)
            overall_base = processed
            overall_value = overall_base + current
            overall_percent = (overall_value / total * 100.0) if total else (100.0 if j.status == "completed" else 0.0)
            current_percent = (current / current_total * 100.0) if current_total else 0.0
            payload.append({"id":j.id,"album":a.name,"source":j.source,"status":j.status,"completed":j.completed_items,"total":j.total_items,"part":j.current_part,"operation":j.current_operation,"filename":j.current_filename,"current_bytes":current,"current_total_bytes":current_total,"processed_bytes":processed,"total_bytes":total,"current_percent":round(current_percent,1),"overall_percent":round(min(overall_percent,100.0),1),"message":j.user_message})
        return {"jobs":payload}
    finally: db.close()

@app.get("/api/jobs/{job_id}/events")
def job_events(request: Request, job_id: int):
    user = current_user(request)

    def stream():
        import json, time
        while True:
            db = SessionLocal()
            try:
                row = db.query(BackupJob, Album).join(Album).filter(BackupJob.id == job_id, BackupJob.user_id == user.id).first()
                if not row:
                    yield "event: error\ndata: {\"message\":\"Job not found.\"}\n\n"
                    return
                j,a = row
                total=int(j.total_bytes or 0); current=int(j.current_bytes or 0); current_total=int(j.current_total_bytes or 0); processed=int(j.processed_bytes or 0)
                overall=((processed+current)/total*100.0) if total else (100.0 if j.status == "completed" else 0.0)
                current_pct=(current/current_total*100.0) if current_total else 0.0
                data={"id":j.id,"album":a.name,"status":j.status,"source":j.source,"operation":j.current_operation,"filename":j.current_filename,"current_bytes":current,"current_total_bytes":current_total,"processed_bytes":processed,"total_bytes":total,"current_percent":round(min(current_pct,100.0),1),"overall_percent":round(min(overall,100.0),1),"completed_items":j.completed_items,"total_items":j.total_items,"current_part":j.current_part,"message":j.user_message}
                yield f"data: {json.dumps(data)}\n\n"
                if j.status in {"completed","failed"}: return
            finally:
                db.close()
            time.sleep(1.0)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.post("/jobs/{job_id}/retry")
def retry_failed_job(request: Request, job_id: int):
    """Explicitly retry a recoverable worker failure without re-uploading input."""
    user = current_user(request); db = SessionLocal()
    try:
        job = db.query(BackupJob).filter_by(id=job_id, user_id=user.id).first()
        if not job: raise HTTPException(404, "Backup job not found.")
        if job.status != "failed": raise HTTPException(409, "Only failed jobs can be retried.")
        job.status = "queued"; job.error = None; job.user_message = None; job.started_at = None; job.completed_at = None; job.current_bytes = 0; job.current_total_bytes = 0; job.processed_bytes = 0; job.current_filename = None; job.current_operation = None
        db.commit(); notify_worker(job.id)
    finally: db.close()
    return RedirectResponse("/", 303)

@app.get("/albums/{album_id}")
def album_page(request: Request, album_id: int):
    user = current_user(request); album = owned_album(user, album_id); db = SessionLocal()
    try:
        chunks = db.query(Chunk).filter_by(album_id=album.id).order_by(Chunk.part_number).all(); media = db.query(MediaItem).join(BackupJob).filter(BackupJob.album_id == album.id).count()
        return render(request, "album.html", album=album, chunks=chunks, media_count=media)
    finally: db.close()

@app.post("/albums/{album_id}/delete")
def delete_album(request: Request, album_id: int, confirmation: str = Form(...)):
    """Remove only DB-associated GitHub assets; it never contacts Google Photos."""
    user = current_user(request); album = owned_album(user, album_id)
    if confirmation != album.name: raise HTTPException(400, "Type the album name exactly to confirm deletion.")
    from storage import ensure_user_repository
    db = SessionLocal()
    try:
        rows = db.query(Chunk).filter_by(album_id=album.id).all()
        github, _ = ensure_user_repository(user)
        release_ids = {row.github_release_id for row in rows}
        for row in rows:
            try: github.delete_asset({"id": row.github_asset_id})
            except RuntimeError as exc:
                if "404" not in str(exc): raise
        # A release can contain chunks from several albums. Delete it only if
        # this album owns every chunk our database records in that release.
        for release_id in release_ids:
            if db.query(Chunk).filter(Chunk.github_release_id == release_id, Chunk.album_id != album.id).count() == 0:
                try: github.delete_release(release_id)
                except RuntimeError as exc:
                    if "404" not in str(exc): raise
        db.delete(db.get(Album, album.id)); db.commit()
    except Exception:
        db.rollback(); raise
    finally: db.close()
    return RedirectResponse("/", 303)

def _safe_remove_local_file(path_value: str | None) -> None:
    """
    Remove an app-owned temporary media file.

    Local uploads live below UPLOAD_ROOT.
    Google backup files use TemporaryDirectory() with the fixed
    ``mygoodoldphotos_backup_`` prefix below the system temp directory.
    """
    if not path_value:
        return

    try:
        target = Path(path_value).resolve()
        upload_root = UPLOAD_ROOT.resolve()
        system_tmp = Path(tempfile.gettempdir()).resolve()

        allowed = False

        try:
            target.relative_to(upload_root)
            allowed = True
        except ValueError:
            pass

        if not allowed:
            try:
                relative = target.relative_to(system_tmp)
                parts = relative.parts
                allowed = (
                    len(parts) >= 2
                    and parts[0].startswith("mygoodoldphotos_backup_")
                )
            except ValueError:
                pass

        if not allowed:
            logger.warning(
                "Skipping unsafe local cleanup path: %r",
                path_value,
            )
            return

    except OSError:
        logger.warning(
            "Skipping invalid local cleanup path: %r",
            path_value,
        )
        return

    if target.is_file():
        try:
            target.unlink()
        except FileNotFoundError:
            return

    parent = target.parent
    try:
        if (
            parent != upload_root
            and parent != system_tmp
            and parent.name.startswith("mygoodoldphotos_backup_")
            and parent.parent == system_tmp
        ):
            parent.rmdir()
    except OSError:
        pass


def _delete_user_github_data(user: User) -> None:
    """Delete all MyGoodOldPhotos backup releases from the user's configured repo."""
    if not user.github_username or not user.github_repo_name:
        return

    from storage import get_user_github_client

    github = get_user_github_client(user)
    github.set_repository(
        user.github_username,
        user.github_repo_name,
    )

    releases = github.list_backup_releases()

    logger.info(
        "Account deletion: deleting %d backup release(s) from %s/%s for user id=%s",
        len(releases),
        user.github_username,
        user.github_repo_name,
        user.id,
    )

    for release in releases:
        logger.info(
            "Account deletion: deleting release %s (%s) for user id=%s",
            release.get("tag_name"),
            release.get("name"),
            user.id,
        )
        github.delete_complete_release(release)

    remaining = github.list_backup_releases()
    if remaining:
        raise RuntimeError(
            "GitHub backup releases still remain after account deletion cleanup."
        )

    # The repository itself was created/managed by MyGoodOldPhotos for this
    # account. Permanently remove it after all backup releases are gone.
    owner = user.github_username
    repo = user.github_repo_name

    logger.info(
        "Account deletion: deleting GitHub repository %s/%s for user id=%s",
        owner,
        repo,
        user.id,
    )

    github.delete_repository(
        owner=owner,
        repo=repo,
    )

    if github.repository_exists(owner, repo):
        raise RuntimeError(
            "GitHub repository still exists after account deletion."
        )


@app.get("/settings")
def settings(request: Request):
    user = current_user(request); storage = public_storage_label(user)
    return render(request, "settings.html", storage=storage)


@app.post("/account/delete")
def delete_account(request: Request, confirmation: str = Form(...)):
    """Permanently erase the authenticated user's app data and app-managed GitHub backups."""
    user = current_user(request)

    if confirmation != "DELETE MY ACCOUNT":
        raise HTTPException(
            400,
            "Type DELETE MY ACCOUNT exactly to confirm account deletion.",
        )

    db = SessionLocal()
    try:
        db_user = db.get(User, user.id)
        if not db_user:
            request.session.clear()
            return RedirectResponse("/", 303)

        jobs = (
            db.query(BackupJob)
            .filter(BackupJob.user_id == db_user.id)
            .all()
        )

        running_jobs = [
            job.id
            for job in jobs
            if job.status == "running"
        ]

        if running_jobs:
            raise HTTPException(
                409,
                "A backup is still running. Wait for it to finish before deleting your account.",
            )

        local_paths = [
            item.local_path
            for job in jobs
            for item in job.media_items
            if item.local_path
        ]

        # Prevent the worker from claiming queued jobs while deletion is in progress.
        for job in jobs:
            if job.status in {"queued", "awaiting_selection"}:
                job.status = "cancelled"
                job.error = "Account deletion requested."

        db.commit()

        # Delete GitHub backups first. If this fails, the DB account is preserved
        # so the user can retry instead of losing the database mapping.
        _delete_user_github_data(db_user)

        # Remove any remaining local temporary originals.
        for local_path in local_paths:
            _safe_remove_local_file(local_path)

        # Delete child rows explicitly in dependency order.
        album_ids = [
            row[0]
            for row in db.query(Album.id)
            .filter(Album.user_id == db_user.id)
            .all()
        ]

        job_ids = [
            row[0]
            for row in db.query(BackupJob.id)
            .filter(BackupJob.user_id == db_user.id)
            .all()
        ]

        if job_ids:
            db.query(MediaItem).filter(
                MediaItem.job_id.in_(job_ids)
            ).delete(synchronize_session=False)

        if album_ids:
            db.query(Chunk).filter(
                Chunk.album_id.in_(album_ids)
            ).delete(synchronize_session=False)

        if job_ids:
            db.query(BackupJob).filter(
                BackupJob.id.in_(job_ids)
            ).delete(synchronize_session=False)

        if album_ids:
            db.query(Album).filter(
                Album.id.in_(album_ids)
            ).delete(synchronize_session=False)

        db.query(User).filter(
            User.id == db_user.id
        ).delete(synchronize_session=False)

        db.commit()

        request.session.clear()
        return RedirectResponse("/", 303)

    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception(
            "Account deletion failed for user id=%s",
            user.id,
        )
        raise HTTPException(
            500,
            "Account deletion could not be completed. No account record was removed.",
        )
    finally:
        db.close()


@app.post("/settings/storage")
def save_storage_settings(request: Request, storage_mode: str = Form(...), github_token: str = Form("")):
    user = current_user(request)
    if storage_mode not in {"platform", "personal"}: raise HTTPException(400, "Unknown storage mode.")
    if storage_mode == "personal" and not github_token.strip(): raise HTTPException(400, "A GitHub personal access token is required for personal storage.")
    db = SessionLocal()
    try:
        row = db.get(User, user.id); row.storage_mode = storage_mode
        if storage_mode == "personal": row.encrypted_github_token = encrypt_string(github_token.strip())
        row.github_username = row.github_repo_name = row.github_repo_id = None
        db.commit()
    finally: db.close()
    return RedirectResponse("/settings", 303)

@app.post("/logout")
def logout(request: Request): request.session.clear(); return RedirectResponse("/", 303)
