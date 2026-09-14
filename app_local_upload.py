from __future__ import annotations

import hashlib
import html
import os
import secrets
import shutil
import tempfile
import uuid
from pathlib import Path

from dotenv import load_dotenv

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)

from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from starlette.middleware.sessions import (
    SessionMiddleware,
)

from auth import (
    clear_google_session,
    create_authorization_url,
    credentials_to_dict,
    encrypt_credentials,
    encrypt_string,
    exchange_code_for_credentials,
    get_google_user_info,
    get_session_credentials,
)

from database import (
    Album,
    BackupJob,
    MediaItem,
    SessionLocal,
    User,
    init_db,
)

from github import GitHubClient

from google_photos import (
    GooglePhotosClient,
)

from album_routes import router as album_router

from backup_engine import (
    MAX_ASSETS_PER_RELEASE,
    build_asset_name,
    calculate_sha256,
    choose_chunk_items,
    create_chunk_archive,
    get_next_part_number,
    persist_uploaded_chunk,
)


# ============================================================
# Environment
# ============================================================

load_dotenv()


# ============================================================
# Configuration
# ============================================================

APP_NAME = "MyGoodOldPhotos"

APP_SECRET_KEY = os.getenv(
    "APP_SECRET_KEY"
)

PLATFORM_GITHUB_TOKEN = os.getenv(
    "PLATFORM_GITHUB_TOKEN"
)


if not APP_SECRET_KEY:

    APP_SECRET_KEY = secrets.token_urlsafe(
        32
    )


COOKIE_SECURE = (
    os.getenv(
        "COOKIE_SECURE",
        "false",
    ).lower()
    == "true"
)


# ============================================================
# FastAPI
# ============================================================

app = FastAPI(
    title=APP_NAME,
    description=(
        "Google Photos → GitHub Releases "
        "backup application"
    ),
    version="1.0.0",
)

app.include_router(
    album_router
)


# ============================================================
# Session
# ============================================================

app.add_middleware(
    SessionMiddleware,
    secret_key=APP_SECRET_KEY,
    session_cookie=(
        "mygoodoldphotos_session"
    ),
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
    https_only=COOKIE_SECURE,
)


# ============================================================
# Startup
# ============================================================

@app.on_event("startup")
def startup():

    init_db()


# ============================================================
# HTML helpers
# ============================================================

def escape(
    value,
) -> str:

    return html.escape(
        str(value)
    )


def page(
    title: str,
    content: str,
) -> str:

    return f"""
<!DOCTYPE html>

<html lang="en">

<head>

    <meta charset="UTF-8">

    <meta
        name="viewport"
        content="width=device-width, initial-scale=1.0"
    >

    <title>
        {escape(title)}
        -
        {APP_NAME}
    </title>


    <style>

        * {{
            box-sizing: border-box;
        }}


        body {{
            margin: 0;

            font-family:
                -apple-system,
                BlinkMacSystemFont,
                "Segoe UI",
                sans-serif;

            background: #f5f6f8;

            color: #1f2937;
        }}


        nav {{
            background: #111827;

            color: white;

            padding: 16px 24px;

            display: flex;

            justify-content: space-between;

            align-items: center;
        }}


        nav a {{
            color: white;

            text-decoration: none;

            margin-left: 18px;
        }}


        .container {{
            max-width: 1000px;

            margin: 40px auto;

            padding: 0 20px;
        }}


        .card {{
            background: white;

            border-radius: 12px;

            padding: 24px;

            margin-bottom: 20px;

            box-shadow:
                0 2px 8px
                rgba(
                    0,
                    0,
                    0,
                    0.08
                );
        }}


        h1,
        h2,
        h3 {{
            margin-top: 0;
        }}


        input {{
            width: 100%;

            padding: 12px;

            margin-top: 8px;

            margin-bottom: 16px;

            border:
                1px solid
                #d1d5db;

            border-radius: 8px;

            font-size: 16px;
        }}


        button {{
            padding: 12px 18px;

            border: none;

            border-radius: 8px;

            background: #111827;

            color: white;

            font-size: 15px;

            cursor: pointer;
        }}


        button:hover {{
            opacity: 0.9;
        }}


        .google {{
            background: #4285f4;
        }}


        .danger {{
            background: #b91c1c;
        }}


        .success {{
            padding: 12px;

            background: #dcfce7;

            color: #166534;

            border-radius: 8px;

            margin-bottom: 16px;
        }}


        .error {{
            padding: 12px;

            background: #fee2e2;

            color: #991b1b;

            border-radius: 8px;

            margin-bottom: 16px;
        }}


        .info {{
            padding: 12px;

            background: #eff6ff;

            color: #1e40af;

            border-radius: 8px;

            margin-bottom: 16px;
        }}


        .warning {{
            padding: 12px;

            background: #fef3c7;

            color: #92400e;

            border-radius: 8px;

            margin-bottom: 16px;
        }}


        .muted {{
            color: #6b7280;
        }}


        .large {{
            font-size: 32px;

            font-weight: 700;
        }}


        .center {{
            text-align: center;
        }}


        .album {{
            border:
                1px solid
                #e5e7eb;

            border-radius: 10px;

            padding: 18px;

            margin-bottom: 12px;
        }}


        .row {{
            display: flex;

            align-items: center;

            justify-content: space-between;

            gap: 16px;

            flex-wrap: wrap;
        }}


        .spinner {{
            width: 28px;

            height: 28px;

            border:
                4px solid
                #e5e7eb;

            border-top-color:
                #111827;

            border-radius: 50%;

            animation:
                spin 1s linear infinite;

            margin: 20px auto;
        }}


        @keyframes spin {{
            to {{
                transform: rotate(360deg);
            }}
        }}


    </style>

</head>


<body>

<nav>

    <div>
        <strong>
            {APP_NAME}
        </strong>
    </div>


    <div>

        <a href="/">
            Dashboard
        </a>

        <a href="/backup">
            New Backup
        </a>

        <a href="/settings">
            Settings
        </a>

        <a href="/logout">
            Logout
        </a>

    </div>

</nav>


<div class="container">

    {content}

</div>


</body>

</html>
"""


