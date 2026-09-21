from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tarfile
import time
import logging
from pathlib import Path
from typing import Any

from google_photos import GooglePhotosClient
from auth import (
    decrypt_credentials,
    encrypt_credentials,
    refresh_credentials,
)

from database import (
    Album,
    BackupJob,
    Chunk,
    MediaItem,
    SessionLocal,
    User,
)

from storage import (
    ensure_user_repository,
)

logger = logging.getLogger("mygoodoldphotos.worker")

class ProgressReporter:
    def __init__(self, db, job):
        self.db=db; self.job=job; self.last_time=0.0; self.last_bytes=-1
    def update(self, *, operation=None, filename=None, current=None, current_total=None, processed=None, total=None, completed_items=None, part=None, force=False):
        now=time.monotonic()
        changed = force or (now-self.last_time >= 0.5) or (current is not None and current-self.last_bytes >= 5*1024*1024)
        if not changed: return
        if operation is not None: self.job.current_operation=operation
        if filename is not None: self.job.current_filename=filename
        if current is not None: self.job.current_bytes=int(current); self.last_bytes=int(current)
        if current_total is not None: self.job.current_total_bytes=int(current_total)
        if processed is not None: self.job.processed_bytes=int(processed)
        if total is not None: self.job.total_bytes=int(total)
        if completed_items is not None: self.job.completed_items=int(completed_items)
        if part is not None: self.job.current_part=int(part)
        self.db.commit(); self.last_time=now




# ============================================================
# Configuration
# ============================================================

# Target chunk size:
# 1.8 GB decimal
MAX_CHUNK_BYTES = 1_800_000_000

# Keep TAR comfortably below GitHub's < 2 GiB asset limit.
GITHUB_MAX_ASSET_BYTES = (
    (2 * 1024 * 1024 * 1024) - 1
)

# GitHub currently allows up to 1000 assets per release.
MAX_ASSETS_PER_RELEASE = 1000

# Approximate TAR metadata overhead per file.
TAR_OVERHEAD_PER_FILE = 4096

# TAR end-of-archive markers.
TAR_END_BYTES = 1024

# Maximum visible filename length we put into a GitHub asset name.
MAX_ASSET_NAME_LENGTH = 240


# ============================================================
# Generic utilities
# ============================================================

def calculate_sha256(
    file_path: Path,
) -> str:

    sha256 = hashlib.sha256()

    with file_path.open(
        "rb"
    ) as file:

        while chunk := file.read(
            1024 * 1024
        ):

            sha256.update(
                chunk
            )

    return sha256.hexdigest()


def sanitize_filename(
    value: str,
) -> str:

    value = Path(
        value or "media"
    ).name

    value = re.sub(
        r"[^A-Za-z0-9._() \-\[\]]+",
        "_",
        value,
    )

    value = value.strip(
        " ."
    )

    if not value:
        return "media"

    return value


def sanitize_album_name(
    value: str,
) -> str:

    value = value.strip()

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    if not value:
        raise ValueError(
            "Album name cannot be empty."
        )

    return value


def build_asset_name(
    album_name: str,
    part_number: int,
    first_filename: str,
    last_filename: str,
) -> str:

    album_name = sanitize_album_name(
        album_name
    )

    first_filename = sanitize_filename(
        first_filename
    )

    last_filename = sanitize_filename(
        last_filename
    )

    name = (
        f"{album_name} - "
        f"Part {part_number:03d} - "
        f"{first_filename} to "
        f"{last_filename}.tar"
    )

    # --------------------------------------------------------
    # GitHub has practical filename/path limits.
    # Keep the user-visible name comfortably bounded.
    # --------------------------------------------------------

    if len(name) > MAX_ASSET_NAME_LENGTH:

        prefix = (
            f"{album_name} - "
            f"Part {part_number:03d} - "
        )

        suffix = ".tar"

        available = (
            MAX_ASSET_NAME_LENGTH
            - len(prefix)
            - len(suffix)
        )

        if available < 20:

            name = (
                f"{album_name[:40]} - "
                f"Part {part_number:03d}.tar"
            )

        else:

            middle = (
                f"{first_filename} "
                f"to "
                f"{last_filename}"
            )

            middle = middle[
                :available
            ]

            name = (
                prefix
                + middle
                + suffix
            )

    return name


