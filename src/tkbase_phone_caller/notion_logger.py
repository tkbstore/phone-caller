"""Notion integration for call logging.

Saves call data (transcript, duration, outcome, summary) to a Notion database.

Credentials: ~/.config/tkbase/notion_credentials.json
Expected format:
    {
        "api_key": "ntn_xxxxxxxxxxxx",
        "database_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    }

Notion database expected properties:
    - 通話日時 (Date)
    - 顧客番号 (Phone / Rich text)
    - ステータス (Select): 完了, 転送済, 不在, 興味なし
    - 所要時間 (Number, seconds)
    - コスト (Number, USD)
    - サマリー (Rich text)
    - トランスクリプト (Rich text)
    - 終了理由 (Select)
    - Meet URL (URL) — optional, for transferred calls
    - 通話ID (Rich text)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger()

_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "tkbase"
_NOTION_API_URL = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"

# Map Vapi endedReason to Japanese status
_STATUS_MAP: dict[str, str] = {
    "assistant-ended-call": "完了",
    "customer-ended-call": "完了",
    "assistant-forwarded-call": "転送済",
    "customer-did-not-answer": "不在",
    "voicemail": "不在",
    "silence-timed-out": "興味なし",
    "max-duration-reached": "完了",
}


class NotionLogger:
    """Log call data to a Notion database.

    Args:
        api_key: Notion API key (internal integration token).
        database_id: Notion database ID.
        credentials_path: Path to credentials JSON.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        database_id: Optional[str] = None,
        credentials_path: str | Path | None = None,
    ):
        self._credentials_path = (
            Path(credentials_path) if credentials_path
            else _DEFAULT_CONFIG_DIR / "notion_credentials.json"
        )
        self._api_key = api_key
        self._database_id = database_id

    def _load_credentials(self) -> dict[str, str]:
        creds: dict[str, str] = {}
        if self._api_key:
            creds["api_key"] = self._api_key
        if self._database_id:
            creds["database_id"] = self._database_id

        if "api_key" in creds and "database_id" in creds:
            return creds

        if not self._credentials_path.exists():
            raise FileNotFoundError(
                f"Notion credentials not found at {self._credentials_path}. "
                "Create a JSON file with api_key and database_id."
            )

        data = json.loads(self._credentials_path.read_text())
        creds.setdefault("api_key", data["api_key"])
        creds.setdefault("database_id", data["database_id"])
        return creds

    def _headers(self) -> dict[str, str]:
        creds = self._load_credentials()
        return {
            "Authorization": f"Bearer {creds['api_key']}",
            "Content-Type": "application/json",
            "Notion-Version": _NOTION_VERSION,
        }

    def _truncate_rich_text(self, text: str, limit: int = 2000) -> str:
        """Notion rich_text blocks have a 2000 char limit."""
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def _build_page_properties(
        self,
        call_data: dict[str, Any],
        meet_url: str = "",
        recording_url: str = "",
    ) -> dict[str, Any]:
        """Build Notion page properties from call data."""
        call = call_data.get("call", {})
        customer = call.get("customer", {})
        ended_reason = call_data.get("endedReason", "unknown")
        status = _STATUS_MAP.get(ended_reason, "完了")
        duration = call_data.get("durationSeconds", 0)
        cost = call_data.get("cost", 0)
        summary = call_data.get("summary", "")
        transcript = call_data.get("transcript", "")
        call_id = call.get("id", "")
        customer_number = customer.get("number", "")

        props: dict[str, Any] = {
            "通話日時": {
                "date": {
                    "start": datetime.now(timezone.utc).isoformat(),
                },
            },
            "顧客番号": {
                "rich_text": [{"text": {"content": customer_number}}],
            },
            "ステータス": {
                "select": {"name": status},
            },
            "所要時間": {"number": duration},
            "コスト": {"number": round(cost, 4) if cost else 0},
            "サマリー": {
                "rich_text": [
                    {"text": {"content": self._truncate_rich_text(summary)}}
                ],
            },
            "トランスクリプト": {
                "rich_text": [
                    {"text": {"content": self._truncate_rich_text(transcript)}}
                ],
            },
            "終了理由": {
                "select": {"name": ended_reason},
            },
            "通話ID": {
                "rich_text": [{"text": {"content": call_id}}],
            },
        }

        if meet_url:
            props["Meet URL"] = {"url": meet_url}

        if recording_url:
            props["録音"] = {"url": recording_url}

        return props

    async def log_call(
        self,
        call_data: dict[str, Any],
        meet_url: str = "",
        recording_url: str = "",
    ) -> dict[str, Any]:
        """Log a call to Notion database.

        Args:
            call_data: Vapi end-of-call-report message payload.
            meet_url: Google Meet URL if call was transferred.

        Returns:
            Notion page creation response.
        """
        creds = self._load_credentials()
        properties = self._build_page_properties(
            call_data, meet_url, recording_url,
        )

        body: dict[str, Any] = {
            "parent": {"database_id": creds["database_id"]},
            "properties": properties,
        }

        transcript = call_data.get("transcript", "")
        if len(transcript) > 2000:
            body["children"] = self._build_transcript_blocks(transcript)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_NOTION_API_URL}/pages",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            result = resp.json()

        logger.info(
            "notion.logged",
            page_id=result.get("id"),
            call_id=call_data.get("call", {}).get("id"),
        )
        return result

    def log_call_sync(
        self,
        call_data: dict[str, Any],
        meet_url: str = "",
        recording_url: str = "",
    ) -> dict[str, Any]:
        """Log a call to Notion database (sync version)."""
        creds = self._load_credentials()
        properties = self._build_page_properties(
            call_data, meet_url, recording_url,
        )

        body: dict[str, Any] = {
            "parent": {"database_id": creds["database_id"]},
            "properties": properties,
        }

        transcript = call_data.get("transcript", "")
        if len(transcript) > 2000:
            body["children"] = self._build_transcript_blocks(transcript)

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{_NOTION_API_URL}/pages",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            result = resp.json()

        logger.info(
            "notion.logged",
            page_id=result.get("id"),
            call_id=call_data.get("call", {}).get("id"),
        )
        return result

    def _build_transcript_blocks(self, transcript: str) -> list[dict[str, Any]]:
        """Split long transcript into Notion paragraph blocks (2000 char limit each)."""
        blocks: list[dict[str, Any]] = [
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"text": {"content": "Full Transcript"}}],
                },
            },
        ]
        chunk_size = 2000
        for i in range(0, len(transcript), chunk_size):
            chunk = transcript[i : i + chunk_size]
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"text": {"content": chunk}}],
                },
            })
        return blocks