# ============================================================
# Current user
# ============================================================

def get_current_user(
    request: Request,
):

    user_id = request.session.get(
        "user_id"
    )

    if not user_id:
        return None

    db = SessionLocal()

    try:

        user = db.get(
            User,
            int(user_id),
        )

        if not user:
            return None

        db.expunge(
            user
        )

        return user

    finally:

        db.close()


def require_google_login(
    request: Request,
):

    user = get_current_user(
        request
    )

    if not user:

        raise HTTPException(
            status_code=401,
            detail=(
                "You must sign in with Google."
            ),
        )

    return user


# ============================================================
# Storage helpers
# ============================================================

def sanitize_repository_name(
    value: str,
) -> str:

    import re

    value = value.strip()

    value = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        value,
    )

    value = value.strip(
        ".-"
    )

    if not value:

        raise RuntimeError(
            "Could not generate a valid "
            "GitHub repository name."
        )

    return value


def find_platform_repository_name(
    github: GitHubClient,
    email_prefix: str,
) -> str:

    base_name = (
        sanitize_repository_name(
            email_prefix
        )
    )

    owner = (
        github.get_authenticated_username()
    )

    candidate = base_name

    counter = 1

    while github.repository_exists(
        owner,
        candidate,
    ):

        candidate = (
            f"{base_name}-{counter}"
        )

        counter += 1

    return candidate


def ensure_user_repository(
    user: User,
):

    # --------------------------------------------------------
    # Platform storage
    # --------------------------------------------------------

    if user.storage_mode == "platform":

        if not PLATFORM_GITHUB_TOKEN:

            raise RuntimeError(
                "PLATFORM_GITHUB_TOKEN is not configured."
            )

        github = GitHubClient(
            PLATFORM_GITHUB_TOKEN
        )

        github_username = (
            github.get_authenticated_username()
        )

        if (
            user.github_username
            and user.github_repo_name
        ):

            repository = (
                github.get_repository(
                    user.github_username,
                    user.github_repo_name,
                )
            )

            github.set_repository(
                user.github_username,
                user.github_repo_name,
            )

            return github, repository

        repo_name = (
            find_platform_repository_name(
                github,
                user.email_prefix,
            )
        )

    # --------------------------------------------------------
    # Personal storage
    # --------------------------------------------------------

    elif user.storage_mode == "personal":

        if not user.encrypted_github_token:

            raise RuntimeError(
                "GitHub PAT is not configured."
            )

        from auth import decrypt_string

        github_pat = (
            decrypt_string(
                user.encrypted_github_token
            )
        )

        github = GitHubClient(
            github_pat
        )

        github_username = (
            github.get_authenticated_username()
        )

        if (
            user.github_username
            and user.github_repo_name
        ):

            repository = (
                github.get_repository(
                    user.github_username,
                    user.github_repo_name,
                )
            )

            github.set_repository(
                user.github_username,
                user.github_repo_name,
            )

            return github, repository

        repo_name = (
            sanitize_repository_name(
                user.email_prefix
            )
        )

    else:

        raise RuntimeError(
            f"Unknown storage mode: "
            f"{user.storage_mode}"
        )

    # --------------------------------------------------------
    # Create repository
    # --------------------------------------------------------

    repository = (
        github.ensure_private_repository(
            repo_name
        )
    )

    # --------------------------------------------------------
    # Save repository mapping
    # --------------------------------------------------------

    db = SessionLocal()

    try:

        db_user = db.get(
            User,
            user.id,
        )

        if not db_user:

            raise RuntimeError(
                "User account no longer exists."
            )

        db_user.github_username = (
            repository[
                "owner"
            ][
                "login"
            ]
        )

        db_user.github_repo_name = (
            repository[
                "name"
            ]
        )

        db_user.github_repo_id = (
            repository[
                "id"
            ]
        )

        db.commit()

        github.set_repository(
            db_user.github_username,
            db_user.github_repo_name,
        )

    finally:

        db.close()

    return github, repository


# ============================================================
# Local file backup
# ============================================================

LOCAL_UPLOAD_ROOT = Path(
    tempfile.gettempdir()
) / "mygoodoldphotos_local_uploads"

LOCAL_UPLOAD_ROOT.mkdir(
    parents=True,
    exist_ok=True,
)


def _local_upload_sha256_and_size(
    file_path: Path,
):
    sha256 = hashlib.sha256()
    size = 0

    with file_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            sha256.update(chunk)
            size += len(chunk)

    return size, sha256.hexdigest()


def _next_github_release_number(
    github: GitHubClient,
) -> int:
    releases = github.list_backup_releases()

    if not releases:
        return 1

    numbers = []

    for release in releases:
        tag = release.get("tag_name", "")

        try:
            numbers.append(
                int(tag.rsplit("-", 1)[-1])
            )
        except (TypeError, ValueError):
            continue

    return max(numbers, default=0) + 1


