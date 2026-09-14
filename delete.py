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

        assets.sort(
            key=lambda asset:
                self.asset_part_number(
                    asset["name"]
                )
        )

        return assets

    # --------------------------------------------------------
    # Chunk number
    # --------------------------------------------------------

    @staticmethod
    def asset_part_number(
        asset_name
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

    # ========================================================
    # Delete one asset
    # ========================================================

    def delete_asset(
        self,
        asset,
    ):

        asset_id = asset[
            "id"
        ]

        self.request(
            "DELETE",
            f"{GITHUB_API_URL}/repos/"
            f"{GITHUB_OWNER}/{GITHUB_REPO}/"
            f"releases/assets/{asset_id}",
            timeout=(30, 300),
        )

    # ========================================================
    # Delete release
    # ========================================================

    def delete_release(
        self,
        release_id,
    ):

        self.request(
            "DELETE",
            f"{GITHUB_API_URL}/repos/"
            f"{GITHUB_OWNER}/{GITHUB_REPO}/"
            f"releases/{release_id}",
            timeout=(30, 300),
        )


# ============================================================
# Album selection
# ============================================================

def choose_album(
    releases,
):

    if not releases:

        print()
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
# Chunk selection
# ============================================================

def choose_asset(
    assets,
):

    if not assets:

        print(
            "This album has no chunks."
        )

        return None

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

    while True:

        choice = input(
            "Select chunk number: "
        ).strip()

        try:

            number = int(
                choice
            )

        except ValueError:

            print(
                "Enter a chunk number."
            )

            continue

        if 1 <= number <= len(
            assets
        ):

            return assets[
                number - 1
            ]

        print(
            "Invalid chunk number."
        )


# ============================================================
# Confirmation helpers
# ============================================================

def confirm_chunk_delete(
    album,
    asset,
):

    print()
    print("=" * 60)
    print("WARNING: DELETE CHUNK")
    print("=" * 60)
    print()
    print(
        f"Album: {album['name']}"
    )

    print(
        f"Chunk: {asset['name']}"
    )

    print(
        "This will permanently delete "
        "the GitHub release asset."
    )

    print()

    answer = input(
        "Type DELETE to confirm: "
    ).strip()

    return answer == "DELETE"


def confirm_album_delete(
    release,
    assets,
):

    print()
    print("=" * 60)
    print("WARNING: DELETE ENTIRE ALBUM")
    print("=" * 60)
    print()

    print(
        f"Album: {release['name']}"
    )

    print(
        f"Chunks: {len(assets)}"
    )

    print()
    print(
        "THIS WILL PERMANENTLY DELETE:"
    )

    print(
        f"- {len(assets)} release assets"
    )

    print(
        "- The GitHub release"
    )

    print()

    print(
        "This action cannot be undone."
    )

    print()

    answer = input(
        "Type DELETE ALBUM to confirm: "
    ).strip()

    return answer == "DELETE ALBUM"


# ============================================================
# Delete one chunk
# ============================================================

def delete_chunk(
    github,
    release,
    asset,
):

    if not confirm_chunk_delete(
        release,
        asset,
    ):

        print(
            "Deletion cancelled."
        )

        return

    print()
    print(
        f"Deleting chunk: "
        f"{asset['name']}"
    )

    github.delete_asset(
        asset
    )

    print(
        "Chunk deleted successfully."
    )


# ============================================================
# Delete entire album
# ============================================================

def delete_album(
    github,
    release,
    assets,
):

    if not confirm_album_delete(
        release,
        assets,
    ):

        print(
            "Deletion cancelled."
        )

        return

    print()
    print("=" * 60)
    print("DELETING ALBUM")
    print("=" * 60)

    # --------------------------------------------------------
    # Delete all release assets
    # --------------------------------------------------------

    for index, asset in enumerate(
        assets,
        start=1,
    ):

        print(
            f"[{index}/{len(assets)}] "
            f"Deleting: {asset['name']}"
        )

        github.delete_asset(
            asset
        )

    # --------------------------------------------------------
    # Delete release itself
    # --------------------------------------------------------

    print()
    print(
        f"Deleting release: "
        f"{release['name']}"
    )

    github.delete_release(
        release["id"]
    )

    print()
    print(
        "Album deleted successfully."
    )


# ============================================================
# Main
# ============================================================

def main():

    print("=" * 60)
    print("MyGoodOldPhotos")
    print("GitHub Release Deleter")
    print("=" * 60)

    github = GitHubClient()

    # --------------------------------------------------------
    # Load albums
    # --------------------------------------------------------

    print()
    print(
        "Loading backup albums..."
    )

    releases = (
        github.list_backup_releases()
    )

    # --------------------------------------------------------
    # Load assets
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

    assets = release[
        "_assets"
    ]

    # --------------------------------------------------------
    # Choose action
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("DELETE OPTIONS")
    print("=" * 60)
    print()

    print(
        "1. Delete one chunk"
    )

    print(
        "2. Delete entire album"
    )

    print(
        "3. Cancel"
    )

    print()

    while True:

        choice = input(
            "Choose option: "
        ).strip()

        if choice in (
            "1",
            "2",
            "3",
        ):
            break

        print(
            "Enter 1, 2 or 3."
        )

    # --------------------------------------------------------
    # Cancel
    # --------------------------------------------------------

    if choice == "3":

        print(
            "Deletion cancelled."
        )

        return

    # --------------------------------------------------------
    # One chunk
    # --------------------------------------------------------

    if choice == "1":

        asset = choose_asset(
            assets
        )

        if asset is None:
            return

        delete_chunk(
            github,
            release,
            asset,
        )

        return

    # --------------------------------------------------------
    # Entire album
    # --------------------------------------------------------

    delete_album(
        github,
        release,
        assets,
    )


if __name__ == "__main__":
    main()