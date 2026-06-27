"""Google Meet integration for call transfers.

Creates a Google Meet room on-the-fly when AI needs to transfer a call.
The caller gets transferred to the Meet dial-in number, and you get
notified to join the same Meet via browser.

Credentials: ~/.config/tkbase/google_credentials.json
Expected format (service account):
    Standard Google service account JSON with domain-wide delegation,
    or OAuth2 credentials JSON.

Also needs: ~/.config/tkbase/google_token.json (auto-created after first OAuth flow).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "tkbase"

# Google Calendar API scopes
_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _load_google_creds(
    credentials_path: Path | None = None,
    token_path: Path | None = None,
) -> Any:
    """Load Google API credentials (OAuth2 flow)."""
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


class MeetRoom:
    """Represents a created Google Meet room."""

    def __init__(
        self,
        meet_url: str,
        dial_in_number: str,
        dial_in_pin: str,
        event_id: str,
        calendar_link: str = "",
    ):
        self.meet_url = meet_url
        self.dial_in_number = dial_in_number
        self.dial_in_pin = dial_in_pin
        self.event_id = event_id
        self.calendar_link = calendar_link

    @property
    def transfer_destination(self) -> str:
        """Phone number + PIN for Vapi transferCall.

        Vapi can transfer to a phone number. The dial-in number
        is the Meet phone bridge. After connecting, the PIN is
        entered via DTMF tones.
        """
        return self.dial_in_number

    def to_dict(self) -> dict[str, str]:
        return {
            "meet_url": self.meet_url,
            "dial_in_number": self.dial_in_number,
            "dial_in_pin": self.dial_in_pin,
            "event_id": self.event_id,
            "calendar_link": self.calendar_link,
        }


class MeetCreator:
    """Create Google Meet rooms for call transfers.

    Args:
        credentials_path: Path to Google OAuth2 credentials JSON.
        token_path: Path to store/load OAuth2 token.
        calendar_id: Google Calendar ID (default "primary").
        meeting_duration_minutes: How long the Meet stays open.
        dial_in_region: Region for dial-in number (default "JP").
    """

    def __init__(
        self,
        credentials_path: str | Path | None = None,
        token_path: str | Path | None = None,
        calendar_id: str = "primary",
        meeting_duration_minutes: int = 30,
        dial_in_region: str = "JP",
    ):
        self._credentials_path = (
            Path(credentials_path) if credentials_path else None
        )
        self._token_path = Path(token_path) if token_path else None
        self._calendar_id = calendar_id
        self._duration = meeting_duration_minutes
        self._region = dial_in_region
        self._service = None

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service

        from googleapiclient.discovery import build

        creds = _load_google_creds(self._credentials_path, self._token_path)
        self._service = build("calendar", "v3", credentials=creds)
        return self._service

    def create_room(
        self,
        title: str = "営業通話 — 転送",
        attendee_email: Optional[str] = None,
    ) -> MeetRoom:
        """Create a Google Meet room with dial-in enabled.

        Args:
            title: Calendar event title.
            attendee_email: Email to invite (you). Gets calendar notification.

        Returns:
            MeetRoom with URLs and dial-in info.
        """
        service = self._get_service()
        now = datetime.now(timezone.utc)
        end = now + timedelta(minutes=self._duration)

        event_body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": now.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
            "conferenceData": {
                "createRequest": {
                    "requestId": str(uuid.uuid4()),
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                },
            },
        }

        if attendee_email:
            event_body["attendees"] = [{"email": attendee_email}]

        event = (
            service.events()
            .insert(
                calendarId=self._calendar_id,
                body=event_body,
                conferenceDataVersion=1,
                sendUpdates="all" if attendee_email else "none",
            )
            .execute()
        )

        conf_data = event.get("conferenceData", {})
        entry_points = conf_data.get("entryPoints", [])

        meet_url = ""
        dial_number = ""
        dial_pin = ""

        for ep in entry_points:
            if ep.get("entryPointType") == "video":
                meet_url = ep.get("uri", "")
            elif ep.get("entryPointType") == "phone":
                region = ep.get("regionCode", "")
                if region == self._region or not dial_number:
                    dial_number = ep.get("uri", "").replace("tel:", "")
                    dial_pin = ep.get("pin", "")

        room = MeetRoom(
            meet_url=meet_url,
            dial_in_number=dial_number,
            dial_in_pin=dial_pin,
            event_id=event.get("id", ""),
            calendar_link=event.get("htmlLink", ""),
        )

        logger.info(
            "meet.created",
            meet_url=meet_url,
            dial_in=dial_number,
            event_id=event.get("id"),
        )
        return room

    def delete_room(self, event_id: str) -> None:
        """Clean up a Meet room after call ends."""
        service = self._get_service()
        service.events().delete(
            calendarId=self._calendar_id,
            eventId=event_id,
        ).execute()
        logger.info("meet.deleted", event_id=event_id)
