from pathlib import Path
import base64
import hashlib
import mimetypes
import re

import requests


# ============================================================
# Configuration
# ============================================================

GITHUB_API_URL = "https://api.github.com"

BACKUP_TAG_PREFIX = "photos-backup-"

MAX_ASSETS_PER_RELEASE = 1000


# ============================================================
# Helpers
# ============================================================

def calculate_sha256(
    file_path: Path,
) -> str:

    sha256 = hashlib.sha256()

    with Path(file_path).open("rb") as file:

        while chunk := file.read(1024 * 1024):
            sha256.update(chunk)

    return sha256.hexdigest()


def parse_release_number(
    tag_name: str,
) -> int:

    match = re.fullmatch(
        rf"{re.escape(BACKUP_TAG_PREFIX)}(\d+)",
        tag_name,
    )

    if not match:
        return -1

    return int(match.group(1))


def parse_part_number(
    asset_name: str,
) -> int:

    match = re.search(
        r"Part\s+(\d+)",
        asset_name,
        re.IGNORECASE,
    )

    if not match:
        return 999999999

    return int(match.group(1))


class _ProgressReader:
    """File-like wrapper that reports bytes consumed by requests."""

    def __init__(self, file, total_size, progress_callback=None):
        self._file = file
        self._total_size = int(total_size)
        self._progress_callback = progress_callback
        self._bytes_read = 0

    def read(self, size=-1):
        chunk = self._file.read(size)
        if chunk:
            self._bytes_read += len(chunk)
            if self._progress_callback:
                self._progress_callback(
                    self._bytes_read,
                    self._total_size,
                )
        elif self._progress_callback and self._bytes_read < self._total_size:
            # Some transports may perform a final zero-byte read.
            self._progress_callback(
                self._bytes_read,
                self._total_size,
            )
        return chunk

    def __len__(self):
        return self._total_size

    def tell(self):
        return self._file.tell()

    def seek(self, offset, whence=0):
        return self._file.seek(offset, whence)

    def fileno(self):
        return self._file.fileno()


# ============================================================
# GitHub Client
# ============================================================