def _process_local_upload_job(
    job_id: int,
    upload_directory: str,
    media_ids: list[str],
):
    """
    Process a local-file backup after the HTTP upload has finished.

    The original local files are copied byte-for-byte to temporary disk,
    then placed in an uncompressed TAR. No media conversion or compression
    is performed.
    """

    db = SessionLocal()

    try:
        job = db.get(
            BackupJob,
            job_id,
        )

        if not job:
            raise RuntimeError(
                f"Local backup job {job_id} does not exist."
            )

        user = db.get(
            User,
            job.user_id,
        )

        album = db.get(
            Album,
            job.album_id,
        )

        if not user or not album:
            raise RuntimeError(
                "Local backup job references missing user or album."
            )

        job.status = "running"
        job.error = None
        db.commit()

        github, _repository = ensure_user_repository(
            user
        )

        pending_items = []

        for index, media_id in enumerate(
            media_ids,
            start=1,
        ):
            media = (
                db.query(MediaItem)
                .filter(
                    MediaItem.job_id == job.id,
                    MediaItem.google_media_id == media_id,
                )
                .first()
            )

            if not media:
                raise RuntimeError(
                    f"Local media row {media_id} was not found."
                )

            local_path = Path(
                media.local_path or ""
            )

            if not local_path.is_file():
                raise FileNotFoundError(
                    f"Uploaded local file disappeared: {local_path}"
                )

            pending_items.append({
                "media_id": media.google_media_id,
                "filename": media.filename,
                "mime_type": media.mime_type,
                "local_path": str(local_path),
                "size": int(media.size),
                "sha256": media.sha256,
                "status": "downloaded",
            })

        job.total_items = len(pending_items)
        job.completed_items = 0
        db.commit()

        part_number = get_next_part_number(
            db,
            album,
        )

        release = None
        release_asset_count = MAX_ASSETS_PER_RELEASE
        release_number = None

        with tempfile.TemporaryDirectory(
            prefix="mygoodoldphotos_local_backup_"
        ) as temporary_directory:

            temp_dir = Path(
                temporary_directory
            )

            while pending_items:
                chunk_items = choose_chunk_items(
                    pending_items
                )

                if not chunk_items:
                    raise RuntimeError(
                        "Unable to create a local backup chunk."
                    )

                # New release for this local backup when the previous
                # release reaches GitHub's asset-count limit.
                if (
                    release is None
                    or release_asset_count >= MAX_ASSETS_PER_RELEASE
                ):
                    if release_number is None:
                        release_number = _next_github_release_number(
                            github
                        )
                    else:
                        release_number += 1

                    release = github.create_release(
                        album.name,
                        release_number,
                    )

                    release_asset_count = 0

                chunk_info = create_chunk_archive(
                    chunk_items,
                    album.name,
                    part_number,
                    temp_dir,
                )

                archive_path = Path(
                    chunk_info["path"]
                )

                release, asset = github.upload_asset(
                    release,
                    archive_path,
                    chunk_info["asset_name"],
                )

                persist_uploaded_chunk(
                    db=db,
                    album=album,
                    chunk_items=chunk_items,
                    release=release,
                    asset=asset,
                    part_number=part_number,
                    archive_size=chunk_info["size"],
                    archive_hash=chunk_info["sha256"],
                )

                release_asset_count += 1

                if archive_path.exists():
                    archive_path.unlink()

                uploaded_ids = {
                    item["media_id"]
                    for item in chunk_items
                }

                pending_items = [
                    item
                    for item in pending_items
                    if item["media_id"] not in uploaded_ids
                ]

                job.completed_items = (
                    db.query(MediaItem)
                    .filter(
                        MediaItem.job_id == job.id,
                        MediaItem.status == "uploaded",
                    )
                    .count()
                )

                job.current_part = part_number
                db.commit()

                part_number += 1

        job.status = "completed"
        job.error = None
        job.completed_items = job.total_items
        db.commit()

    except Exception as exc:
        try:
            job = db.get(
                BackupJob,
                job_id,
            )

            if job:
                job.status = "failed"
                job.error = str(exc)
                db.commit()
        finally:
            db.close()

        shutil.rmtree(
            upload_directory,
            ignore_errors=True,
        )

        raise

    finally:
        try:
            db.close()
        except Exception:
            pass

        shutil.rmtree(
            upload_directory,
            ignore_errors=True,
        )


# ============================================================
# Health
# ============================================================

@app.get(
    "/health"
)
def health():

    return {
        "status": "ok",
        "service": APP_NAME,
    }


