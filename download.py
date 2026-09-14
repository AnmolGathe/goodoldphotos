from pathlib import Path
import os
import re

import requests
from dotenv import load_dotenv


# ============================================================
# Configuration
# ============================================================

GITHUB_OWNER = "AnmolGathe"
GITHUB_REPO = "MyGoodOldPhotos_Gallery"

GITHUB_API_URL = "https://api.github.com"

BACKUP_TAG_PREFIX = "photos-backup-"

DOWNLOAD_DIR = Path("downloads")


# ============================================================
# GitHub Client
# ============================================================

class GitHubClient:

    def __init__(self):

        load_dotenv()

        token = os.getenv(
            "GITHUB_TOKEN"
        )

        if not token:
            raise RuntimeError(
                "GITHUB_TOKEN was not found in .env"
            )

        self.session = requests.Session()

        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    # --------------------------------------------------------
    # Generic request
    # --------------------------------------------------------

    def request(
        self,
        method,
        url,
        *,
        timeout=(30, 60),
        **kwargs,
    ):

        response = self.session.request(
            method,
            url,
            timeout=timeout,
            **kwargs,
        )

        if response.status_code >= 400:

            raise RuntimeError(
                "GitHub API request failed.\n"
                f"Method: {method}\n"
                f"URL: {url}\n"
                f"Status: {response.status_code}\n"
                f"Response: {response.text}"
            )

        return response

    # ========================================================
    # Releases
    # ========================================================

    def list_backup_releases(self):

        releases = []

        page = 1

        while True:

            response = self.request(
                "GET",
                f"{GITHUB_API_URL}/repos/"
                f"{GITHUB_OWNER}/{GITHUB_REPO}/releases",
                params={
                    "per_page": 100,
                    "page": page,
                },
            )

            batch = response.json()

            if not batch:
                break

            for release in batch:

                tag_name = release.get(
                    "tag_name",
                    "",
                )

                if tag_name.startswith(
                    BACKUP_TAG_PREFIX
                ):
                    releases.append(
                        release
                    )

            if len(batch) < 100:
                break

            page += 1

        releases.sort(
            key=lambda release:
                self.release_number(
                    release["tag_name"]
                )
        )

        return releases

    # --------------------------------------------------------
    # Release number
    # --------------------------------------------------------

    @staticmethod
    def release_number(
        tag_name
    ):

        match = re.fullmatch(
            rf"{re.escape(BACKUP_TAG_PREFIX)}(\d+)",
            tag_name,
        )

        if not match:
            return -1

        return int(
            match.group(1)
        )

    # --------------------------------------------------------
    # Release assets
    # --------------------------------------------------------

    def list_release_assets(
        self,
        release_id,
    ):

        assets = []

        page = 1

        while True:

            response = self.request(
                "GET",
                f"{GITHUB_API_URL}/repos/"
                f"{GITHUB_OWNER}/{GITHUB_REPO}/releases/"
                f"{release_id}/assets",
                params={
                    "per_page": 100,
                    "page": page,
                },
            )

            batch = response.json()

            if not batch:
                break

            assets.extend(
                batch
            )

            if len(batch) < 100:
                break

            page += 1

        return assets

    # ========================================================
    # Download one release asset
    # ========================================================

    def download_asset(
        self,
        asset,
        destination,
    ):

        asset_name = asset[
            "name"
        ]

        destination = Path(
            destination
        )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_path = destination.with_suffix(
            destination.suffix
            + ".part"
        )

        print()
        print(
            f"Downloading: {asset_name}"
        )

        print(
            f"Size: {asset['size']:,} bytes"
        )

        url = (
            f"{GITHUB_API_URL}/repos/"
            f"{GITHUB_OWNER}/{GITHUB_REPO}/"
            f"releases/assets/{asset['id']}"
        )

        headers = {
            "Accept": "application/octet-stream",
        }

        with self.session.get(
            url,
            headers=headers,
            stream=True,
            timeout=(30, 3600),
        ) as response:

            if response.status_code >= 400:

                raise RuntimeError(
                    "Failed to download asset.\n"
                    f"Status: {response.status_code}\n"
                    f"Response: {response.text}"
                )

            with temporary_path.open(
                "wb"
            ) as file:

                downloaded = 0

                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):

                    if not chunk:
                        continue

                    file.write(
                        chunk
                    )

                    downloaded += len(
                        chunk
                    )

                    print(
                        f"\rDownloaded: "
                        f"{downloaded:,} / "
                        f"{asset['size']:,} bytes",
                        end="",
                        flush=True,
                    )

        print()

        temporary_path.replace(
            destination
        )

        print(
            f"Saved: {destination}"
        )

        return destination


# ============================================================
# TAR extraction
# ============================================================

def safe_extract_tar(
    archive_path,
    destination,
):

    archive_path = Path(
        archive_path
    )

    destination = Path(
        destination
    )

    destination.mkdir(
        parents=True,
        exist_ok=True,
    )

    print(
        f"Extracting: {archive_path.name}"
    )

    import tarfile

    destination_resolved = (
        destination.resolve()
    )

    with tarfile.open(
        archive_path,
        "r",
    ) as archive:

        for member in archive.getmembers():

            member_path = (
                destination
                / member.name
            ).resolve()

            if (
                destination_resolved
                not in member_path.parents
                and member_path
                != destination_resolved
            ):

                raise RuntimeError(
                    "Unsafe path detected "
                    "inside TAR archive:\n"
                    f"{member.name}"
                )

        archive.extractall(
            destination
        )

    print(
        f"Extracted to: {destination}"
    )


