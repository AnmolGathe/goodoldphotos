from __future__ import annotations

from pathlib import Path


from database import User

from github import (
    MAX_ASSETS_PER_RELEASE,
)

from storage import (
    get_configured_storage,
)


# ============================================================
# Album Manager
# ============================================================

class AlbumManager:

    def __init__(
        self,
        user: User,
    ):
        self.user = user

        self.github = (
            get_configured_storage(
                user
            )
        )

    # ========================================================
    # Albums
    # ========================================================

    def list_albums(self):
        """
        Return all backup releases for this user.

        In our storage model:

            GitHub Release = Album
        """

        releases = (
            self.github.list_backup_releases()
        )

        albums = []

        for release in releases:

            assets = (
                self.github.list_release_assets(
                    release["id"]
                )
            )

            total_size = sum(
                asset.get(
                    "size",
                    0,
                )
                for asset in assets
            )

            albums.append({
                "id": release[
                    "id"
                ],
                "name": release[
                    "name"
                ],
                "tag": release[
                    "tag_name"
                ],
                "url": release.get(
                    "html_url"
                ),
                "created_at": release.get(
                    "created_at"
                ),
                "published_at": release.get(
                    "published_at"
                ),
                "chunks": len(
                    assets
                ),
                "total_size": total_size,
                "max_chunks": MAX_ASSETS_PER_RELEASE,
            })

        return albums

    # ========================================================
    # Get album
    # ========================================================

    def get_album(
        self,
        release_id: int,
    ):
        """
        Return one release and all of its chunks.
        """

        release = (
            self.github.get_release(
                release_id
            )
        )

        # ----------------------------------------------------
        # Make sure this is one of our backup releases.
        # ----------------------------------------------------

        tag_name = release.get(
            "tag_name",
            "",
        )

        if not tag_name.startswith(
            "photos-backup-"
        ):

            raise ValueError(
                "The requested GitHub release "
                "is not a MyGoodOldPhotos album."
            )

        assets = (
            self.github.list_release_assets(
                release_id
            )
        )

        chunks = []

        for asset in assets:

            chunks.append({
                "id": asset[
                    "id"
                ],
                "name": asset[
                    "name"
                ],
                "size": asset.get(
                    "size",
                    0,
                ),
                "content_type": asset.get(
                    "content_type"
                ),
                "download_url": asset.get(
                    "browser_download_url"
                ),
                "digest": asset.get(
                    "digest"
                ),
                "created_at": asset.get(
                    "created_at"
                ),
                "updated_at": asset.get(
                    "updated_at"
                ),
            })

        return {
            "id": release[
                "id"
            ],
            "name": release[
                "name"
            ],
            "tag": release[
                "tag_name"
            ],
            "url": release.get(
                "html_url"
            ),
            "created_at": release.get(
                "created_at"
            ),
            "published_at": release.get(
                "published_at"
            ),
            "chunks": chunks,
            "chunk_count": len(
                chunks
            ),
        }

    # ========================================================
    # Find chunk
    # ========================================================

    def get_chunk(
        self,
        release_id: int,
        asset_id: int,
    ):
        """
        Find a particular release asset.
        """

        album = self.get_album(
            release_id
        )

        for chunk in album[
            "chunks"
        ]:

            if chunk[
                "id"
            ] == asset_id:

                return {
                    "album_id": album[
                        "id"
                    ],
                    "album_name": album[
                        "name"
                    ],
                    "album_tag": album[
                        "tag"
                    ],
                    **chunk,
                }

        raise ValueError(
            "Chunk was not found in the requested album."
        )

    # ========================================================
    # Download one chunk
    # ========================================================

    def download_chunk(
        self,
        release_id: int,
        asset_id: int,
        destination: Path,
    ):
        """
        Download one release asset.

        The downloaded file is returned.
        """

        chunk = self.get_chunk(
            release_id,
            asset_id,
        )

        # We deliberately construct the GitHub asset
        # dictionary required by GitHubClient.download_asset().
        github_asset = {
            "id": chunk[
                "id"
            ],
            "name": chunk[
                "name"
            ],
            "size": chunk[
                "size"
            ],
        }

        destination = Path(
            destination
        )

        return self.github.download_asset(
            github_asset,
            destination,
        )

    # ========================================================
    # Download complete album
    # ========================================================

    def download_album(
        self,
        release_id: int,
        destination: Path,
    ):
        """
        Download every chunk in an album.

        Returns a list of downloaded files.
        """

        album = self.get_album(
            release_id
        )

        destination = Path(
            destination
        )

        destination.mkdir(
            parents=True,
            exist_ok=True,
        )

        downloaded_files = []

        for chunk in album[
            "chunks"
        ]:

            github_asset = {
                "id": chunk[
                    "id"
                ],
                "name": chunk[
                    "name"
                ],
                "size": chunk[
                    "size"
                ],
            }

            output_path = (
                destination
                / chunk[
                    "name"
                ]
            )

            downloaded = (
                self.github.download_asset(
                    github_asset,
                    output_path,
                )
            )

            downloaded_files.append(
                downloaded
            )

        return downloaded_files

    # ========================================================
    # Delete one chunk
    # ========================================================

    def delete_chunk(
        self,
        release_id: int,
        asset_id: int,
    ):
        """
        Permanently delete one release asset and verify
        that it no longer exists.
        """

        chunk = self.get_chunk(
            release_id,
            asset_id,
        )

        self.github.delete_asset(
            asset_id
        )

        deleted = (
            self.github.verify_asset_deleted(
                asset_id
            )
        )

        if not deleted:

            raise RuntimeError(
                "GitHub asset still exists "
                "after deletion."
            )

        return {
            "deleted": True,
            "album_id": release_id,
            "asset_id": asset_id,
            "asset_name": chunk[
                "name"
            ],
        }

    # ========================================================
    # Delete complete album
    # ========================================================

    def delete_album(
        self,
        release_id: int,
    ):
        """
        Permanently delete:

            all release assets
            release
            release tag

        The GitHubClient performs deletion verification.
        """

        album = self.get_album(
            release_id
        )

        self.github.delete_complete_release(
            {
                "id": album[
                    "id"
                ],
                "name": album[
                    "name"
                ],
                "tag_name": album[
                    "tag"
                ],
            }
        )

        return {
            "deleted": True,
            "album_id": release_id,
            "album_name": album[
                "name"
            ],
            "tag": album[
                "tag"
            ],
            "deleted_chunks": album[
                "chunk_count"
            ],
        }