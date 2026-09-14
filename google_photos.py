from __future__ import annotations

import json
import time
from typing import Any

import requests
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request


# ============================================================
# Configuration
# ============================================================

PICKER_API_URL = (
    "https://photospicker.googleapis.com/v1"
)

PICKER_SCOPE = (
    "https://www.googleapis.com/auth/"
    "photospicker.mediaitems.readonly"
)


# ============================================================
# Google Photos Client
# ============================================================

class GooglePhotosClient:

    def __init__(
        self,
        token_data: str | dict[str, Any],
    ):
        """
        token_data can be either:

        1. JSON string containing Google OAuth credentials
        2. Python dictionary containing the same data
        """

        if isinstance(
            token_data,
            str,
        ):

            try:

                token_data = json.loads(
                    token_data
                )

            except json.JSONDecodeError as exc:

                raise ValueError(
                    "Invalid Google token JSON."
                ) from exc

        if not isinstance(
            token_data,
            dict,
        ):

            raise TypeError(
                "Google token data must be "
                "a dictionary or JSON string."
            )

        self.credentials = (
            Credentials.from_authorized_user_info(
                token_data,
                scopes=[PICKER_SCOPE],
            )
        )

        self.refresh_if_needed()

        self.session = requests.Session()

        self._update_session_headers()

    # ========================================================
    # Authentication
    # ========================================================

    def refresh_if_needed(
        self,
    ):

        if (
            self.credentials.expired
            and self.credentials.refresh_token
        ):

            self.credentials.refresh(
                Request()
            )

    def _update_session_headers(
        self,
    ):

        self.session.headers.update({
            "Authorization": (
                f"Bearer {self.credentials.token}"
            ),
            "Accept": "application/json",
        })

    # ========================================================
    # Picker session
    # ========================================================

    def create_picker_session(
        self,
        max_item_count: int = 2000,
    ):

        if max_item_count < 1:
            raise ValueError(
                "max_item_count must be positive."
            )

        if max_item_count > 2000:

            raise ValueError(
                "Google Photos Picker currently "
                "allows at most 2000 selected items "
                "per session."
            )

        self.refresh_if_needed()
        self._update_session_headers()

        response = self.session.post(
            f"{PICKER_API_URL}/sessions",
            json={
                "pickingConfig": {
                    "maxItemCount": max_item_count,
                }
            },
            timeout=30,
        )

        if response.status_code != 200:

            raise RuntimeError(
                "Failed to create Google Photos "
                "Picker session.\n"
                f"Status: {response.status_code}\n"
                f"Response: {response.text}"
            )

        return response.json()

    # ========================================================
    # Get Picker session
    # ========================================================

    def get_picker_session(
        self,
        session_id: str,
    ):

        self.refresh_if_needed()
        self._update_session_headers()

        response = self.session.get(
            f"{PICKER_API_URL}/sessions/"
            f"{session_id}",
            timeout=30,
        )

        if response.status_code != 200:

            raise RuntimeError(
                "Failed to retrieve Google Photos "
                "Picker session.\n"
                f"Status: {response.status_code}\n"
                f"Response: {response.text}"
            )

        return response.json()

    # ========================================================
    # Poll Picker session
    # ========================================================

    def wait_for_selection(
        self,
        session_id: str,
        poll_interval: float = 2.0,
        timeout_seconds: int = 1800,
    ):

        started = time.monotonic()

        while True:

            if (
                time.monotonic() - started
                > timeout_seconds
            ):

                raise TimeoutError(
                    "Google Photos Picker session "
                    "timed out."
                )

            session = (
                self.get_picker_session(
                    session_id
                )
            )

            if session.get(
                "mediaItemsSet"
            ):

                return session

            time.sleep(
                poll_interval
            )

    # ========================================================
    # List selected media
    # ========================================================

    def list_selected_media(
        self,
        session_id: str,
        page_size: int = 100,
    ):

        if page_size < 1:
            raise ValueError(
                "page_size must be positive."
            )

        if page_size > 100:
            page_size = 100

        all_items = []

        page_token = None

        while True:

            self.refresh_if_needed()
            self._update_session_headers()

            params = {
                "sessionId": session_id,
                "pageSize": page_size,
            }

            if page_token:
                params[
                    "pageToken"
                ] = page_token

            response = self.session.get(
                f"{PICKER_API_URL}/mediaItems",
                params=params,
                timeout=30,
            )

            if response.status_code != 200:

                raise RuntimeError(
                    "Failed to retrieve selected "
                    "Google Photos media.\n"
                    f"Status: {response.status_code}\n"
                    f"Response: {response.text}"
                )

            data = response.json()

            batch = data.get(
                "mediaItems",
                [],
            )

            all_items.extend(
                batch
            )

            page_token = data.get(
                "nextPageToken"
            )

            if not page_token:
                break

        return all_items

    # ========================================================
    # Get individual media item
    # ========================================================

    def get_media_item(
        self,
        session_id: str,
        media_item_id: str,
    ):

        items = self.list_selected_media(
            session_id
        )

        for item in items:

            if item.get(
                "id"
            ) == media_item_id:

                return item

        return None

    # ========================================================
    # Download selected media
    # ========================================================

    def download_media(
        self,
        media_item: dict[str, Any],
        destination,
        progress_callback=None,
    ):
        """
        Download the bytes exposed by the Google Photos Picker API.

        Important:
        - This code performs NO compression, resizing, conversion, or
          re-encoding on our side.
        - Google requires `d` for image downloads and `dv` for video
          downloads.
        - Google's Picker `dv` endpoint returns a high-quality transcoded
          version of a video, so the API itself does not guarantee the
          original uploaded video bytes.
        """

        media_file = media_item.get("mediaFile")

        if not media_file:
            raise RuntimeError(
                "Selected media item does not contain mediaFile."
            )

        base_url = media_file.get("baseUrl")
        filename = media_file.get("filename")
        mime_type = (
            media_file.get("mimeType")
            or media_item.get("mimeType")
            or ""
        )

        if not base_url:
            raise RuntimeError(
                "Selected media item does not contain baseUrl."
            )

        if not filename:
            raise RuntimeError(
                "Selected media item does not contain filename."
            )

        # --------------------------------------------------------
        # Picker API media type
        # --------------------------------------------------------
        # Picker API explicitly provides:
        #   type = PHOTO or VIDEO
        #
        # Video metadata is under:
        #   mediaFile.mediaFileMetadata.videoMetadata
        #
        # Do NOT use the Library-API-style media_item["mediaMetadata"].
        # --------------------------------------------------------
        media_type = str(
            media_item.get("type") or ""
        ).upper()

        media_file_metadata = media_file.get(
            "mediaFileMetadata",
            {}
        )

        video_metadata = media_file_metadata.get(
            "videoMetadata",
            {}
        )

        is_video = (
            media_type == "VIDEO"
            or str(mime_type).lower().startswith("video/")
            or bool(video_metadata)
        )

        # --------------------------------------------------------
        # A video must be READY before requesting `dv`.
        # Picker API exposes the processing state as
        # videoMetadata.processingStatus.
        # --------------------------------------------------------
        if is_video:
            status = video_metadata.get(
                "processingStatus"
            )

            if status is None:
                status = video_metadata.get(
                    "status"
                )

            if status and str(status).upper() != "READY":
                raise RuntimeError(
                    f"Google Photos video '{filename}' is not ready. "
                    f"Processing status: {status}"
                )

        self.refresh_if_needed()
        self._update_session_headers()

        destination = str(destination)
        temporary_path = destination + ".part"

        # --------------------------------------------------------
        # Construct the Google-required base URL.
        #
        # Do NOT send d/dv through requests params. Google's
        # Picker documentation specifies concatenating the
        # parameter to the base URL:
        #
        #   image -> baseUrl=d
        #   video -> baseUrl=dv
        # --------------------------------------------------------
        parameter = "dv" if is_video else "d"
        download_url = f"{base_url}={parameter}"

        try:
            with self.session.get(
                download_url,
                stream=True,
                timeout=(30, 3600),
                allow_redirects=True,
            ) as response:

                if response.status_code != 200:
                    raise RuntimeError(
                        f"Failed to download '{filename}'.\n"
                        f"Status: {response.status_code}\n"
                        f"Response: {response.text[:1000]}"
                    )

                content_type = (
                    response.headers.get("Content-Type")
                    or ""
                ).lower()

                if is_video and (
                    not content_type.startswith("video/")
                    and "application/octet-stream" not in content_type
                ):
                    raise RuntimeError(
                        f"Unexpected Content-Type for video "
                        f"'{filename}': {content_type!r}. "
                        f"Expected video/* or application/octet-stream."
                    )

                expected_size_header = response.headers.get(
                    "Content-Length"
                )

                expected_size = None

                if expected_size_header:
                    try:
                        expected_size = int(
                            expected_size_header
                        )
                    except ValueError:
                        expected_size = None

                Path(destination).parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                bytes_written = 0

                with open(
                    temporary_path,
                    "wb",
                ) as file:

                    for chunk in response.iter_content(
                        chunk_size=1024 * 1024,
                    ):
                        if not chunk:
                            continue

                        file.write(chunk)
                        bytes_written += len(chunk)
                        if progress_callback is not None:
                            progress_callback(bytes_written, expected_size)

                # Never accept an empty media file.
                if bytes_written <= 0:
                    raise RuntimeError(
                        f"Google Photos returned zero bytes for "
                        f"'{filename}'."
                    )

                # If Google supplied Content-Length, make sure we
                # received exactly that many bytes.
                if (
                    expected_size is not None
                    and bytes_written != expected_size
                ):
                    raise RuntimeError(
                        f"Incomplete download for '{filename}'. "
                        f"Expected {expected_size:,} bytes but "
                        f"received {bytes_written:,} bytes."
                    )

            import os

            os.replace(
                temporary_path,
                destination,
            )

            actual_size = os.path.getsize(
                destination
            )

            return {
                "path": destination,
                "filename": filename,
                "mime_type": mime_type or None,
                "size": actual_size,
                "download_parameter": parameter,
                "google_base_url": base_url,
            }

        except Exception:
            # Never leave a partial media file behind.
            try:
                Path(temporary_path).unlink(
                    missing_ok=True
                )
            except Exception:
                pass

            raise

    # ========================================================
    # Delete Picker session
    # ========================================================

    def delete_picker_session(
        self,
        session_id: str,
    ):

        self.refresh_if_needed()
        self._update_session_headers()

        response = self.session.delete(
            f"{PICKER_API_URL}/sessions/"
            f"{session_id}",
            timeout=30,
        )

        if response.status_code not in (
            200,
            204,
        ):

            raise RuntimeError(
                "Failed to delete Google Photos "
                "Picker session.\n"
                f"Status: {response.status_code}\n"
                f"Response: {response.text}"
            )

    # ========================================================
    # Utility
    # ========================================================

    def get_picker_url(
        self,
        picker_session: dict[str, Any],
    ) -> str:

        picker_uri = picker_session.get(
            "pickerUri"
        )

        if not picker_uri:

            raise RuntimeError(
                "Picker session did not contain "
                "pickerUri."
            )

        return picker_uri


# ============================================================
# Module test
# ============================================================

if __name__ == "__main__":

    print(
        "google_photos.py is a backend module."
    )

    print(
        "It is not intended to be run directly."
    )