class GitHubClient:

    def __init__(
        self,
        token: str,
        owner: str | None = None,
        repo: str | None = None,
    ):

        if not token:
            raise ValueError(
                "GitHub token cannot be empty."
            )

        self.token = token

        self.owner = owner
        self.repo = repo

        self.session = requests.Session()

        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    # ========================================================
    # Generic HTTP request
    # ========================================================

    def request(
        self,
        method: str,
        url: str,
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
    # Account
    # ========================================================

    def get_authenticated_user(
        self,
    ):

        response = self.request(
            "GET",
            f"{GITHUB_API_URL}/user",
        )

        return response.json()

    def get_authenticated_username(
        self,
    ) -> str:

        user = self.get_authenticated_user()

        login = user.get(
            "login"
        )

        if not login:

            raise RuntimeError(
                "GitHub API did not return "
                "the authenticated username."
            )

        return login

    # ========================================================
    # Repository
    # ========================================================

    def set_repository(
        self,
        owner: str,
        repo: str,
    ):

        if not owner:
            raise ValueError(
                "Repository owner cannot be empty."
            )

        if not repo:
            raise ValueError(
                "Repository name cannot be empty."
            )

        self.owner = owner
        self.repo = repo

    def repository_configured(
        self,
    ):

        return bool(
            self.owner
            and self.repo
        )

    def repository_url(
        self,
    ):

        self._require_repository()

        return (
            f"{GITHUB_API_URL}/repos/"
            f"{self.owner}/{self.repo}"
        )

    def get_repository(
        self,
        owner: str | None = None,
        repo: str | None = None,
    ):

        owner = owner or self.owner
        repo = repo or self.repo

        if not owner or not repo:

            raise RuntimeError(
                "GitHub repository is not configured."
            )

        response = self.request(
            "GET",
            f"{GITHUB_API_URL}/repos/"
            f"{owner}/{repo}",
        )

        return response.json()

    def repository_exists(
        self,
        owner: str | None = None,
        repo: str | None = None,
    ) -> bool:

        owner = owner or self.owner
        repo = repo or self.repo

        if not owner or not repo:
            return False

        response = self.session.get(
            f"{GITHUB_API_URL}/repos/"
            f"{owner}/{repo}",
            timeout=(30, 60),
        )

        if response.status_code == 200:
            return True

        if response.status_code == 404:
            return False

        raise RuntimeError(
            "Failed to check GitHub repository.\n"
            f"Status: {response.status_code}\n"
            f"Response: {response.text}"
        )

    def create_private_repository(
        self,
        repo_name: str,
        description: str = (
            "Private storage created by MyGoodOldPhotos."
        ),
    ):

        if not repo_name:
            raise ValueError(
                "Repository name cannot be empty."
            )

        response = self.request(
            "POST",
            f"{GITHUB_API_URL}/user/repos",
            json={
                "name": repo_name,
                "description": description,
                "private": True,
                "has_issues": False,
                "has_projects": False,
                "has_wiki": False,
                "has_discussions": False,
                "auto_init": True,
            },
            timeout=(30, 120),
        )

        repository = response.json()

        self.owner = (
            repository["owner"]["login"]
        )

        self.repo = repository["name"]

        return repository

    def ensure_private_repository(
        self,
        repo_name: str,
        description: str = (
            "Private storage created by MyGoodOldPhotos."
        ),
    ):

        username = (
            self.get_authenticated_username()
        )

        if self.repository_exists(
            username,
            repo_name,
        ):

            repository = self.get_repository(
                username,
                repo_name,
            )

            if not repository.get(
                "private"
            ):

                raise RuntimeError(
                    f"GitHub repository "
                    f"{username}/{repo_name} "
                    "already exists but is not private."
                )

            self.set_repository(
                username,
                repo_name,
            )

            return repository

        repository = (
            self.create_private_repository(
                repo_name,
                description,
            )
        )

        return repository

    # ========================================================
    # Ensure repository is not empty
    # ========================================================

    def ensure_repository_initialized(
        self,
    ):

        self._require_repository()

        commits_url = (
            f"{GITHUB_API_URL}/repos/"
            f"{self.owner}/{self.repo}/commits"
        )

        response = self.session.get(
            commits_url,
            params={
                "per_page": 1,
            },
            timeout=(30, 60),
        )

        # Repository has at least one commit.
        if response.status_code == 200:

            commits = response.json()

            if commits:
                return

        # GitHub returns 409 for an empty repository.
        elif response.status_code != 409:

            raise RuntimeError(
                "Failed to check whether GitHub "
                "repository is empty.\n"
                f"Status: {response.status_code}\n"
                f"Response: {response.text}"
            )

        # ----------------------------------------------------
        # Repository is empty.
        # Create an initial README commit.
        # ----------------------------------------------------

        content = base64.b64encode(
            (
                "This private repository stores "
                "MyGoodOldPhotos backup releases.\n"
            ).encode("utf-8")
        ).decode("ascii")

        self.request(
            "PUT",
            f"{GITHUB_API_URL}/repos/"
            f"{self.owner}/{self.repo}/contents/"
            f"README.md",
            json={
                "message": (
                    "Initialize MyGoodOldPhotos repository"
                ),
                "content": content,
            },
            timeout=(30, 120),
        )

        # Verify that the repository now has a commit.
        response = self.session.get(
            commits_url,
            params={
                "per_page": 1,
            },
            timeout=(30, 60),
        )

        if response.status_code != 200:

            raise RuntimeError(
                "Repository initialization appeared to "
                "succeed, but GitHub still does not return "
                "the repository commits.\n"
                f"Status: {response.status_code}\n"
                f"Response: {response.text}"
            )

        commits = response.json()

        if not commits:

            raise RuntimeError(
                "GitHub repository is still empty "
                "after initialization."
            )

    # ========================================================
    # Internal validation
    # ========================================================

    def _require_repository(
        self,
    ):

        if not self.owner or not self.repo:

            raise RuntimeError(
                "GitHub repository is not configured."
            )

    # ========================================================
    # Releases
    # ========================================================

    def list_backup_releases(
        self,
    ):

        self._require_repository()

        releases = []
        page = 1

        while True:

            response = self.request(
                "GET",
                f"{GITHUB_API_URL}/repos/"
                f"{self.owner}/{self.repo}/releases",
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
                parse_release_number(
                    release["tag_name"]
                )
        )

        return releases

    def list_release_assets(
        self,
        release_id: int,
    ):

        self._require_repository()

        assets = []
        page = 1

        while True:

            response = self.request(
                "GET",
                f"{GITHUB_API_URL}/repos/"
                f"{self.owner}/{self.repo}/releases/"
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
                parse_part_number(
                    asset["name"]
                )
        )

        return assets

    def get_release(
        self,
        release_id: int,
    ):

        self._require_repository()

        response = self.request(
            "GET",
            f"{GITHUB_API_URL}/repos/"
            f"{self.owner}/{self.repo}/releases/"
            f"{release_id}",
        )

        return response.json()

    def create_release(
        self,
        album_name: str,
        release_number: int,
    ):

        self._require_repository()

        if not album_name.strip():

            raise ValueError(
                "Album name cannot be empty."
            )

        if release_number < 1:

            raise ValueError(
                "Release number must be >= 1."
            )

        # ----------------------------------------------------
        # IMPORTANT:
        # Releases cannot be created in an empty repository.
        # Always verify initialization immediately here.
        # ----------------------------------------------------

        self.ensure_repository_initialized()

        tag_name = (
            f"{BACKUP_TAG_PREFIX}"
            f"{release_number:04d}"
        )

        response = self.request(
            "POST",
            f"{GITHUB_API_URL}/repos/"
            f"{self.owner}/{self.repo}/releases",
            json={
                "tag_name": tag_name,
                "name": album_name,
                "body": (
                    "Google Photos backup.\n\n"
                    f"Album: {album_name}\n"
                    "Each release asset is a backup "
                    "chunk archive."
                ),
                "draft": False,
                "prerelease": False,
                "generate_release_notes": False,
            },
            timeout=(30, 120),
        )

        return response.json()

    def get_current_release(
        self,
        album_name: str,
    ):

        releases = (
            self.list_backup_releases()
        )

        if not releases:

            return self.create_release(
                album_name,
                1,
            )

        current = releases[-1]

        assets = (
            self.list_release_assets(
                current["id"]
            )
        )

        if len(assets) >= MAX_ASSETS_PER_RELEASE:

            current_number = (
                parse_release_number(
                    current["tag_name"]
                )
            )

            return self.create_release(
                album_name,
                current_number + 1,
            )

        current["_assets"] = assets

        return current

    # ========================================================
    # Asset lookup
    # ========================================================

    def find_backup_asset(
        self,
        asset_name: str,
    ):

        releases = (
            self.list_backup_releases()
        )

        for release in releases:

            assets = (
                self.list_release_assets(
                    release["id"]
                )
            )

            for asset in assets:

                if asset[
                    "name"
                ] == asset_name:

                    return {
                        "release": release,
                        "asset": asset,
                    }

        return None

    # ========================================================
    # Upload release asset
    # ========================================================

    def upload_asset(
        self,
        release,
        file_path: Path,
        asset_name: str,
        progress_callback=None,
    ):

        self._require_repository()

        file_path = Path(
            file_path
        )

        if not file_path.is_file():

            raise FileNotFoundError(
                f"File not found: {file_path}"
            )

        local_size = file_path.stat().st_size

        local_hash = calculate_sha256(
            file_path
        )

        expected_digest = (
            f"sha256:{local_hash}"
        )

        # ----------------------------------------------------
        # Existing asset
        # ----------------------------------------------------

        existing = (
            self.find_backup_asset(
                asset_name
            )
        )

        if existing:

            asset = existing[
                "asset"
            ]

            remote_digest = asset.get(
                "digest"
            )

            if (
                remote_digest
                == expected_digest
            ):

                return (
                    existing["release"],
                    asset,
                )

            raise RuntimeError(
                "An asset with this name already "
                "exists but contains different data.\n"
                f"Asset: {asset_name}\n"
                f"Local:  {expected_digest}\n"
                f"Remote: {remote_digest}"
            )

        # ----------------------------------------------------
        # Upload URL
        # ----------------------------------------------------

        upload_url = release[
            "upload_url"
        ]

        upload_url = upload_url.split(
            "{"
        )[0]

        content_type = (
            mimetypes.guess_type(
                file_path.name
            )[0]
            or "application/octet-stream"
        )

        headers = {
            "Content-Type": content_type,
            "Content-Length": str(
                local_size
            ),
        }

        # ----------------------------------------------------
        # Stream file to GitHub
        # ----------------------------------------------------

        with file_path.open(
            "rb"
        ) as file:

            if progress_callback:
                progress_callback(0, local_size)

            stream = _ProgressReader(
                file,
                local_size,
                progress_callback,
            )

            response = self.session.post(
                upload_url,
                params={
                    "name": asset_name,
                },
                headers=headers,
                data=stream,
                timeout=(30, 3600),
            )

            if progress_callback:
                progress_callback(local_size, local_size)

        if response.status_code != 201:

            raise RuntimeError(
                "GitHub release asset upload failed.\n"
                f"Status: {response.status_code}\n"
                f"Response: {response.text}"
            )

        asset = response.json()

        # ----------------------------------------------------
        # Verify SHA-256
        # ----------------------------------------------------

        remote_digest = asset.get(
            "digest"
        )

        if remote_digest is None:

            asset = (
                self.get_release_asset(
                    asset["id"]
                )
            )

            remote_digest = asset.get(
                "digest"
            )

        if remote_digest != expected_digest:

            raise RuntimeError(
                "CHUNK VERIFICATION FAILED.\n"
                f"Local:  {expected_digest}\n"
                f"GitHub: {remote_digest}"
            )

        return (
            release,
            asset,
        )

    # ========================================================
    # Get individual asset
    # ========================================================

    def get_release_asset(
        self,
        asset_id: int,
    ):

        self._require_repository()

        response = self.request(
            "GET",
            f"{GITHUB_API_URL}/repos/"
            f"{self.owner}/{self.repo}/releases/"
            f"assets/{asset_id}",
        )

        return response.json()

    # ========================================================
    # Download asset
    # ========================================================

    def download_asset(
        self,
        asset,
        destination: Path,
        progress_callback=None,
    ):

        self._require_repository()

        destination = Path(
            destination
        )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_path = (
            destination.with_suffix(
                destination.suffix
                + ".part"
            )
        )

        url = (
            f"{GITHUB_API_URL}/repos/"
            f"{self.owner}/{self.repo}/"
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
                    "Failed to download release asset.\n"
                    f"Status: {response.status_code}\n"
                    f"Response: {response.text}"
                )

            expected_size = asset.get("size")
            bytes_written = 0

            with temporary_path.open(
                "wb"
            ) as file:

                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):

                    if chunk:
                        file.write(
                            chunk
                        )
                        bytes_written += len(chunk)

                        if progress_callback:
                            progress_callback(
                                bytes_written,
                                int(expected_size)
                                if expected_size is not None
                                else None,
                            )

        temporary_path.replace(
            destination
        )

        return destination

    # ========================================================
    # Delete asset
    # ========================================================

    def delete_asset(
        self,
        asset_id: int,
    ):

        self._require_repository()

        response = self.session.delete(
            f"{GITHUB_API_URL}/repos/"
            f"{self.owner}/{self.repo}/releases/"
            f"assets/{asset_id}",
            timeout=(30, 300),
        )

        if response.status_code not in (
            204,
        ):

            raise RuntimeError(
                "Failed to delete release asset.\n"
                f"Status: {response.status_code}\n"
                f"Response: {response.text}"
            )

    def verify_asset_deleted(
        self,
        asset_id: int,
    ) -> bool:

        self._require_repository()

        response = self.session.get(
            f"{GITHUB_API_URL}/repos/"
            f"{self.owner}/{self.repo}/releases/"
            f"assets/{asset_id}",
            timeout=(30, 60),
        )

        if response.status_code == 404:
            return True

        if response.status_code == 200:
            return False

        raise RuntimeError(
            "Failed to verify release asset deletion.\n"
            f"Status: {response.status_code}\n"
            f"Response: {response.text}"
        )

    # ========================================================
    # Delete release
    # ========================================================

    def delete_release(
        self,
        release_id: int,
    ):

        self._require_repository()

        response = self.session.delete(
            f"{GITHUB_API_URL}/repos/"
            f"{self.owner}/{self.repo}/releases/"
            f"{release_id}",
            timeout=(30, 300),
        )

        if response.status_code not in (
            204,
        ):

            raise RuntimeError(
                "Failed to delete GitHub release.\n"
                f"Status: {response.status_code}\n"
                f"Response: {response.text}"
            )

    def verify_release_deleted(
        self,
        release_id: int,
    ) -> bool:

        self._require_repository()

        response = self.session.get(
            f"{GITHUB_API_URL}/repos/"
            f"{self.owner}/{self.repo}/releases/"
            f"{release_id}",
            timeout=(30, 60),
        )

        if response.status_code == 404:
            return True

        if response.status_code == 200:
            return False

        raise RuntimeError(
            "Failed to verify release deletion.\n"
            f"Status: {response.status_code}\n"
            f"Response: {response.text}"
        )

    # ========================================================
    # Delete release tag
    # ========================================================

    def delete_release_tag(
        self,
        tag_name: str,
    ):

        self._require_repository()

        response = self.session.delete(
            f"{GITHUB_API_URL}/repos/"
            f"{self.owner}/{self.repo}/git/refs/tags/"
            f"{tag_name}",
            timeout=(30, 300),
        )

        if response.status_code not in (
            204,
            404,
        ):

            raise RuntimeError(
                "Failed to delete GitHub release tag.\n"
                f"Tag: {tag_name}\n"
                f"Status: {response.status_code}\n"
                f"Response: {response.text}"
            )

    def verify_tag_deleted(
        self,
        tag_name: str,
    ) -> bool:

        self._require_repository()

        response = self.session.get(
            f"{GITHUB_API_URL}/repos/"
            f"{self.owner}/{self.repo}/git/ref/tags/"
            f"{tag_name}",
            timeout=(30, 60),
        )

        if response.status_code == 404:
            return True

        if response.status_code == 200:
            return False

        raise RuntimeError(
            "Failed to verify Git tag deletion.\n"
            f"Tag: {tag_name}\n"
            f"Status: {response.status_code}\n"
            f"Response: {response.text}"
        )

    # ========================================================
    # Delete complete release
    # ========================================================

    def delete_complete_release(
        self,
        release,
    ):

        release_id = release[
            "id"
        ]

        tag_name = release[
            "tag_name"
        ]

        # ----------------------------------------------------
        # 1. Delete every release asset
        # ----------------------------------------------------

        assets = (
            self.list_release_assets(
                release_id
            )
        )

        for asset in assets:

            self.delete_asset(
                asset["id"]
            )

            if not self.verify_asset_deleted(
                asset["id"]
            ):

                raise RuntimeError(
                    "Release asset still exists "
                    "after deletion.\n"
                    f"Asset: {asset['name']}"
                )

        # ----------------------------------------------------
        # 2. Delete the release
        # ----------------------------------------------------

        self.delete_release(
            release_id
        )

        if not self.verify_release_deleted(
            release_id
        ):

            raise RuntimeError(
                "GitHub release still exists "
                "after deletion."
            )

        # ----------------------------------------------------
        # 3. Delete the release tag
        # ----------------------------------------------------

        self.delete_release_tag(
            tag_name
        )

        # ----------------------------------------------------
        # 4. Verify tag
        # ----------------------------------------------------

        if not self.verify_tag_deleted(
            tag_name
        ):

            raise RuntimeError(
                "GitHub release tag still exists "
                "after deletion.\n"
                f"Tag: {tag_name}"
            )


# ============================================================
# Module test
# ============================================================

if __name__ == "__main__":

    print(
        "github.py is a library module."
    )

    print(
        "It is not intended to be run directly."
    )