# ============================================================
# Dashboard
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse,
)
def dashboard(
    request: Request,
):

    user = get_current_user(
        request
    )

    if not user:

        content = """
        <div class="card center">

            <h1>
                MyGoodOldPhotos
            </h1>

            <p class="muted">

                Backup selected Google Photos media
                to private GitHub Releases.

            </p>

            <br>

            <a href="/auth/google">

                <button class="google">

                    Sign in with Google

                </button>

            </a>

        </div>
        """

        return HTMLResponse(
            page(
                "Dashboard",
                content,
            )
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

        jobs = (
            db.query(BackupJob)
            .filter(
                BackupJob.user_id
                == user.id
            )
            .order_by(
                BackupJob.id.desc()
            )
            .limit(10)
            .all()
        )

        album_data = [
            {
                "id": album.id,
                "name": album.name,
            }
            for album in albums
        ]

        job_data = [
            {
                "id": job.id,
                "status": job.status,
                "completed": job.completed_items,
                "total": job.total_items,
                "album_id": job.album_id,
            }
            for job in jobs
        ]

    finally:

        db.close()

    # --------------------------------------------------------
    # Albums
    # --------------------------------------------------------

    if album_data:

        album_html = ""

        for album in album_data:

            album_html += f"""
            <div class="album">

                <div class="row">

                    <div>
                        <h3>
                            {escape(album["name"])}
                        </h3>

                        <span class="muted">
                            Album ID: {album["id"]}
                        </span>
                    </div>

                    <div>
                        <a href="/albums/{album["id"]}">
                            <button type="button">
                                Open Album
                            </button>
                        </a>
                    </div>

                </div>

            </div>
            """

    else:

        album_html = """
        <p class="muted">
            No albums yet.
        </p>
        """

    # --------------------------------------------------------
    # Jobs
    # --------------------------------------------------------

    if job_data:

        job_html = ""

        for job in job_data:

            job_html += f"""
            <div class="album">

                <div class="row">

                    <div>

                        <strong>
                            Job #{job["id"]}
                        </strong>

                        <p class="muted">

                            {job["completed"]}
                            /
                            {job["total"]}
                            items

                        </p>

                    </div>

                    <strong>
                        {escape(job["status"])}
                    </strong>

                </div>

            </div>
            """

    else:

        job_html = """
        <p class="muted">
            No backup jobs yet.
        </p>
        """

    repository = (
        "Not configured"
    )

    if (
        user.github_username
        and user.github_repo_name
    ):

        repository = (
            f"{user.github_username}/"
            f"{user.github_repo_name}"
        )

    content = f"""
    <div class="card">

        <h1>
            Dashboard
        </h1>

        <p>

            Google account:
            <strong>
                {escape(user.email)}
            </strong>

        </p>

        <p>

            Storage:
            <strong>
                {escape(user.storage_mode)}
            </strong>

        </p>

        <p>

            Repository:
            <strong>
                {escape(repository)}
            </strong>

        </p>

    </div>


    <div class="card">

        <h2>
            Albums
        </h2>

        {album_html}

    </div>


    <div class="card">

        <h2>
            Recent Backup Jobs
        </h2>

        {job_html}

    </div>


    <div class="card">

        <a href="/backup">

            <button>

                + New Backup

            </button>

        </a>

    </div>
    """

    return HTMLResponse(
        page(
            "Dashboard",
            content,
        )
    )


# ============================================================
# Album browser
# ============================================================

@app.get(
    "/albums/{album_id}",
    response_class=HTMLResponse,
)
def album_page(
    request: Request,
    album_id: int,
):
    user = get_current_user(request)

    if not user:
        return RedirectResponse(
            "/",
            status_code=302,
        )

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

        album_name = album.name

    finally:
        db.close()

    content = f"""
    <div class="card">

        <div class="row">

            <div>
                <h1>{escape(album_name)}</h1>
                <p class="muted">
                    GitHub Release chunks
                </p>
            </div>

            <div>
                <button
                    type="button"
                    onclick="downloadWholeAlbum()"
                >
                    Download Album
                </button>
            </div>

        </div>

        <div id="album-status">
            <div class="spinner"></div>
            <p>Loading album...</p>
        </div>

        <div id="chunks"></div>

    </div>

    <script>

        function formatBytes(bytes) {{
            if (!bytes) return "0 B";

            const units = [
                "B", "KB", "MB", "GB", "TB"
            ];

            let value = Number(bytes);
            let unit = 0;

            while (
                value >= 1024 &&
                unit < units.length - 1
            ) {{
                value /= 1024;
                unit++;
            }}

            return value.toFixed(
                unit === 0 ? 0 : 2
            ) + " " + units[unit];
        }}

        function escapeHtml(value) {{
            return String(value)
                .replaceAll("&", "&amp;")
                .replaceAll("<", "&lt;")
                .replaceAll(">", "&gt;")
                .replaceAll('"', "&quot;")
                .replaceAll("'", "&#039;");
        }}

        async function loadAlbum() {{

            const status =
                document.getElementById("album-status");

            const chunks =
                document.getElementById("chunks");

            try {{

                const response = await fetch(
                    "/api/albums/{album_id}/chunks"
                );

                const data =
                    await response.json();

                if (!response.ok) {{
                    throw new Error(
                        data.detail ||
                        "Failed to load album."
                    );
                }}

                status.innerHTML = `
                    <div class="info">
                        ${{data.chunks.length}}
                        chunk(s) available.
                    </div>
                `;

                if (!data.chunks.length) {{
                    chunks.innerHTML = `
                        <p class="muted">
                            This album has no uploaded chunks.
                        </p>
                    `;
                    return;
                }}

                let html = "";

                for (const chunk of data.chunks) {{

                    html += `
                        <div class="album">

                            <div class="row">

                                <div>
                                    <strong>
                                        ${{escapeHtml(chunk.name)}}
                                    </strong>

                                    <p class="muted">
                                        Size:
                                        ${{formatBytes(chunk.size)}}
                                    </p>
                                </div>

                                <div>
                                    <a
                                        href="/api/albums/{album_id}/chunks/${{chunk.id}}/download"
                                    >
                                        <button type="button">
                                            Download Chunk
                                        </button>
                                    </a>
                                </div>

                            </div>

                        </div>
                    `;
                }}

                chunks.innerHTML = html;

            }} catch (error) {{

                status.innerHTML = `
                    <div class="error">
                        ${{escapeHtml(error.message)}}
                    </div>
                `;

            }}
        }}

        async function downloadWholeAlbum() {{

            try {{

                const response = await fetch(
                    "/api/albums/{album_id}/download"
                );

                const data =
                    await response.json();

                if (!response.ok) {{
                    throw new Error(
                        data.detail ||
                        "Could not build album download plan."
                    );
                }}

                if (!data.chunks || !data.chunks.length) {{
                    alert(
                        "This album has no uploaded chunks."
                    );
                    return;
                }}

                const confirmed = confirm(
                    "This album contains "
                    + data.chunks.length
                    + " chunk(s), totalling "
                    + formatBytes(data.total_bytes)
                    + ". Download each chunk?"
                );

                if (!confirmed) {{
                    return;
                }}

                for (const chunk of data.chunks) {{
                    window.open(
                        "/api/albums/{album_id}/chunks/"
                        + chunk.id
                        + "/download",
                        "_blank"
                    );
                }}

            }} catch (error) {{
                alert(error.message);
            }}
        }}

        loadAlbum();

    </script>
    """

    return HTMLResponse(
        page(
            album_name,
            content,
        )
    )


# ============================================================
# Google OAuth
# ============================================================

@app.get(
    "/auth/google"
)
def google_login(
    request: Request,
):

    try:

        authorization_url = (
            create_authorization_url(
                request
            )
        )

    except Exception as exc:

        return HTMLResponse(
            page(
                "Google Login Error",
                f"""
                <div class="card">

                    <div class="error">

                        Google OAuth is not configured.

                    </div>

                    <p>
                        {escape(str(exc))}
                    </p>

                </div>
                """,
            ),
            status_code=500,
        )

    return RedirectResponse(
        authorization_url,
        status_code=302,
    )


# ============================================================
# Google OAuth callback
# ============================================================

@app.get(
    "/auth/google/callback"
)
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):

    if error:

        return HTMLResponse(
            page(
                "Google Login Error",
                f"""
                <div class="card">

                    <div class="error">

                        Google authorization failed:
                        {escape(error)}

                    </div>

                    <a href="/">

                        Return to dashboard

                    </a>

                </div>
                """,
            ),
            status_code=400,
        )

    if not code or not state:

        raise HTTPException(
            status_code=400,
            detail=(
                "Google OAuth callback is missing "
                "code or state."
            ),
        )

    # --------------------------------------------------------
    # Exchange code
    # --------------------------------------------------------

    credentials = (
        exchange_code_for_credentials(
            request,
            code,
            state,
        )
    )

    # --------------------------------------------------------
    # Google account info
    # --------------------------------------------------------

    user_info = (
        get_google_user_info(
            credentials
        )
    )

    # --------------------------------------------------------
    # Encrypt OAuth credentials
    # --------------------------------------------------------

    encrypted_google_token = (
        encrypt_credentials(
            credentials
        )
    )

    # --------------------------------------------------------
    # Find/create user
    # --------------------------------------------------------

    db = SessionLocal()

    try:

        user = (
            db.query(User)
            .filter(
                User.google_subject_id
                == user_info[
                    "subject_id"
                ]
            )
            .first()
        )

        if not user:

            user = User(
                google_subject_id=(
                    user_info[
                        "subject_id"
                    ]
                ),
                email=(
                    user_info[
                        "email"
                    ]
                ),
                email_prefix=(
                    user_info[
                        "email_prefix"
                    ]
                ),
                display_name=(
                    user_info.get(
                        "name"
                    )
                ),
                storage_mode="platform",
                encrypted_google_token=(
                    encrypted_google_token
                ),
            )

            db.add(
                user
            )

            db.commit()

            db.refresh(
                user
            )

        else:

            user.email = (
                user_info[
                    "email"
                ]
            )

            user.email_prefix = (
                user_info[
                    "email_prefix"
                ]
            )

            user.display_name = (
                user_info.get(
                    "name"
                )
            )

            user.encrypted_google_token = (
                encrypted_google_token
            )

            db.commit()

            db.refresh(
                user
            )

        user_id = user.id

    finally:

        db.close()

    # --------------------------------------------------------
    # Session contains ONLY user ID.
    # --------------------------------------------------------

    request.session[
        "user_id"
    ] = user_id

    return RedirectResponse(
        "/",
        status_code=302,
    )


