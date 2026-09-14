from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from database import Album, Chunk, SessionLocal, User
from storage import ensure_user_repository


# ============================================================
# Constants
# ============================================================

DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "mygoodoldphotos_downloads"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)


# ============================================================
# Utilities
# ============================================================

def extract_part_number(asset_name: str) -> int:
    match = re.search(r"Part\s+(\d+)", asset_name or "", re.IGNORECASE)
    return int(match.group(1)) if match else 999999999


def sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_download_name(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"[^A-Za-z0-9._() \-\[\]]+", "_", value)
    value = value.strip(" .")
    return value or "album"


def safe_extract_tar(archive_path: Path, destination: Path) -> list[Path]:
    archive_path = Path(archive_path)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    destination_resolved = destination.resolve()
    extracted_paths: list[Path] = []

    with tarfile.open(archive_path, "r") as archive:
        members = archive.getmembers()

        for member in members:
            if member.issym() or member.islnk():
                raise RuntimeError("Unsafe TAR archive: links are not allowed.")

            member_path = (destination / member.name).resolve()

            if (
                destination_resolved not in member_path.parents
                and member_path != destination_resolved
            ):
                raise RuntimeError(
                    "Unsafe path detected inside TAR archive: "
                    f"{member.name}"
                )

        archive.extractall(destination)

        for member in members:
            extracted_paths.append(destination / member.name)

    return extracted_paths


def read_manifest(extraction_directory: Path) -> dict[str, Any] | None:
    manifest_path = extraction_directory / "manifest.json"

    if not manifest_path.exists():
        return None

    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid manifest.json: {exc}") from exc


# ============================================================
# User storage
# ============================================================

def get_user_github(user: User):
    return ensure_user_repository(user)


# ============================================================
# Album lookup
# ============================================================

def get_user_album(user_id: int, album_id: int) -> Album:
    db = SessionLocal()
    try:
        album = db.get(Album, album_id)
        if not album:
            raise RuntimeError("Album not found.")
        if album.user_id != user_id:
            raise PermissionError("Access denied.")
        db.expunge(album)
        return album
    finally:
        db.close()


def get_user_by_id(user_id: int) -> User:
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user:
            raise RuntimeError("User not found.")
        db.expunge(user)
        return user
    finally:
        db.close()


# ============================================================
# Album -> chunks
# ============================================================

def _get_album_chunk_rows(album_id: int) -> list[Chunk]:
    db = SessionLocal()
    try:
        rows = (
            db.query(Chunk)
            .filter(
                Chunk.album_id == album_id,
                Chunk.status == "uploaded",
            )
            .order_by(Chunk.part_number.asc(), Chunk.id.asc())
            .all()
        )

        for row in rows:
            db.expunge(row)

        return rows
    finally:
        db.close()


def list_album_chunks(
    user: User,
    album: Album,
) -> list[dict[str, Any]]:
    """
    Return ONLY chunks that the database associates with this album.

    An album may span multiple GitHub releases. We therefore do not
    infer album membership from the current/latest GitHub release.
    """
    github, _ = get_user_github(user)
    chunk_rows = _get_album_chunk_rows(album.id)

    if not chunk_rows:
        return []

    # Fetch each stored release once, then map only DB-associated assets.
    assets_by_release: dict[int, dict[int, dict[str, Any]]] = {}
    release_ids = sorted(
        {int(row.github_release_id) for row in chunk_rows}
    )

    for release_id in release_ids:
        release_assets = github.list_release_assets(release_id)
        assets_by_release[release_id] = {
            int(asset["id"]): asset
            for asset in release_assets
        }

    result: list[dict[str, Any]] = []

    for row in chunk_rows:
        release_id = int(row.github_release_id)
        asset_id = int(row.github_asset_id)

        asset = assets_by_release.get(release_id, {}).get(asset_id)

        if not asset:
            raise RuntimeError(
                "Backup chunk is recorded in the database but its "
                "GitHub release asset could not be found.\n"
                f"Album: {album.name}\n"
                f"Part: {row.part_number}\n"
                f"Asset ID: {asset_id}\n"
                f"Release ID: {release_id}"
            )

        # Defense in depth: verify the asset name stored in DB still matches.
        if row.asset_name and asset.get("name") != row.asset_name:
            raise RuntimeError(
                "GitHub asset metadata does not match the database record.\n"
                f"Expected: {row.asset_name}\n"
                f"Actual: {asset.get('name')}"
            )

        result.append(
            {
                **asset,
                "album_id": album.id,
                "part_number": int(row.part_number),
                "github_release_id": release_id,
                "database_chunk_id": int(row.id),
                "database_sha256": row.sha256,
                "database_size": int(row.size),
            }
        )

    result.sort(
        key=lambda asset: (
            int(asset.get("part_number", 999999999)),
            asset.get("name", ""),
        )
    )

    return result


def get_album_chunk(
    user: User,
    album: Album,
    asset_id: int,
) -> dict[str, Any]:
    """
    Resolve one chunk strictly through the album's DB association.
    """
    github, _ = get_user_github(user)

    db = SessionLocal()
    try:
        row = (
            db.query(Chunk)
            .filter(
                Chunk.album_id == album.id,
                Chunk.github_asset_id == int(asset_id),
                Chunk.status == "uploaded",
            )
            .first()
        )

        if not row:
            raise RuntimeError(
                "Requested chunk does not belong to this album."
            )

        release_id = int(row.github_release_id)
        asset = github.get_release_asset(int(row.github_asset_id))

        if int(asset.get("id")) != int(row.github_asset_id):
            raise RuntimeError("GitHub asset identity verification failed.")

        if row.asset_name and asset.get("name") != row.asset_name:
            raise RuntimeError(
                "GitHub asset name does not match the database record."
            )

        asset["album_id"] = album.id
        asset["part_number"] = int(row.part_number)
        asset["github_release_id"] = release_id
        asset["database_chunk_id"] = int(row.id)
        asset["database_sha256"] = row.sha256
        asset["database_size"] = int(row.size)

        return asset
    finally:
        db.close()


