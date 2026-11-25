"""
YouTube Data API v3 transport client.

NOTE: Uses dynamic ``importlib.import_module(...)`` intentionally so static package
imports never trigger top-level google-api client dependencies when YouTube is unused.
"""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.core.errors import (
    DependencyNotFoundError,
    DomainValidationError,
    ProviderIntegrityError,
    ProviderTimeoutError,
)
from src.core.logger import logger

TRANSIENT_UPLOAD_STATUSES = frozenset({"uploaded", "processing"})


class YouTubeDataClient:
    """Thin API transport adapter wrapping dynamic ``googleapiclient`` service calls."""

    def __init__(self, creds: Any, *, sleep_fn: Callable[[float], None] = time.sleep) -> None:
        try:
            discovery_mod = importlib.import_module("googleapiclient.discovery")
            http_mod = importlib.import_module("googleapiclient.http")
        except ImportError as exc:
            raise DependencyNotFoundError(
                "Missing googleapiclient. Install with: pip install google-api-python-client"
            ) from exc

        self._build = discovery_mod.build
        self._media_cls = http_mod.MediaFileUpload
        self._service = self._build("youtube", "v3", credentials=creds)
        self._sleep = sleep_fn

    @property
    def service(self) -> Any:
        return self._service

    def fetch_mine_channel_info(self) -> Tuple[Optional[str], Optional[str]]:
        """Best-effort query returning ``(channel_id, channel_title)`` for authorized token."""
        try:
            res = self._service.channels().list(mine=True, part="snippet").execute()
            if isinstance(res, dict) and isinstance(res.get("items"), list) and res["items"]:
                first_ch = res["items"][0]
                if isinstance(first_ch, dict):
                    return (
                        first_ch.get("id"),
                        first_ch.get("snippet", {}).get("title"),
                    )
        except Exception:
            pass
        return None, None

    def insert_video(
        self,
        *,
        video_path: Path,
        title: str,
        description: str,
        tags: List[str],
        category_id: str,
        privacy_status: str,
        made_for_kids: bool,
    ) -> str:
        """Uploads an MP4 file via resumable chunked transfer and returns YouTube ``video_id``."""
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": made_for_kids,
            },
        }
        media = self._media_cls(
            str(video_path),
            mimetype="video/mp4",
            resumable=True,
            chunksize=2 * 1024 * 1024,
        )
        logger.info(f"Starting YouTube upload: {title} ({video_path.name})")
        request = self._service.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                logger.info(f"Upload progress: {progress}%")
        video_id = response.get("id") if isinstance(response, dict) else None
        if not video_id:
            raise ProviderIntegrityError(
                "YouTube videos().insert returned empty video ID.",
                category="invalid_response",
                provider="youtube",
            )
        return str(video_id)

    def upload_captions(
        self,
        *,
        video_id: str,
        subtitle_path: Path,
        language: str = "vi",
        name: str = "Tiếng Việt",
    ) -> None:
        """Attaches closed captions SRT track to an existing uploaded YouTube video."""
        caption_body = {
            "snippet": {
                "videoId": video_id,
                "language": language,
                "name": name,
                "isDraft": False,
            }
        }
        caption_media = self._media_cls(str(subtitle_path), mimetype="application/x-subrip")
        self._service.captions().insert(
            part="snippet",
            body=caption_body,
            media_body=caption_media,
        ).execute()

    def set_thumbnail(self, *, video_id: str, thumbnail_path: Path) -> None:
        """Uploads and binds a custom JPEG or PNG thumbnail to a YouTube video."""
        thumb_mimetype = "image/png" if thumbnail_path.suffix.lower() == ".png" else "image/jpeg"
        self._service.thumbnails().set(
            videoId=video_id,
            media_body=self._media_cls(str(thumbnail_path), mimetype=thumb_mimetype),
        ).execute()

    def wait_for_processing(
        self,
        *,
        video_id: str,
        account: str,
        timeout: float,
        poll_interval: float,
        max_backoff: float,
        max_attempts: int,
    ) -> Tuple[Dict[str, Any], int]:
        """Polls YouTube video processing until ``processed`` or terminal/timeout error."""
        deadline = time.monotonic() + timeout
        interval = float(poll_interval)
        for attempt in range(1, max_attempts + 1):
            try:
                response = self._service.videos().list(
                    part="status,snippet,contentDetails",
                    id=video_id,
                ).execute()
            except Exception as exc:
                raise DomainValidationError(f"Could not check YouTube video {video_id}: {exc}") from exc
            items = response.get("items", [])
            if not items:
                raise DomainValidationError(f"YouTube video {video_id} was not found on account {account}")
            status = items[0].get("status", {})
            state = str(status.get("uploadStatus", "")).strip()
            if state == "processed":
                return items[0], attempt
            if state not in TRANSIENT_UPLOAD_STATUSES:
                reason = status.get("rejectionReason") or status.get("failureReason") or "unknown"
                raise DomainValidationError(
                    f"YouTube processing failed for {video_id}: {state} ({reason})"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0 or attempt >= max_attempts:
                raise DomainValidationError(
                    f"Timed out waiting for YouTube video {video_id} (last status: {state})"
                )
            if interval > 0:
                self._sleep(min(interval, remaining))
                interval = min(max_backoff, interval * 1.5)
        raise ProviderTimeoutError(
            f"Timed out waiting for YouTube video {video_id}",
            category="timeout",
            provider="youtube",
        )

    def list_captions(self, *, video_id: str) -> List[Dict[str, Any]]:
        """Lists caption snippet items associated with a YouTube video."""
        res = self._service.captions().list(part="snippet", videoId=video_id).execute()
        return list(res.get("items", []))