# ============================================================
# Logout
# ============================================================

@app.get(
    "/logout"
)
def logout(
    request: Request,
):

    clear_google_session(
        request
    )

    request.session.clear()

    return RedirectResponse(
        "/",
        status_code=302,
    )


# ============================================================
# Settings
# ============================================================

@app.get(
    "/settings",
    response_class=HTMLResponse,
)
def settings(
    request: Request,
):

    user = get_current_user(
        request
    )

    if not user:

        return RedirectResponse(
            "/",
            status_code=302,
        )

    platform_checked = (
        "checked"
        if user.storage_mode
        == "platform"
        else ""
    )

    personal_checked = (
        "checked"
        if user.storage_mode
        == "personal"
        else ""
    )

    repository = (
        user.github_repo_name
        or "Not created yet"
    )

    github_account = (
        user.github_username
        or "Not connected"
    )

    content = f"""
    <div class="card">

        <h1>
            Settings
        </h1>

        <p>

            Google account:
            <strong>
                {escape(user.email)}
            </strong>

        </p>

        <p>

            GitHub account:
            <strong>
                {escape(github_account)}
            </strong>

        </p>

        <p>

            Repository:
            <strong>
                {escape(repository)}
            </strong>

        </p>

    </div>


    <div class="card">

        <h2>
            Storage
        </h2>

        <form
            method="post"
            action="/settings/storage"
        >

            <div class="mode">

                <label>

                    <input
                        type="radio"
                        name="storage_mode"
                        value="platform"
                        {platform_checked}
                    >

                    Use MyGoodOldPhotos storage

                </label>

                <p class="muted">

                    No GitHub PAT is required.

                    A private repository will be
                    created automatically.

                </p>

            </div>


            <div class="mode">

                <label>

                    <input
                        type="radio"
                        name="storage_mode"
                        value="personal"
                        {personal_checked}
                    >

                    Use my own GitHub account

                </label>

                <p class="muted">

                    Provide a GitHub PAT.

                    A private repository will be
                    created automatically.

                </p>

            </div>


            <label>
                GitHub PAT
            </label>

            <input
                type="password"
                name="github_pat"
                placeholder="Only required for personal storage"
                autocomplete="off"
            >


            <button type="submit">

                Save Settings

            </button>

        </form>

    </div>
    """

    return HTMLResponse(
        page(
            "Settings",
            content,
        )
    )


# ============================================================
# Save storage settings
# ============================================================