# ============================================================
# Google credential handling
# ============================================================

def get_google_client_for_user(
    user: User,
) -> GooglePhotosClient:

    if not user.encrypted_google_token:

        raise RuntimeError(
            "Google authorization is missing "
            f"for user {user.id}."
        )

    credentials = (
        decrypt_credentials(
            user.encrypted_google_token
        )
    )

    old_token = credentials.token

    credentials = refresh_credentials(
        credentials
    )

    # --------------------------------------------------------
    # Save refreshed access token if Google rotated it.
    # --------------------------------------------------------

    if old_token != credentials.token:

        db = SessionLocal()

        try:

            db_user = db.get(
                User,
                user.id,
            )

            if db_user:

                db_user.encrypted_google_token = (
                    encrypt_credentials(
                        credentials
                    )
                )

                db.commit()

        finally:

            db.close()

    return GooglePhotosClient(
        credentials.to_json()
    )


# ============================================================
# TAR sizing
# ============================================================

def estimate_tar_size(
    items: list[dict[str, Any]],
) -> int:

    total = TAR_END_BYTES

    for item in items:

        total += (
            int(item["size"])
            + TAR_OVERHEAD_PER_FILE
        )

    # Manifest overhead.
    total += 8192

    return total


def can_add_to_chunk(
    current_items: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> bool:

    candidate_items = (
        current_items
        + [candidate]
    )

    return (
        estimate_tar_size(
            candidate_items
        )
        <= MAX_CHUNK_BYTES
    )


def choose_chunk_items(
    pending_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:

    if not pending_items:
        return []

    chunk = []

    for item in pending_items:

        if not chunk:

            # A single item may be larger than the normal 1.8 GB
            # target, but it must still fit inside GitHub's asset limit.
            estimated_single_size = estimate_tar_size([item])

            if estimated_single_size > GITHUB_MAX_ASSET_BYTES:
                raise RuntimeError(
                    "A single media file is too large for a GitHub "
                    "Release asset. "
                    f"File: {item.get('filename')} "
                    f"({item.get('size')} bytes)."
                )

            chunk.append(item)
            continue

        if can_add_to_chunk(
            chunk,
            item,
        ):
            chunk.append(item)
        else:
            break

    return chunk


# ============================================================
# Media download
# ============================================================

def download_media_item(
    google: GooglePhotosClient,
    media_item: dict[str, Any],
    destination: Path,
    progress_callback=None,
) -> dict[str, Any]:

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    media_id = media_item[
        "id"
    ]

    media_file = media_item.get(
        "mediaFile"
    )

    if not media_file:

        raise RuntimeError(
            f"Media item {media_id} "
            "does not contain mediaFile."
        )

    filename = (
        media_file.get(
            "filename"
        )
        or f"{media_id}.bin"
    )

    base_url = media_file.get(
        "baseUrl"
    )

    if not base_url:

        raise RuntimeError(
            f"Media item {media_id} "
            "does not contain baseUrl."
        )

    # --------------------------------------------------------
    # GooglePhotosClient handles streaming and .part files.
    # --------------------------------------------------------

    result = google.download_media(
        media_item,
        destination,
        progress_callback=progress_callback,
    )

    output_path = Path(
        result["path"]
    )

    size = output_path.stat().st_size

    sha256 = calculate_sha256(
        output_path
    )

    return {
        "media_id": media_id,
        "filename": filename,
        "mime_type": media_file.get(
            "mimeType"
        ),
        "local_path": str(
            output_path
        ),
        "size": size,
        "sha256": sha256,
        "status": "downloaded",
    }


# ============================================================
# Create TAR chunk
# ============================================================

def create_chunk_archive(
    chunk_items: list[dict[str, Any]],
    album_name: str,
    part_number: int,
    temporary_directory: Path,
):
    archive_path = (
        temporary_directory
        / f"chunk-{part_number:06d}.tar"
    )

    manifest_items = []

    with tarfile.open(
        archive_path,
        mode="w",
    ) as archive:

        for index, item in enumerate(
            chunk_items,
            start=1,
        ):

            source_path = Path(
                item["local_path"]
            )

            if not source_path.is_file():

                raise FileNotFoundError(
                    "Downloaded media file disappeared:\n"
                    f"{source_path}"
                )

            archive_filename = (
                f"{index:06d}_"
                f"{sanitize_filename(item['filename'])}"
            )

            archive_path_in_tar = (
                f"media/"
                f"{archive_filename}"
            )

            archive.add(
                source_path,
                arcname=archive_path_in_tar,
            )

            manifest_items.append({
                "google_media_id": item[
                    "media_id"
                ],
                "filename": item[
                    "filename"
                ],
                "mime_type": item.get(
                    "mime_type"
                ),
                "size": item[
                    "size"
                ],
                "sha256": item[
                    "sha256"
                ],
                "archive_path": archive_path_in_tar,
            })

        # ----------------------------------------------------
        # Manifest
        # ----------------------------------------------------

        manifest = {
            "format": "MyGoodOldPhotos",
            "version": 1,
            "album": album_name,
            "part": part_number,
            "items": manifest_items,
        }

        manifest_bytes = json.dumps(
            manifest,
            indent=2,
            ensure_ascii=False,
        ).encode(
            "utf-8"
        )

        manifest_path = (
            temporary_directory
            / "manifest.json"
        )

        manifest_path.write_bytes(
            manifest_bytes
        )

        archive.add(
            manifest_path,
            arcname="manifest.json",
        )

    archive_size = (
        archive_path.stat().st_size
    )

    if archive_size >= GITHUB_MAX_ASSET_BYTES:

        raise RuntimeError(
            "Generated TAR chunk is too large "
            "for a GitHub release asset.\n"
            f"Size: {archive_size:,} bytes"
        )

    archive_hash = calculate_sha256(
        archive_path
    )

    asset_name = build_asset_name(
        album_name=album_name,
        part_number=part_number,
        first_filename=chunk_items[
            0
        ]["filename"],
        last_filename=chunk_items[
            -1
        ]["filename"],
    )

    return {
        "path": archive_path,
        "size": archive_size,
        "sha256": archive_hash,
        "asset_name": asset_name,
        "manifest": manifest,
    }


# ============================================================
# Persist media information
# ============================================================

def create_or_update_media_rows(
    db,
    job: BackupJob,
    media_items: list[dict[str, Any]],
):
    media_rows = []

    for item in media_items:

        media = (
            db.query(MediaItem)
            .filter(
                MediaItem.job_id
                == job.id,
                MediaItem.google_media_id
                == item["media_id"],
            )
            .first()
        )

        if not media:

            media = MediaItem(
                job_id=job.id,
                google_media_id=item[
                    "media_id"
                ],
                filename=item[
                    "filename"
                ],
                mime_type=item.get(
                    "mime_type"
                ),
                size=int(
                    item["size"]
                ),
                sha256=item.get(
                    "sha256"
                ),
                local_path=item.get(
                    "local_path"
                ),
                status="pending",
            )

            db.add(
                media
            )

        else:

            media.filename = item[
                "filename"
            ]

            media.mime_type = item.get(
                "mime_type"
            )

            media.size = int(
                item["size"]
            )

            media.sha256 = item.get(
                "sha256"
            )

            media.local_path = item.get(
                "local_path"
            )

            if media.status != "uploaded":

                media.status = "pending"

        media_rows.append(
            media
        )

    db.commit()

    return media_rows


# ============================================================
# Mark chunk + media uploaded
# ============================================================

def persist_uploaded_chunk(
    db,
    album: Album,
    chunk_items: list[dict[str, Any]],
    release,
    asset,
    part_number: int,
    archive_size: int,
    archive_hash: str,
):
    chunk_row = Chunk(
        album_id=album.id,
        part_number=part_number,
        asset_name=asset[
            "name"
        ],
        github_asset_id=asset[
            "id"
        ],
        github_release_id=release[
            "id"
        ],
        release_tag=release[
            "tag_name"
        ],
        size=archive_size,
        sha256=archive_hash,
        status="uploaded",
    )

    db.add(
        chunk_row
    )

    # --------------------------------------------------------
    # Associate every original media file with this chunk.
    # --------------------------------------------------------

    for item in chunk_items:

        media = (
            db.query(MediaItem)
            .filter(
                MediaItem.google_media_id
                == item["media_id"],
            )
            .first()
        )

        if not media:

            raise RuntimeError(
                "Media database row disappeared.\n"
                f"Google media ID: "
                f"{item['media_id']}"
            )

        media.status = "uploaded"

        media.part_number = (
            part_number
        )

        media.asset_name = (
            asset["name"]
        )

        media.release_tag = (
            release["tag_name"]
        )

        # ----------------------------------------------------
        # Original local file is no longer required after
        # successful upload + verification.
        # ----------------------------------------------------

        local_path = item.get(
            "local_path"
        )

        if local_path:

            local_file = Path(
                local_path
            )

            if local_file.exists():

                local_file.unlink()

            media.local_path = None

    db.commit()

    return chunk_row


# ============================================================
# Get next part number
# ============================================================

def get_next_part_number(
    db,
    album: Album,
) -> int:

    latest = (
        db.query(Chunk)
        .filter(
            Chunk.album_id
            == album.id
        )
        .order_by(
            Chunk.part_number.desc()
        )
        .first()
    )

    if not latest:

        return 1

    return (
        latest.part_number
        + 1
    )


# ============================================================
# Run backup
# ============================================================

def _repository_string(repository: dict[str, Any]) -> str:
    return (
        f"{repository['owner']['login']}/"
        f"{repository['name']}"
    )


def _next_github_release_number(
    github,
) -> int:
    releases = github.list_backup_releases()

    if not releases:
        return 1

    numbers = []

    for release in releases:
        tag_name = release.get("tag_name", "")

        match = re.search(r"(\d+)$", tag_name)
        if match:
            numbers.append(int(match.group(1)))

    return max(numbers, default=0) + 1


def _load_local_pending_items(
    db,
    job: BackupJob,
) -> list[dict[str, Any]]:
    rows = (
        db.query(MediaItem)
        .filter(
            MediaItem.job_id == job.id,
        )
        .order_by(
            MediaItem.id.asc()
        )
        .all()
    )

    pending_items = []

    for media in rows:
        if media.status == "uploaded":
            continue

        local_path = Path(
            media.local_path or ""
        )

        if not local_path.is_file():
            raise FileNotFoundError(
                "Uploaded local file disappeared:\n"
                f"{local_path}"
            )

        actual_size = local_path.stat().st_size

        if int(media.size) != actual_size:
            raise RuntimeError(
                "Local file size changed after upload.\n"
                f"File: {media.filename}\n"
                f"Expected: {media.size}\n"
                f"Actual: {actual_size}"
            )

        actual_sha256 = calculate_sha256(local_path)

        if media.sha256 and actual_sha256 != media.sha256:
            raise RuntimeError(
                "Local file SHA-256 changed after upload.\n"
                f"File: {media.filename}\n"
                f"Expected: {media.sha256}\n"
                f"Actual: {actual_sha256}"
            )

        media.sha256 = actual_sha256
        media.size = actual_size

        pending_items.append({
            "media_id": media.google_media_id,
            "filename": media.filename,
            "mime_type": media.mime_type,
            "local_path": str(local_path),
            "size": actual_size,
            "sha256": actual_sha256,
            "status": "downloaded",
        })

    db.commit()
    return pending_items


def _run_local_backup_job(
    db,
    job: BackupJob,
    user: User,
    album: Album,
):
    """
    Process files uploaded directly from the user's device.

    The files are never compressed, resized, re-encoded, or otherwise
    modified. They are copied byte-for-byte into an uncompressed TAR.
    """

    github, repository = ensure_user_repository(user)
    progress = ProgressReporter(db, job)

    pending_items = _load_local_pending_items(
        db,
        job,
    )

    job.total_items = (
        db.query(MediaItem)
        .filter(MediaItem.job_id == job.id)
        .count()
    )

    job.completed_items = (
        db.query(MediaItem)
        .filter(
            MediaItem.job_id == job.id,
            MediaItem.status == "uploaded",
        )
        .count()
    )
    job.total_bytes = sum(int(x.get("size", 0)) for x in pending_items) + int(job.processed_bytes or 0)
    progress.update(operation="preparing", filename=None, current=0, current_total=0, processed=job.processed_bytes, total=job.total_bytes, force=True)

    if not pending_items:
        job.status = "completed"
        job.error = None
        db.commit()
        return {
            "job_id": job.id,
            "status": job.status,
            "total_items": job.total_items,
            "completed_items": job.completed_items,
            "current_part": job.current_part,
            "repository": _repository_string(repository),
        }

    part_number = get_next_part_number(
        db,
        album,
    )

    release = None
    release_asset_count = MAX_ASSETS_PER_RELEASE
    next_release_number = _next_github_release_number(github)

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

            if (
                release is None
                or release_asset_count >= MAX_ASSETS_PER_RELEASE
            ):
                release = github.create_release(
                    album.name,
                    next_release_number,
                )

                next_release_number += 1
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

            archive_size = archive_path.stat().st_size

            if archive_size > GITHUB_MAX_ASSET_BYTES:
                raise RuntimeError(
                    "Generated TAR exceeds GitHub's maximum asset size.\n"
                    f"Asset: {chunk_info['asset_name']}\n"
                    f"Size: {archive_size} bytes"
                )

            archive_size_for_progress = int(chunk_info["size"])
            def on_upload(count, _job=job, _progress=progress, _asset=chunk_info["asset_name"], _processed=int(job.processed_bytes or 0)):
                _progress.update(operation="uploading", filename=_asset, current=count, current_total=archive_size_for_progress, processed=_processed, total=job.total_bytes)
            progress.update(operation="uploading", filename=chunk_info["asset_name"], current=0, current_total=archive_size_for_progress, processed=job.processed_bytes, total=job.total_bytes, force=True)
            release, asset = github.upload_asset(
                release,
                archive_path,
                chunk_info["asset_name"],
                progress_callback=on_upload,
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
            job.processed_bytes = min(job.total_bytes, int(job.processed_bytes or 0) + sum(int(x.get("size", 0)) for x in chunk_items))
            progress.update(operation="uploaded", filename=None, current=0, current_total=0, processed=job.processed_bytes, total=job.total_bytes, completed_items=job.completed_items, part=part_number, force=True)

            part_number += 1
            release_asset_count += 1

    job.status = "completed"
    job.error = None
    job.completed_items = job.total_items
    db.commit()

    return {
        "job_id": job.id,
        "status": job.status,
        "total_items": job.total_items,
        "completed_items": job.completed_items,
        "current_part": job.current_part,
        "repository": _repository_string(repository),
    }


def run_backup_job(
    job_id: int,
    picker_session_id: str | None,
):
    """
    Process one backup job.

    Google Photos jobs provide a Picker session ID.
    Local-file jobs have no Picker session and are processed from the
    MediaItem rows created by the HTTP upload step.
    """

    db = SessionLocal()

    try:
        job = db.get(
            BackupJob,
            job_id,
        )

        if not job:
            raise RuntimeError(
                f"Backup job {job_id} does not exist."
            )

        user = db.get(
            User,
            job.user_id,
        )

        album = db.get(
            Album,
            job.album_id,
        )

        if not user:
            raise RuntimeError(
                "Backup job user does not exist."
            )

        if not album:
            raise RuntimeError(
                "Backup job album does not exist."
            )

        job.status = "running"
        job.error = None
        db.commit()

        # --------------------------------------------------------
        # Local Upload
        # --------------------------------------------------------

        if picker_session_id is None:
            return _run_local_backup_job(
                db=db,
                job=job,
                user=user,
                album=album,
            )

        # --------------------------------------------------------
        # Google Photos
        # --------------------------------------------------------

        github, repository = ensure_user_repository(user)
        progress_reporter = ProgressReporter(db, job)
        if not isinstance(progress_reporter, ProgressReporter):
            raise RuntimeError(
                "Internal progress reporter initialization failed."
            )


        google = get_google_client_for_user(user)

        media_items = google.list_selected_media(
            picker_session_id
        )

        job.total_items = len(media_items)
        job.completed_items = 0
        job.total_bytes = sum(int((m.get("mediaFile") or {}).get("mediaFileMetadata", {}).get("contentSize") or 0) for m in media_items)
        job.processed_bytes = 0
        db.commit()

        if not media_items:
            job.status = "completed"
            db.commit()

            return {
                "job_id": job.id,
                "status": "completed",
                "total_items": 0,
                "completed_items": 0,
                "current_part": job.current_part,
                "repository": _repository_string(repository),
            }

        with tempfile.TemporaryDirectory(
            prefix="mygoodoldphotos_backup_"
        ) as temporary_directory:

            temp_dir = Path(
                temporary_directory
            )

            pending_items = []

            for index, media_item in enumerate(
                media_items,
                start=1,
            ):
                media_id = media_item["id"]

                existing = (
                    db.query(MediaItem)
                    .filter(
                        MediaItem.job_id == job.id,
                        MediaItem.google_media_id == media_id,
                    )
                    .first()
                )

                if (
                    existing
                    and existing.status == "uploaded"
                ):
                    job.completed_items += 1
                    db.commit()
                    continue

                if existing and existing.local_path:
                    existing_path = Path(
                        existing.local_path
                    )

                    if existing_path.exists():
                        pending_items.append({
                            "media_id": existing.google_media_id,
                            "filename": existing.filename,
                            "mime_type": existing.mime_type,
                            "size": existing.size,
                            "sha256": existing.sha256,
                            "local_path": str(existing_path),
                            "status": "pending",
                        })
                        continue

                filename = (
                    media_item.get("mediaFile", {}).get("filename")
                    or f"{media_id}.bin"
                )

                destination = (
                    temp_dir
                    / sanitize_filename(
                        f"{media_id}_{filename}"
                    )
                )

                current_expected = int((media_item.get("mediaFile") or {}).get("mediaFileMetadata", {}).get("contentSize") or 0)
                download_base = int(job.processed_bytes or 0)
                def on_download(count, _filename=filename, _expected=current_expected, _base=download_base):
                    total_value = max(int(job.total_bytes or 0), _base + int(_expected or 0))
                    if job.total_bytes < total_value:
                        job.total_bytes = total_value
                    progress_reporter.update(operation="downloading", filename=_filename, current=count, current_total=_expected, processed=_base, total=job.total_bytes)
                progress_reporter.update(operation="downloading", filename=filename, current=0, current_total=current_expected, processed=download_base, total=job.total_bytes, force=True)

                download_start = time.perf_counter()

                downloaded = download_media_item(google, media_item, destination,progress_callback=on_download)

                download_elapsed = time.perf_counter() - download_start
                download_size_mb = int(downloaded.get("size") or 0) / (1024 * 1024)
                download_speed = download_size_mb / download_elapsed if download_elapsed > 0 else 0

                logger.info(
                    "Google download: %s | %.2f MB | %.2f s | %.2f MB/s",
                    filename,
                    download_size_mb,
                    download_elapsed,
                    download_speed,
                )

                create_or_update_media_rows(
                    db,
                    job,
                    [downloaded],
                )

                pending_items.append(downloaded)

                # Commit the completed download into the durable progress state.
                actual_size = int(downloaded.get("size") or 0)
                job.processed_bytes = min(
                    int(job.total_bytes or 0),
                    int(job.processed_bytes or 0) + actual_size,
                )
                job.completed_items = index
                progress_reporter.update(
                    operation="downloaded",
                    filename=filename,
                    current=actual_size,
                    current_total=actual_size,
                    processed=job.processed_bytes,
                    total=job.total_bytes,
                    completed_items=job.completed_items,
                    force=True,
                )

            job.completed_items = (
                db.query(MediaItem)
                .filter(
                    MediaItem.job_id == job.id,
                    MediaItem.status == "uploaded",
                )
                .count()
            )
            job.current_bytes = 0
            job.current_total_bytes = 0
            job.current_filename = None
            job.current_operation = "completed"
            db.commit()

            if not pending_items:
                job.status = "completed"
                job.error = None
                db.commit()

                try:
                    google.delete_picker_session(
                        picker_session_id
                    )
                except Exception:
                    pass

                return {
                    "job_id": job.id,
                    "status": "completed",
                    "total_items": job.total_items,
                    "completed_items": job.completed_items,
                    "current_part": job.current_part,
                    "repository": _repository_string(repository),
                }

            next_part_number = get_next_part_number(
                db,
                album,
            )

            while pending_items:
                chunk_items = choose_chunk_items(
                    pending_items
                )

                if not chunk_items:
                    raise RuntimeError(
                        "Unable to create a backup chunk."
                    )

                chunk_info = create_chunk_archive(
                    chunk_items,
                    album.name,
                    next_part_number,
                    temp_dir,
                )

                archive_path = Path(
                    chunk_info["path"]
                )

                archive_size = archive_path.stat().st_size

                if archive_size > GITHUB_MAX_ASSET_BYTES:
                    raise RuntimeError(
                        "Generated TAR exceeds GitHub's maximum asset size.\n"
                        f"Asset: {chunk_info['asset_name']}\n"
                        f"Size: {archive_size} bytes"
                    )

                archive_size_for_progress = int(chunk_info["size"])
                processed_before_upload = int(job.processed_bytes or 0)
                def on_upload(count, _asset=chunk_info["asset_name"], _processed=processed_before_upload):
                    progress_reporter.update(operation="uploading", filename=_asset, current=count, current_total=archive_size_for_progress, processed=_processed, total=max(job.total_bytes, _processed + archive_size_for_progress))
                progress_reporter.update(operation="uploading", filename=chunk_info["asset_name"], current=0, current_total=archive_size_for_progress, processed=processed_before_upload, total=max(job.total_bytes, processed_before_upload + archive_size_for_progress), force=True)
                upload_start = time.perf_counter()

                release, asset = github.upload_asset(
                    github.get_current_release(album.name),
                    archive_path,
                    chunk_info["asset_name"],
                    progress_callback=on_upload,
                )
                
                upload_elapsed = time.perf_counter() - upload_start
                upload_size_mb = archive_size_for_progress / (1024 * 1024)
                upload_speed = upload_size_mb / upload_elapsed if upload_elapsed > 0 else 0
                
                logger.info(
                    "GitHub upload: %s | %.2f MB | %.2f s | %.2f MB/s",
                    chunk_info["asset_name"],
                    upload_size_mb,
                    upload_elapsed,
                    upload_speed,
                )

                persist_uploaded_chunk(
                    db=db,
                    album=album,
                    chunk_items=chunk_items,
                    release=release,
                    asset=asset,
                    part_number=next_part_number,
                    archive_size=chunk_info["size"],
                    archive_hash=chunk_info["sha256"],
                )

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

                job.current_part = next_part_number
                progress_reporter.update(
                    operation="uploaded",
                    filename=chunk_info["asset_name"],
                    current=archive_size_for_progress,
                    current_total=archive_size_for_progress,
                    processed=job.processed_bytes,
                    total=job.total_bytes,
                    completed_items=job.completed_items,
                    part=next_part_number,
                    force=True,
                )
                db.commit()

                next_part_number += 1

            job.status = "completed"
            job.error = None
            job.completed_items = (
                db.query(MediaItem)
                .filter(
                    MediaItem.job_id == job.id,
                    MediaItem.status == "uploaded",
                )
                .count()
            )
            db.commit()

            try:
                google.delete_picker_session(
                    picker_session_id
                )
            except Exception:
                pass

            return {
                "job_id": job.id,
                "status": job.status,
                "total_items": job.total_items,
                "completed_items": job.completed_items,
                "current_part": job.current_part,
                "repository": _repository_string(repository),
            }

    except Exception as exc:
        try:
            job = db.get(
                BackupJob,
                job_id,
            )

            if job:
                job.status = "failed"
                job.error = str(exc)
                job.user_message = "Backup failed. Please check the server logs for details."
                db.commit()
            logger.exception("Backup job %s failed", job_id)
        finally:
            db.close()

        raise

    finally:
        try:
            db.close()
        except Exception:
            pass


# ============================================================
# Direct module execution
# ============================================================

if __name__ == "__main__":

    print(
        "backup_engine.py is a backend worker module."
    )

    print(
        "It is not intended to be run directly."
    )