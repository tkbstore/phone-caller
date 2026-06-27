"""AI phone caller with Vapi REST API and rate limiting.

Usage:
    from tkbase_phone_caller import PhoneCaller

    caller = PhoneCaller(
        rate_limit=5,     # max 5/min
        daily_limit=50,   # max 50/day
    )
    result = await caller.call(
        to="+819012345678",
        prompt="御社のDX推進についてご提案があります。",
    )

Transfer modes:
    - "phone": Transfer to a phone number (default)
    - "meet": Create a Google Meet and transfer to dial-in
    - "webrtc": Notify via webhook for browser-based pickup

Credentials default to ~/.config/tkbase/vapi_credentials.json.
Expected format:
    {
        "api_key": "your-vapi-api-key",
        "phone_number_id": "your-vapi-phone-number-id",
        "transfer_number": "+8190XXXXXXXX",
        "transfer_mode": "phone",
        "meet_attendee_email": "you@example.com"
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog

from .phone_router import PhoneRouter
from .prompts import build_sales_prompt
from .rate_limiter import RateLimiter

logger = structlog.get_logger()

_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "tkbase"
_VAPI_BASE_URL = "https://api.vapi.ai"

# Vapi assistant defaults optimized for low latency
_DEFAULT_ASSISTANT_CONFIG: dict[str, Any] = {
    "transcriber": {
        "provider": "deepgram",
        "model": "nova-2",
        "language": "ja",
    },
    "model": {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "temperature": 0.3,
    },
    "voice": {
        "provider": "deepgram",
        "voiceId": "asteria",
    },
    "silenceTimeoutSeconds": 20,
    "responseDelaySeconds": 0.4,
    "llmRequestDelaySeconds": 0.1,
    "numWordsToInterruptAssistant": 2,
    "backgroundDenoisingEnabled": True,
    "modelOutputInMessagesEnabled": True,
    "recordingEnabled": True,
}


class PhoneCaller:
    """Make AI phone calls via Vapi with rate limiting.

    Args:
        api_key: Vapi API key. Defaults to reading from credentials file.
        phone_number_id: Vapi phone number ID. Defaults to credentials file.
        transfer_number: Phone number to transfer to when human needed.
        transfer_mode: "phone" (default), "meet" (Google Meet), or "webrtc".
        meet_attendee_email: Email to invite to Google Meet (for "meet" mode).
        credentials_path: Path to Vapi credentials JSON.
        rate_limit: Max calls per minute (default 5, 0=unlimited).
        daily_limit: Max calls per day (default 50, 0=unlimited).
        dry_run: If True, log but don't actually call.
        state_dir: Directory to persist rate limit state across restarts.
        assistant_overrides: Override default assistant config values.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        phone_number_id: Optional[str] = None,
        transfer_number: Optional[str] = None,
        transfer_mode: str = "phone",
        meet_attendee_email: str = "",
        credentials_path: str | Path | None = None,
        rate_limit: int = 5,
        daily_limit: int = 50,
        dry_run: bool = False,
        state_dir: str | Path | None = None,
        assistant_overrides: dict[str, Any] | None = None,
    ):
        self._credentials_path = (
            Path(credentials_path) if credentials_path
            else _DEFAULT_CONFIG_DIR / "vapi_credentials.json"
        )
        self._api_key = api_key
        self._phone_number_id = phone_number_id
        self._transfer_number = transfer_number
        self._transfer_mode = transfer_mode
        self._meet_attendee_email = meet_attendee_email
        self._dry_run = dry_run
        self._assistant_overrides = assistant_overrides or {}
        self._meet_creator = None
        self._active_meets: dict[str, Any] = {}  # call_id -> MeetRoom
        self._router = PhoneRouter(vapi_api_key=api_key)

        state_path = None
        if state_dir:
            state_path = Path(state_dir) / "phone_call_state.json"
        self._limiter = RateLimiter(
            rate_limit=rate_limit,
            daily_limit=daily_limit,
            state_path=state_path,
        )

    def _load_credentials(self) -> dict[str, str]:
        """Load Vapi credentials from config file or instance vars."""
        creds: dict[str, str] = {}

        if self._api_key:
            creds["api_key"] = self._api_key
        if self._phone_number_id:
            creds["phone_number_id"] = self._phone_number_id
        if self._transfer_number:
            creds["transfer_number"] = self._transfer_number

        if "api_key" in creds and "phone_number_id" in creds:
            return creds

        if not self._credentials_path.exists():
            raise FileNotFoundError(
                f"Vapi credentials not found at {self._credentials_path}. "
                "Create a JSON file with api_key, phone_number_id, and transfer_number."
            )

        data = json.loads(self._credentials_path.read_text())
        creds.setdefault("api_key", data["api_key"])
        creds.setdefault("phone_number_id", data["phone_number_id"])
        creds.setdefault("transfer_number", data.get("transfer_number", ""))
        creds.setdefault("transfer_mode", data.get("transfer_mode", "phone"))
        creds.setdefault(
            "meet_attendee_email", data.get("meet_attendee_email", ""),
        )
        return creds

    def _get_transfer_number(
        self, transfer_number: str | None, call_id: str = "",
    ) -> str:
        """Resolve transfer destination based on mode.

        For "phone" mode: returns the phone number directly.
        For "meet" mode: creates a Google Meet and returns its dial-in number.
        For "webrtc" mode: returns the user's phone as fallback
            (actual WebRTC pickup happens via webhook notification).
        """
        mode = self._transfer_mode
        xfer = transfer_number or self._transfer_number
        if not xfer:
            creds = self._load_credentials()
            xfer = creds.get("transfer_number", "")
            mode = creds.get("transfer_mode", mode)

        if mode == "meet":
            return self._create_meet_for_transfer(call_id, xfer)

        # "phone" and "webrtc" both use phone number as transfer target
        # webrtc mode sends a webhook notification for browser pickup
        return xfer

    def _create_meet_for_transfer(
        self, call_id: str, fallback_number: str,
    ) -> str:
        """Create a Google Meet room and return its dial-in number."""
        try:
            from .meet import MeetCreator

            if self._meet_creator is None:
                self._meet_creator = MeetCreator()

            email = self._meet_attendee_email
            if not email:
                creds = self._load_credentials()
                email = creds.get("meet_attendee_email", "")

            room = self._meet_creator.create_room(
                title=f"営業通話転送 [{call_id[:8]}]",
                attendee_email=email or None,
            )
            self._active_meets[call_id] = room

            if room.dial_in_number:
                logger.info(
                    "transfer.meet_created",
                    call_id=call_id,
                    meet_url=room.meet_url,
                    dial_in=room.dial_in_number,
                )
                return room.dial_in_number

            logger.warning(
                "transfer.meet_no_dialin",
                call_id=call_id,
                meet_url=room.meet_url,
            )
            return fallback_number

        except Exception as e:
            logger.error("transfer.meet_failed", error=str(e), call_id=call_id)
            return fallback_number

    def get_active_meet(self, call_id: str) -> dict[str, str] | None:
        """Get active Meet room info for a call (if any)."""
        room = self._active_meets.get(call_id)
        if room is None:
            return None
        return room.to_dict()

    def cleanup_meet(self, call_id: str) -> None:
        """Delete the Meet room after call ends."""
        room = self._active_meets.pop(call_id, None)
        if room is None:
            return
        try:
            if self._meet_creator:
                self._meet_creator.delete_room(room.event_id)
        except Exception as e:
            logger.warning("meet.cleanup_failed", error=str(e), call_id=call_id)

    def _build_assistant_config(
        self,
        system_prompt: str,
        transfer_number: str | None = None,
        call_id: str = "",
    ) -> dict[str, Any]:
        """Build Vapi assistant config with transfer tool."""
        config = {**_DEFAULT_ASSISTANT_CONFIG}
        config.update(self._assistant_overrides)

        config["model"] = {**config["model"], "messages": [
            {"role": "system", "content": system_prompt},
        ]}

        xfer = self._get_transfer_number(transfer_number, call_id)

        if xfer:
            config["model"]["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": "transferCall",
                        "description": (
                            "Transfer the call to a human representative. "
                            "Use when the customer shows serious interest, "
                            "asks complex questions, or requests to speak with a person."
                        ),
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            ]
            config["forwardingPhoneNumber"] = xfer

        return config

    def _build_call_payload(
        self,
        to: str,
        system_prompt: str,
        transfer_number: str | None,
    ) -> dict[str, Any]:
        creds = self._load_credentials()
        assistant_config = self._build_assistant_config(
            system_prompt=system_prompt,
            transfer_number=transfer_number,
        )

        # Use country-routed phone number if available
        phone_id = self._router.get_vapi_phone_id(to)
        if not phone_id:
            phone_id = creds["phone_number_id"]

        return {
            "phoneNumberId": phone_id,
            "customer": {"number": to},
            "assistant": assistant_config,
        }

    def _resolve_prompt(
        self,
        prompt: str | None,
        system_prompt: str | None,
        company_name: str,
        product_name: str,
        caller_name: str,
    ) -> str:
        if system_prompt is not None:
            return system_prompt
        return build_sales_prompt(
            purpose=prompt or "",
            company_name=company_name,
            product_name=product_name,
            caller_name=caller_name,
        )

    async def call(
        self,
        to: str,
        prompt: str | None = None,
        system_prompt: str | None = None,
        company_name: str = "",
        product_name: str = "",
        caller_name: str = "",
        transfer_number: str | None = None,
    ) -> dict[str, Any]:
        """Make an AI phone call asynchronously.

        Args:
            to: Phone number to call (E.164 format).
            prompt: Short description of call purpose (used to build system prompt).
            system_prompt: Full system prompt (overrides prompt/company/product).
            company_name: Company name for auto-generated prompt.
            product_name: Product/service name for auto-generated prompt.
            caller_name: Caller's name for auto-generated prompt.
            transfer_number: Override default transfer number for this call.

        Returns:
            Dict with call_id and status.
        """
        self._limiter.check()

        resolved = self._resolve_prompt(
            prompt, system_prompt, company_name, product_name, caller_name,
        )

        if self._dry_run:
            logger.info(
                "phone.dry_run",
                to=to,
                prompt=prompt[:80] if prompt else "(custom system_prompt)",
                remaining_today=self._limiter.remaining_today,
            )
            self._limiter.record_call()
            return {"call_id": "dry_run", "dry_run": True}

        creds = self._load_credentials()
        payload = self._build_call_payload(to, resolved, transfer_number)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_VAPI_BASE_URL}/call/phone",
                headers={
                    "Authorization": f"Bearer {creds['api_key']}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        self._limiter.record_call()
        logger.info(
            "phone.called",
            to=to,
            call_id=data.get("id"),
            status=data.get("status"),
            remaining_today=self._limiter.remaining_today,
        )
        return {"call_id": data.get("id"), "status": data.get("status")}

    def call_sync(
        self,
        to: str,
        prompt: str | None = None,
        system_prompt: str | None = None,
        company_name: str = "",
        product_name: str = "",
        caller_name: str = "",
        transfer_number: str | None = None,
    ) -> dict[str, Any]:
        """Make an AI phone call synchronously."""
        self._limiter.check()

        resolved = self._resolve_prompt(
            prompt, system_prompt, company_name, product_name, caller_name,
        )

        if self._dry_run:
            logger.info(
                "phone.dry_run",
                to=to,
                prompt=prompt[:80] if prompt else "(custom system_prompt)",
                remaining_today=self._limiter.remaining_today,
            )
            self._limiter.record_call()
            return {"call_id": "dry_run", "dry_run": True}

        creds = self._load_credentials()
        payload = self._build_call_payload(to, resolved, transfer_number)

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{_VAPI_BASE_URL}/call/phone",
                headers={
                    "Authorization": f"Bearer {creds['api_key']}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        self._limiter.record_call()
        logger.info(
            "phone.called",
            to=to,
            call_id=data.get("id"),
            status=data.get("status"),
            remaining_today=self._limiter.remaining_today,
        )
        return {"call_id": data.get("id"), "status": data.get("status")}

    async def get_call(self, call_id: str) -> dict[str, Any]:
        """Get call details by ID."""
        creds = self._load_credentials()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_VAPI_BASE_URL}/call/{call_id}",
                headers={"Authorization": f"Bearer {creds['api_key']}"},
            )
            resp.raise_for_status()
            return resp.json()

    async def list_calls(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent calls."""
        creds = self._load_credentials()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_VAPI_BASE_URL}/call",
                headers={"Authorization": f"Bearer {creds['api_key']}"},
                params={"limit": limit},
            )
            resp.raise_for_status()
            return resp.json()

    @property
    def remaining_today(self) -> int:
        return self._limiter.remaining_today

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @dry_run.setter
    def dry_run(self, value: bool) -> None:
        self._dry_run = value