# ============================================================
# Album selection
# ============================================================

def choose_album(
    releases,
):

    if not releases:

        print(
            "No backup albums found."
        )

        return None

    print()
    print("=" * 60)
    print("AVAILABLE ALBUMS")
    print("=" * 60)
    print()

    for index, release in enumerate(
        releases,
        start=1,
    ):

        assets = release.get(
            "_assets",
            []
        )

        print(
            f"{index}. "
            f"{release['name']} "
            f"({len(assets)} chunks)"
        )

    print()

    while True:

        choice = input(
            "Select album number: "
        ).strip()

        try:

            number = int(
                choice
            )

        except ValueError:

            print(
                "Enter a number."
            )

            continue

        if 1 <= number <= len(
            releases
        ):

            return releases[
                number - 1
            ]

        print(
            "Invalid album number."
        )


# ============================================================
# Chunk sorting
# ============================================================

def extract_part_number(
    asset_name,
):

    match = re.search(
        r"Part\s+(\d+)",
        asset_name,
        re.IGNORECASE,
    )

    if match:

        return int(
            match.group(1)
        )

    return 999999999


# ============================================================
# Chunk selection
# ============================================================

def choose_chunks(
    assets,
):

    if not assets:

        print(
            "This album has no chunks."
        )

        return []

    assets = sorted(
        assets,
        key=lambda asset:
            extract_part_number(
                asset["name"]
            )
    )

    print()
    print("=" * 60)
    print("ALBUM CHUNKS")
    print("=" * 60)
    print()

    for index, asset in enumerate(
        assets,
        start=1,
    ):

        print(
            f"{index}. "
            f"{asset['name']}"
        )

        print(
            f"   Size: "
            f"{asset['size']:,} bytes"
        )

    print()
    print("A. Download whole album")
    print()

    while True:

        choice = input(
            "Select chunk number or A: "
        ).strip().lower()

        if choice == "a":

            return assets

        try:

            number = int(
                choice
            )

        except ValueError:

            print(
                "Enter a chunk number "
                "or A."
            )

            continue

        if 1 <= number <= len(
            assets
        ):

            return [
                assets[number - 1]
            ]

        print(
            "Invalid chunk number."
        )


# ============================================================
# Download + extract
# ============================================================

def process_asset(
    github,
    asset,
    album_directory,
):

    archive_name = asset[
        "name"
    ]

    archive_path = (
        album_directory
        / "_chunks"
        / archive_name
    )

    extraction_directory = (
        album_directory
    )

    # --------------------------------------------------------
    # Download
    # --------------------------------------------------------

    github.download_asset(
        asset,
        archive_path,
    )

    # --------------------------------------------------------
    # Extract
    # --------------------------------------------------------

    safe_extract_tar(
        archive_path,
        extraction_directory,
    )

    # --------------------------------------------------------
    # Remove TAR after successful extraction
    # --------------------------------------------------------

    try:

        archive_path.unlink()

        print(
            f"Removed local archive: "
            f"{archive_path.name}"
        )

    except OSError:

        print(
            "Warning: could not remove "
            f"{archive_path}"
        )


# ============================================================
# Main
# ============================================================

def main():

    print("=" * 60)
    print("MyGoodOldPhotos")
    print("GitHub Release Downloader")
    print("=" * 60)

    github = GitHubClient()

    # --------------------------------------------------------
    # Get releases
    # --------------------------------------------------------

    print()
    print(
        "Loading backup albums..."
    )

    releases = (
        github.list_backup_releases()
    )

    # --------------------------------------------------------
    # Load assets for every release
    # --------------------------------------------------------

    for release in releases:

        release["_assets"] = (
            github.list_release_assets(
                release["id"]
            )
        )

    # --------------------------------------------------------
    # Choose album
    # --------------------------------------------------------

    release = choose_album(
        releases
    )

    if release is None:
        return

    # --------------------------------------------------------
    # Choose chunks
    # --------------------------------------------------------

    assets = choose_chunks(
        release["_assets"]
    )

    if not assets:
        return

    # --------------------------------------------------------
    # Destination
    # --------------------------------------------------------

    album_name = release[
        "name"
    ]

    album_directory = (
        DOWNLOAD_DIR
        / album_name
    )

    album_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    print()
    print("=" * 60)
    print("DOWNLOAD")
    print("=" * 60)
    print(
        f"Album: {album_name}"
    )
    print(
        f"Chunks: {len(assets)}"
    )
    print(
        f"Destination: {album_directory}"
    )
    print()

    # --------------------------------------------------------
    # Process assets
    # --------------------------------------------------------

    for index, asset in enumerate(
        assets,
        start=1,
    ):

        print()
        print(
            "-" * 60
        )

        print(
            f"Chunk {index}/{len(assets)}"
        )

        process_asset(
            github,
            asset,
            album_directory,
        )

    # --------------------------------------------------------
    # Finished
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("DOWNLOAD COMPLETE")
    print("=" * 60)
    print()
    print(
        f"Album: {album_name}"
    )
    print(
        f"Location: {album_directory}"
    )
    print()


if __name__ == "__main__":
    main()