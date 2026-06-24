"""Phone caller with Twilio and rate limiting.

Usage:
    from tkbase_phone_caller import PhoneCaller

    caller = PhoneCaller(
        rate_limit=5,     # max 5/min
        daily_limit=50,   # max 50/day
    )
    result = caller.call_sync(
        to="+819012345678",
        message="会議が15分後に始まります。",
    )

Credentials default to ~/.config/tkbase/ (shared across all tkbase repos).
Override with account_sid/auth_token params if needed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import structlog

from .rate_limiter import RateLimiter

logger = structlog.get_logger()

_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "tkbase"


class PhoneCaller:
    """Make phone calls via Twilio with rate limiting.

    Args:
        account_sid: Twilio Account SID. Defaults to reading from credentials file.
        auth_token: Twilio Auth Token. Defaults to reading from credentials file.
        from_number: Twilio phone number to call from. Defaults to credentials file.
        credentials_path: Path to Twilio credentials JSON.
            Defaults to ~/.config/tkbase/twilio_credentials.json.
        rate_limit: Max calls per minute (default 5, 0=unlimited).
        daily_limit: Max calls per day (default 50, 0=unlimited).
        dry_run: If True, log but don't actually call.
        state_dir: Directory to persist rate limit state across restarts.
        voice: Twilio voice for TTS (default "Polly.Mizuki" for Japanese).
        language: TTS language code (default "ja-JP").
    """

    def __init__(
        self,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        from_number: Optional[str] = None,
        credentials_path: str | Path | None = None,
        rate_limit: int = 5,
        daily_limit: int = 50,
        dry_run: bool = False,
        state_dir: str | Path | None = None,
        voice: str = "Polly.Mizuki",
        language: str = "ja-JP",
    ):
        self._credentials_path = (
            Path(credentials_path) if credentials_path
            else _DEFAULT_CONFIG_DIR / "twilio_credentials.json"
        )
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_number = from_number
        self._dry_run = dry_run
        self._voice = voice
        self._language = language
        self._client = None

        state_path = None
        if state_dir:
            state_path = Path(state_dir) / "phone_call_state.json"
        self._limiter = RateLimiter(
            rate_limit=rate_limit,
            daily_limit=daily_limit,
            state_path=state_path,
        )

    def _load_credentials(self) -> tuple[str, str, str]:
        """Load Twilio credentials from config file or instance vars."""
        sid = self._account_sid
        token = self._auth_token
        from_num = self._from_number

        if sid and token and from_num:
            return sid, token, from_num

        if not self._credentials_path.exists():
            raise FileNotFoundError(
                f"Twilio credentials not found at {self._credentials_path}. "
                "Create a JSON file with account_sid, auth_token, and from_number."
            )

        data = json.loads(self._credentials_path.read_text())
        sid = sid or data["account_sid"]
        token = token or data["auth_token"]
        from_num = from_num or data["from_number"]
        return sid, token, from_num

    def _get_client(self):
        """Build Twilio client."""
        if self._client is not None:
            return self._client

        from twilio.rest import Client

        sid, token, _ = self._load_credentials()
        self._client = Client(sid, token)
        return self._client

    def _build_twiml(self, message: str) -> str:
        """Build TwiML for text-to-speech call."""
        escaped = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f'<Say voice="{self._voice}" language="{self._language}">'
            f"{escaped}"
            "</Say>"
            "</Response>"
        )

    def call_sync(
        self,
        to: str,
        message: str,
        voice: Optional[str] = None,
        language: Optional[str] = None,
        twiml: Optional[str] = None,
    ) -> dict:
        """Make a phone call synchronously.

        Args:
            to: Phone number to call (E.164 format, e.g. "+819012345678").
            message: Text message to speak via TTS.
            voice: Override default TTS voice for this call.
            language: Override default TTS language for this call.
            twiml: Raw TwiML string (overrides message/voice/language).

        Returns:
            Dict with call SID and status.
            In dry_run mode, returns {"sid": "dry_run", "dry_run": True}.

        Raises:
            RateLimitExceeded: If rate or daily limit is hit.
        """
        self._limiter.check()

        if self._dry_run:
            logger.info(
                "phone.dry_run",
                to=to,
                message=message[:50],
                remaining_today=self._limiter.remaining_today,
            )
            self._limiter.record_call()
            return {"sid": "dry_run", "dry_run": True}

        client = self._get_client()
        _, _, from_number = self._load_credentials()

        if twiml is None:
            v = voice or self._voice
            lang = language or self._language
            twiml = self._build_twiml(message)

        call = client.calls.create(
            to=to,
            from_=from_number,
            twiml=twiml,
        )

        self._limiter.record_call()
        logger.info(
            "phone.called",
            to=to,
            message=message[:50],
            call_sid=call.sid,
            status=call.status,
            remaining_today=self._limiter.remaining_today,
        )
        return {"sid": call.sid, "status": call.status}

    async def call(
        self,
        to: str,
        message: str,
        voice: Optional[str] = None,
        language: Optional[str] = None,
        twiml: Optional[str] = None,
    ) -> dict:
        """Make a phone call asynchronously (runs sync in thread pool)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.call_sync(to, message, voice, language, twiml),
        )

    @property
    def remaining_today(self) -> int:
        """Number of calls remaining today (-1 if unlimited)."""
        return self._limiter.remaining_today

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @dry_run.setter
    def dry_run(self, value: bool) -> None:
        self._dry_run = value