# ============================================================
# Download one chunk
# ============================================================

def download_chunk(
    user: User,
    album: Album,
    asset: dict[str, Any],
    progress_callback=None,
) -> tuple[Path, Path]:
    github, _ = get_user_github(user)

    asset_id = int(asset["id"])
    verified_asset = get_album_chunk(user, album, asset_id)

    working_directory = Path(
        tempfile.mkdtemp(
            prefix="mygoodoldphotos_download_",
            dir=str(DOWNLOAD_ROOT),
        )
    )

    archive_path = working_directory / verified_asset["name"]

    try:
        github.download_asset(
            verified_asset,
            archive_path,
            progress_callback=progress_callback,
        )

        if not archive_path.exists():
            raise RuntimeError(
                "GitHub download completed without creating the expected archive."
            )

        actual_size = archive_path.stat().st_size
        expected_size = verified_asset.get("size")

        if expected_size is not None and actual_size != int(expected_size):
            raise RuntimeError(
                "Downloaded asset size does not match GitHub metadata.\n"
                f"Expected: {int(expected_size):,}\n"
                f"Actual:   {actual_size:,}"
            )

        expected_digest = verified_asset.get("digest")
        if expected_digest and expected_digest.startswith("sha256:"):
            actual_digest = f"sha256:{sha256_file(archive_path)}"
            if actual_digest != expected_digest:
                raise RuntimeError(
                    "Downloaded chunk SHA-256 does not match GitHub digest.\n"
                    f"Expected: {expected_digest}\n"
                    f"Actual:   {actual_digest}"
                )

        database_size = verified_asset.get("database_size")
        database_hash = verified_asset.get("database_sha256")

        if database_size is not None and int(database_size) <= 0:
            raise RuntimeError("Invalid database chunk size.")

        if database_hash and not re.fullmatch(r"[0-9a-fA-F]{64}", database_hash):
            raise RuntimeError("Invalid database chunk SHA-256.")

        return archive_path, working_directory

    except Exception:
        shutil.rmtree(working_directory, ignore_errors=True)
        raise


# ============================================================
# Extract one chunk
# ============================================================

def _verify_manifest_integrity(
    extraction_directory: Path,
    manifest: dict[str, Any],
) -> None:
    items = manifest.get("items")

    if not isinstance(items, list):
        raise RuntimeError("Backup manifest does not contain a valid items list.")

    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("Backup manifest contains an invalid media entry.")

        archive_path = item.get("archive_path")
        filename = item.get("filename")
        expected_size = item.get("size")
        expected_sha256 = item.get("sha256")

        if not archive_path:
            raise RuntimeError("Backup manifest entry is missing archive_path.")

        file_path = (extraction_directory / archive_path).resolve()
        root = extraction_directory.resolve()

        if root not in file_path.parents or not file_path.is_file():
            raise RuntimeError(
                "Backup manifest points to a missing or unsafe media file.\n"
                f"Path: {archive_path}"
            )

        actual_size = file_path.stat().st_size
        if expected_size is not None and actual_size != int(expected_size):
            raise RuntimeError(
                "Media integrity check failed: size mismatch.\n"
                f"File: {filename or archive_path}\n"
                f"Expected: {int(expected_size):,}\n"
                f"Actual:   {actual_size:,}"
            )

        if expected_sha256:
            actual_sha256 = sha256_file(file_path)
            if actual_sha256.lower() != str(expected_sha256).lower():
                raise RuntimeError(
                    "Media integrity check failed: SHA-256 mismatch.\n"
                    f"File: {filename or archive_path}\n"
                    f"Expected: {expected_sha256}\n"
                    f"Actual:   {actual_sha256}"
                )


def extract_chunk(
    archive_path: Path,
    working_directory: Path,
) -> tuple[Path, dict[str, Any] | None]:
    extraction_directory = working_directory / "extracted"

    safe_extract_tar(
        archive_path,
        extraction_directory,
    )

    manifest = read_manifest(extraction_directory)

    if manifest is not None:
        _verify_manifest_integrity(
            extraction_directory,
            manifest,
        )

    return extraction_directory, manifest


# ============================================================
# Download + extract one chunk
# ============================================================

def download_and_extract_chunk(
    user: User,
    album: Album,
    asset: dict[str, Any],
) -> tuple[Path, Path, dict[str, Any] | None]:
    archive_path, working_directory = download_chunk(
        user,
        album,
        asset,
    )

    try:
        extraction_directory, manifest = extract_chunk(
            archive_path,
            working_directory,
        )

        return (
            extraction_directory,
            working_directory,
            manifest,
        )

    except Exception:
        shutil.rmtree(working_directory, ignore_errors=True)
        raise


# ============================================================
# Whole album metadata
# ============================================================

def get_album_download_plan(
    user: User,
    album: Album,
) -> dict[str, Any]:
    assets = list_album_chunks(
        user,
        album,
    )

    total_bytes = sum(
        int(asset.get("size", 0))
        for asset in assets
    )

    return {
        "album_id": album.id,
        "album_name": album.name,
        "chunks": len(assets),
        "total_bytes": total_bytes,
        "assets": [
            {
                "id": asset["id"],
                "name": asset["name"],
                "size": int(asset.get("size", 0)),
                "part_number": int(
                    asset.get(
                        "part_number",
                        extract_part_number(asset.get("name", "")),
                    )
                ),
            }
            for asset in assets
        ],
    }
