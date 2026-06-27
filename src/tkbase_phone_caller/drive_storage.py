"""Google Drive storage for call recordings.

Downloads Vapi call recordings and uploads them to Google Drive.
All recordings are stored in a dedicated folder for easy access.

Credentials: reuses ~/.config/tkbase/google_credentials.json
(same OAuth2 flow as meet.py, but with Drive scope added).
"""

from __future__ import annotations

import io
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger()

_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "tkbase"
_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/calendar.events",
]

_FOLDER_NAME = "Phone Caller Recordings"


def _load_google_creds(
    credentials_path: Path | None = None,
    token_path: Path | None = None,
) -> Any:
    """Load Google API credentials with Drive + Calendar scopes."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    cred_file = credentials_path or (_DEFAULT_CONFIG_DIR / "google_credentials.json")
    tok_file = token_path or (_DEFAULT_CONFIG_DIR / "google_token.json")

    creds = None
    if tok_file.exists():
        creds = Credentials.from_authorized_user_file(str(tok_file), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not cred_file.exists():
                raise FileNotFoundError(
                    f"Google credentials not found at {cred_file}. "
                    "Download OAuth2 client credentials from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(cred_file), _SCOPES)
            creds = flow.run_local_server(port=0)
        tok_file.parent.mkdir(parents=True, exist_ok=True)
        tok_file.write_text(creds.to_json())

    return creds


class DriveStorage:
    """Upload call recordings to Google Drive.

    Args:
        credentials_path: Path to Google OAuth2 credentials JSON.
        token_path: Path to store/load OAuth2 token.
        folder_name: Drive folder name for recordings.
    """

    def __init__(
        self,
        credentials_path: str | Path | None = None,
        token_path: str | Path | None = None,
        folder_name: str = _FOLDER_NAME,
    ):
        self._credentials_path = (
            Path(credentials_path) if credentials_path else None
        )
        self._token_path = Path(token_path) if token_path else None
        self._folder_name = folder_name
        self._service = None
        self._folder_id: str | None = None

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service

        from googleapiclient.discovery import build

        creds = _load_google_creds(self._credentials_path, self._token_path)
        self._service = build("drive", "v3", credentials=creds)
        return self._service

    def _ensure_folder(self) -> str:
        """Get or create the recordings folder in Drive."""
        if self._folder_id is not None:
            return self._folder_id

        service = self._get_service()

        results = (
            service.files()
            .list(
                q=(
                    f"name = '{self._folder_name}' "
                    "and mimeType = 'application/vnd.google-apps.folder' "
                    "and trashed = false"
                ),
                spaces="drive",
                fields="files(id, name)",
                pageSize=1,
            )
            .execute()
        )
        files = results.get("files", [])

        if files:
            self._folder_id = files[0]["id"]
        else:
            folder_metadata = {
                "name": self._folder_name,
                "mimeType": "application/vnd.google-apps.folder",
            }
            folder = (
                service.files()
                .create(body=folder_metadata, fields="id")
                .execute()
            )
            self._folder_id = folder["id"]
            logger.info("drive.folder_created", folder_id=self._folder_id)

        return self._folder_id

    async def upload_recording(
        self,
        recording_url: str,
        call_id: str,
        customer_number: str = "",
        duration_seconds: int = 0,
        ended_reason: str = "",
    ) -> dict[str, str]:
        """Download a recording from Vapi and upload to Google Drive.

        Args:
            recording_url: Vapi recording URL.
            call_id: Vapi call ID.
            customer_number: Customer phone number (for filename).
            duration_seconds: Call duration.
            ended_reason: How the call ended.

        Returns:
            Dict with file_id, file_name, and web_view_link.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_number = customer_number.replace("+", "").replace("-", "")
        filename = f"{timestamp}_{safe_number}_{call_id[:8]}.wav"

        # Download recording from Vapi
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.get(recording_url)
            resp.raise_for_status()
            audio_data = resp.content

        content_type = resp.headers.get("content-type", "audio/wav")
        if "mp3" in content_type or recording_url.endswith(".mp3"):
            filename = filename.replace(".wav", ".mp3")
        elif "mp4" in content_type or "m4a" in content_type:
            filename = filename.replace(".wav", ".m4a")

        # Upload to Drive
        return self._upload_bytes(
            data=audio_data,
            filename=filename,
            content_type=content_type,
            call_id=call_id,
            customer_number=customer_number,
            duration_seconds=duration_seconds,
            ended_reason=ended_reason,
        )

    def upload_recording_sync(
        self,
        recording_url: str,
        call_id: str,
        customer_number: str = "",
        duration_seconds: int = 0,
        ended_reason: str = "",
    ) -> dict[str, str]:
        """Sync version of upload_recording."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_number = customer_number.replace("+", "").replace("-", "")
        filename = f"{timestamp}_{safe_number}_{call_id[:8]}.wav"

        with httpx.Client(timeout=120.0) as client:
            resp = client.get(recording_url)
            resp.raise_for_status()
            audio_data = resp.content

        content_type = resp.headers.get("content-type", "audio/wav")
        if "mp3" in content_type or recording_url.endswith(".mp3"):
            filename = filename.replace(".wav", ".mp3")
        elif "mp4" in content_type or "m4a" in content_type:
            filename = filename.replace(".wav", ".m4a")

        return self._upload_bytes(
            data=audio_data,
            filename=filename,
            content_type=content_type,
            call_id=call_id,
            customer_number=customer_number,
            duration_seconds=duration_seconds,
            ended_reason=ended_reason,
        )

    def _upload_bytes(
        self,
        data: bytes,
        filename: str,
        content_type: str,
        call_id: str,
        customer_number: str,
        duration_seconds: int,
        ended_reason: str,
    ) -> dict[str, str]:
        """Upload bytes to Google Drive."""
        from googleapiclient.http import MediaInMemoryUpload

        service = self._get_service()
        folder_id = self._ensure_folder()

        description_parts = [
            f"Call ID: {call_id}",
            f"Customer: {customer_number}",
            f"Duration: {duration_seconds}s",
            f"Ended: {ended_reason}",
        ]

        file_metadata = {
            "name": filename,
            "parents": [folder_id],
            "description": " | ".join(description_parts),
        }

        media = MediaInMemoryUpload(data, mimetype=content_type, resumable=True)

        file = (
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, name, webViewLink",
            )
            .execute()
        )

        logger.info(
            "drive.uploaded",
            file_id=file["id"],
            filename=filename,
            size_bytes=len(data),
            call_id=call_id,
        )

        return {
            "file_id": file["id"],
            "file_name": file["name"],
            "web_view_link": file.get("webViewLink", ""),
        }

    def list_recordings(self, limit: int = 20) -> list[dict[str, str]]:
        """List recent recordings from Drive."""
        service = self._get_service()
        folder_id = self._ensure_folder()

        results = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                spaces="drive",
                fields="files(id, name, webViewLink, createdTime, description)",
                orderBy="createdTime desc",
                pageSize=limit,
            )
            .execute()
        )

        return [
            {
                "file_id": f["id"],
                "file_name": f["name"],
                "web_view_link": f.get("webViewLink", ""),
                "created": f.get("createdTime", ""),
                "description": f.get("description", ""),
            }
            for f in results.get("files", [])
        ]