@app.post(
    "/settings/storage"
)
def save_storage_settings(
    request: Request,
    storage_mode: str = Form(...),
    github_pat: str = Form(""),
):

    user = require_google_login(
        request
    )

    github_pat = github_pat.strip()

    # --------------------------------------------------------
    # Personal GitHub storage
    # --------------------------------------------------------

    if storage_mode == "personal":

        if not github_pat:

            return HTMLResponse(
                page(
                    "Settings Error",
                    """
                    <div class="card">

                        <div class="error">

                            GitHub PAT is required.

                        </div>

                        <a href="/settings">

                            Return to settings

                        </a>

                    </div>
                    """,
                ),
                status_code=400,
            )

        try:

            github = GitHubClient(
                github_pat
            )

            github_username = (
                github.get_authenticated_username()
            )

            repo_name = sanitize_repository_name(
                user.email_prefix
            )

            repository = (
                github.ensure_private_repository(
                    repo_name
                )
            )

            encrypted_pat = (
                encrypt_string(
                    github_pat
                )
            )

            db = SessionLocal()

            try:

                db_user = db.get(
                    User,
                    user.id,
                )

                if not db_user:

                    raise RuntimeError(
                        "User account no longer exists."
                    )

                db_user.storage_mode = (
                    "personal"
                )

                db_user.github_username = (
                    github_username
                )

                db_user.github_repo_name = (
                    repository[
                        "name"
                    ]
                )

                db_user.github_repo_id = (
                    repository[
                        "id"
                    ]
                )

                db_user.encrypted_github_token = (
                    encrypted_pat
                )

                db.commit()

            finally:

                db.close()

        except Exception as exc:

            return HTMLResponse(
                page(
                    "GitHub Error",
                    f"""
                    <div class="card">

                        <div class="error">

                            GitHub setup failed.

                        </div>

                        <p>
                            {escape(str(exc))}
                        </p>

                        <a href="/settings">

                            Return to settings

                        </a>

                    </div>
                    """,
                ),
                status_code=400,
            )

        return RedirectResponse(
            "/",
            status_code=302,
        )

    # --------------------------------------------------------
    # Platform storage
    # --------------------------------------------------------

    if storage_mode == "platform":

        if not PLATFORM_GITHUB_TOKEN:

            return HTMLResponse(
                page(
                    "Storage Error",
                    """
                    <div class="card">

                        <div class="error">

                            Platform GitHub storage is
                            not configured on the server.

                        </div>

                    </div>
                    """,
                ),
                status_code=500,
            )

        try:

            github = GitHubClient(
                PLATFORM_GITHUB_TOKEN
            )

            github_username = (
                github.get_authenticated_username()
            )

            repo_name = (
                find_platform_repository_name(
                    github,
                    user.email_prefix,
                )
            )

            repository = (
                github.ensure_private_repository(
                    repo_name
                )
            )

            db = SessionLocal()

            try:

                db_user = db.get(
                    User,
                    user.id,
                )

                if not db_user:

                    raise RuntimeError(
                        "User account no longer exists."
                    )

                db_user.storage_mode = (
                    "platform"
                )

                db_user.github_username = (
                    github_username
                )

                db_user.github_repo_name = (
                    repository[
                        "name"
                    ]
                )

                db_user.github_repo_id = (
                    repository[
                        "id"
                    ]
                )

                db_user.encrypted_github_token = (
                    None
                )

                db.commit()

            finally:

                db.close()

        except Exception as exc:

            return HTMLResponse(
                page(
                    "Storage Error",
                    f"""
                    <div class="card">

                        <div class="error">

                            Failed to create your
                            private repository.

                        </div>

                        <p>
                            {escape(str(exc))}
                        </p>

                        <a href="/settings">

                            Return to settings

                        </a>

                    </div>
                    """,
                ),
                status_code=400,
            )

        return RedirectResponse(
            "/",
            status_code=302,
        )

    raise HTTPException(
        status_code=400,
        detail="Invalid storage mode.",
    )


# ============================================================
# New backup
# ============================================================

@app.get(
    "/backup",
    response_class=HTMLResponse,
)
def backup_page(
    request: Request,
):

    user = get_current_user(
        request
    )

    if not user:

        return RedirectResponse(
            "/",
            status_code=302,
        )

    content = """
    <div class="card">

        <h1>
            New Backup
        </h1>

        <h2>
            Google Photos
        </h2>

        <form
            method="post"
            action="/backup/start"
        >

            <label>
                Album name
            </label>

            <input
                type="text"
                name="album_name"
                placeholder="e.g. Goa Trip 2026"
                required
            >

            <button type="submit">
                Select Photos
            </button>

        </form>

        <hr>

        <h2>
            Local Files
        </h2>

        <p class="muted">
            Upload files from this device without compression,
            resizing, or media conversion. The original file bytes
            are stored inside an uncompressed TAR archive.
        </p>

        <form
            method="post"
            action="/backup/local"
            enctype="multipart/form-data"
        >

            <label>
                Album name
            </label>

            <input
                type="text"
                name="album_name"
                placeholder="e.g. Phone Backup"
                required
            >

            <label>
                Files
            </label>

            <input
                type="file"
                name="files"
                multiple
                required
            >

            <button type="submit">
                Upload Local Files
            </button>

        </form>

    </div>
    """

    return HTMLResponse(
        page(
            "New Backup",
            content,
        )
    )


# ============================================================
# Local file upload
# ============================================================

@app.post(
    "/backup/local",
    response_class=HTMLResponse,
)
def start_local_backup(
    request: Request,
    background_tasks: BackgroundTasks,
    album_name: str = Form(...),
    files: list[UploadFile] = File(...),
):
    user = require_google_login(
        request
    )

    album_name = album_name.strip()

    if not album_name:
        raise HTTPException(
            status_code=400,
            detail="Album name cannot be empty.",
        )

    if not files:
        raise HTTPException(
            status_code=400,
            detail="At least one local file is required.",
        )

    upload_directory = (
        LOCAL_UPLOAD_ROOT
        / str(uuid.uuid4())
    )

    upload_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    db = SessionLocal()

    media_ids = []
    saved_media_paths = []

    try:
        album = Album(
            user_id=user.id,
            name=album_name,
        )

        db.add(album)
        db.commit()
        db.refresh(album)

        job = BackupJob(
            user_id=user.id,
            album_id=album.id,
            status="local_queued",
            total_items=len(files),
            completed_items=0,
            current_part=0,
            error=None,
            picker_session_id=None,
        )

        db.add(job)
        db.commit()
        db.refresh(job)

        for index, upload in enumerate(
            files,
            start=1,
        ):
            filename = (
                Path(
                    upload.filename or "media.bin"
                ).name
            )

            if not filename or filename in {
                ".",
                "..",
            }:
                filename = f"media-{index:04d}.bin"

            media_id = (
                f"local:{job.id}:{uuid.uuid4().hex}"
            )

            destination = (
                upload_directory
                / f"{index:06d}_{filename}"
            )

            sha256 = hashlib.sha256()
            size = 0

            with destination.open("wb") as output:
                while chunk := upload.file.read(
                    1024 * 1024
                ):
                    output.write(chunk)
                    sha256.update(chunk)
                    size += len(chunk)

            if size <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Local file '{filename}' is empty."
                    ),
                )

            media = MediaItem(
                job_id=job.id,
                google_media_id=media_id,
                source_base_url=None,
                filename=filename,
                mime_type=(
                    upload.content_type
                    or "application/octet-stream"
                ),
                size=size,
                sha256=sha256.hexdigest(),
                local_path=str(destination),
                status="pending",
            )

            db.add(media)
            media_ids.append(media_id)
            saved_media_paths.append(destination)

        db.commit()

        background_tasks.add_task(
            _process_local_upload_job,
            job.id,
            str(upload_directory),
            media_ids,
        )

        content = f"""
        <div class="card center">

            <div class="success">
                Local backup queued.
            </div>

            <h1>
                Job #{job.id}
            </h1>

            <p>
                {len(files)} file(s) uploaded.
            </p>

            <p class="muted">
                No compression or media conversion is performed.
            </p>

            <a href="/">
                <button type="button">
                    Back to Dashboard
                </button>
            </a>

        </div>
        """

        return HTMLResponse(
            page(
                "Local Backup Queued",
                content,
            )
        )

    except HTTPException:
        db.rollback()
        shutil.rmtree(
            upload_directory,
            ignore_errors=True,
        )
        raise

    except Exception as exc:
        db.rollback()
        shutil.rmtree(
            upload_directory,
            ignore_errors=True,
        )

        try:
            if 'job' in locals() and job:
                job.status = "failed"
                job.error = str(exc)
                db.commit()
        except Exception:
            pass

        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )

    finally:
        db.close()


# ============================================================
# Start Google Photos Picker
# ============================================================

@app.post(
    "/backup/start"
)
def start_backup(
    request: Request,
    album_name: str = Form(...),
):

    user = require_google_login(
        request
    )

    album_name = album_name.strip()

    if not album_name:

        raise HTTPException(
            status_code=400,
            detail=(
                "Album name cannot be empty."
            ),
        )

    # --------------------------------------------------------
    # Get encrypted Google credentials from DB
    # --------------------------------------------------------

    credentials = (
        get_session_credentials(
            request
        )
    )

    # --------------------------------------------------------
    # Google Photos client
    # --------------------------------------------------------

    google = GooglePhotosClient(
        credentials.to_json()
    )

    # --------------------------------------------------------
    # Create album
    # --------------------------------------------------------

    db = SessionLocal()

    try:

        album = Album(
            user_id=user.id,
            name=album_name,
        )

        db.add(
            album
        )

        db.commit()

        db.refresh(
            album
        )

        album_id = album.id

    finally:

        db.close()

    # --------------------------------------------------------
    # Create Picker session
    # --------------------------------------------------------

    try:

        picker_session = (
            google.create_picker_session(
                max_item_count=2000
            )
        )

    except Exception as exc:

        db = SessionLocal()

        try:

            album = db.get(
                Album,
                album_id,
            )

            if album:

                db.delete(
                    album
                )

                db.commit()

        finally:

            db.close()

        return HTMLResponse(
            page(
                "Google Photos Error",
                f"""
                <div class="card">

                    <div class="error">

                        Failed to create
                        Google Photos Picker.

                    </div>

                    <p>
                        {escape(str(exc))}
                    </p>

                    <a href="/backup">

                        Try again

                    </a>

                </div>
                """,
            ),
            status_code=500,
        )

    picker_session_id = (
        picker_session[
            "id"
        ]
    )

    picker_url = (
        picker_session[
            "pickerUri"
        ]
    )

    # --------------------------------------------------------
    # Store ONLY identifiers in browser session.
    # --------------------------------------------------------

    request.session[
        "current_album_id"
    ] = album_id

    request.session[
        "picker_session_id"
    ] = picker_session_id

    # --------------------------------------------------------
    # Picker UI
    # --------------------------------------------------------

    safe_album_name = escape(
        album_name
    )

    safe_picker_url = escape(
        picker_url
        + "/autoclose"
    )

    content = f"""
    <div class="card center">

        <h1>
            {safe_album_name}
        </h1>

        <p>

            Select the photos and videos
            you want to back up.

        </p>

        <br>

        <a
            href="{safe_picker_url}"
            target="_blank"
            rel="noopener noreferrer"
        >

            <button class="google">

                Open Google Photos

            </button>

        </a>

        <div class="info">

            After completing your selection,
            close the Google Photos Picker.

        </div>


        <div id="status">

            <div class="spinner"></div>

            <p>
                Waiting for selection...
            </p>

        </div>

    </div>


    <script>

        async function checkPickerStatus() {{

            try {{

                const response = await fetch(
                    "/api/backup/picker-status"
                );

                const data =
                    await response.json();

                if (!response.ok) {{

                    throw new Error(
                        data.detail
                        || "Picker status failed."
                    );

                }}


                const status =
                    document.getElementById(
                        "status"
                    );


                if (
                    data.status
                    === "waiting"
                ) {{

                    setTimeout(
                        checkPickerStatus,
                        2000
                    );

                    return;

                }}


                if (
                    data.status
                    === "media_selected"
                ) {{

                    status.innerHTML = `

                        <div class="success">

                            Google Photos selection
                            completed.

                        </div>

                        <div class="large">

                            ${{data.count}}

                        </div>

                        <p>
                            photos/videos selected
                        </p>


                        <a href="/backup/start-job">

                            <button>

                                Start Backup

                            </button>

                        </a>

                    `;

                    return;

                }}


                status.innerHTML = `

                    <div class="error">

                        ${{data.status}}

                    </div>

                `;

            }}
            catch (error) {{

                document.getElementById(
                    "status"
                ).innerHTML = `

                    <div class="error">

                        ${{error.message}}

                    </div>

                `;

            }}

        }}


        checkPickerStatus();

    </script>
    """

    return HTMLResponse(
        page(
            "Select Photos",
            content,
        )
    )


# ============================================================
# Picker status
# ============================================================

@app.get(
    "/api/backup/picker-status"
)
def picker_status(
    request: Request,
):

    user = require_google_login(
        request
    )

    picker_session_id = (
        request.session.get(
            "picker_session_id"
        )
    )

    album_id = (
        request.session.get(
            "current_album_id"
        )
    )

    if not picker_session_id:

        return JSONResponse(
            {
                "status": (
                    "no_picker_session"
                )
            },
            status_code=400,
        )

    if not album_id:

        return JSONResponse(
            {
                "status": "no_album"
            },
            status_code=400,
        )

    credentials = (
        get_session_credentials(
            request
        )
    )

    google = GooglePhotosClient(
        credentials.to_json()
    )

    try:

        picker_session = (
            google.get_picker_session(
                picker_session_id
            )
        )

    except Exception as exc:

        return JSONResponse(
            {
                "status": "error",
                "detail": str(exc),
            },
            status_code=500,
        )

    if not picker_session.get(
        "mediaItemsSet"
    ):

        return {
            "status": "waiting",
            "count": 0,
        }

    try:

        media_items = (
            google.list_selected_media(
                picker_session_id
            )
        )

    except Exception as exc:

        return JSONResponse(
            {
                "status": "error",
                "detail": str(exc),
            },
            status_code=500,
        )

    # --------------------------------------------------------
    # Store count only.
    #
    # We deliberately KEEP picker_session_id because
    # the worker still needs the Picker session.
    # --------------------------------------------------------

    request.session[
        "selected_media_count"
    ] = len(
        media_items
    )

    return {
        "status": "media_selected",
        "count": len(
            media_items
        ),
    }


# ============================================================
# Start persistent backup job
# ============================================================

@app.get(
    "/backup/start-job",
    response_class=HTMLResponse,
)
def create_backup_job(
    request: Request,
):

    user = require_google_login(
        request
    )

    album_id = (
        request.session.get(
            "current_album_id"
        )
    )

    picker_session_id = (
        request.session.get(
            "picker_session_id"
        )
    )

    selected_count = (
        request.session.get(
            "selected_media_count",
            0,
        )
    )

    if not album_id:

        raise HTTPException(
            status_code=400,
            detail="No active album.",
        )

    if not picker_session_id:

        raise HTTPException(
            status_code=400,
            detail=(
                "No active Google Photos "
                "Picker session."
            ),
        )

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

        # ----------------------------------------------------
        # Prevent duplicate job creation from repeated clicks.
        # ----------------------------------------------------

        existing_job = (
            db.query(BackupJob)
            .filter(
                BackupJob.album_id
                == album.id,
                BackupJob.status.in_(
                    [
                        "queued",
                        "running",
                    ]
                ),
            )
            .first()
        )

        if existing_job:

            job_id = existing_job.id

        else:

            job = BackupJob(
                user_id=user.id,
                album_id=album.id,
                status="queued",
                total_items=selected_count,
                completed_items=0,
                current_part=0,
                error=None,
                picker_session_id=picker_session_id,
            )

            db.add(
                job
            )

            db.commit()

            db.refresh(
                job
            )

            job_id = job.id

    finally:

        db.close()

    content = f"""
    <div class="card center">

        <div class="success">

            Backup job created.

        </div>

        <h1>
            Job #{job_id}
        </h1>

        <p>

            {selected_count}
            photos/videos selected.

        </p>

        <p class="muted">

            Status:
            <strong>
                queued
            </strong>

        </p>

        <p>

            The backup worker will process
            this job next.

        </p>

        <a href="/">

            <button>
                Back to Dashboard
            </button>

        </a>

    </div>
    """

    # --------------------------------------------------------
    # Do NOT delete the Picker session here.
    # The worker requires it.
    # --------------------------------------------------------

    return HTMLResponse(
        page(
            "Backup Queued",
            content,
        )
    )


# ============================================================
# Backup job status
# ============================================================

@app.get(
    "/api/jobs/{job_id}"
)
def backup_job_status(
    request: Request,
    job_id: int,
):

    user = require_google_login(
        request
    )

    db = SessionLocal()

    try:

        job = db.get(
            BackupJob,
            job_id,
        )

        if not job:

            raise HTTPException(
                status_code=404,
                detail="Backup job not found.",
            )

        if job.user_id != user.id:

            raise HTTPException(
                status_code=403,
                detail="Access denied.",
            )

        return {
            "id": job.id,
            "status": job.status,
            "total_items": job.total_items,
            "completed_items": (
                job.completed_items
            ),
            "current_part": (
                job.current_part
            ),
            "error": job.error,
            "album_id": job.album_id,
        }

    finally:

        db.close()


# ============================================================
# Cancel queued job
# ============================================================

@app.post(
    "/api/jobs/{job_id}/cancel"
)
def cancel_backup_job(
    request: Request,
    job_id: int,
):

    user = require_google_login(
        request
    )

    db = SessionLocal()

    try:

        job = db.get(
            BackupJob,
            job_id,
        )

        if not job:

            raise HTTPException(
                status_code=404,
                detail="Backup job not found.",
            )

        if job.user_id != user.id:

            raise HTTPException(
                status_code=403,
                detail="Access denied.",
            )

        if job.status not in (
            "queued",
        ):

            raise HTTPException(
                status_code=400,
                detail=(
                    "Only queued jobs can be "
                    "cancelled at this stage."
                ),
            )

        job.status = "cancelled"

        job.error = None

        db.commit()

        return {
            "status": "cancelled",
            "job_id": job.id,
        }

    finally:

        db.close()


# ============================================================
# Local development entry point
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "8000",
            )
        ),
        reload=True,
    )