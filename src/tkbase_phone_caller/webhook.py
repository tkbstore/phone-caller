"""Webhook endpoint for call events with dashboard and REST API.

Run standalone:
    uvicorn tkbase_phone_caller.webhook:app --host 0.0.0.0 --port 8000

Provides:
    - POST /webhook/vapi — Vapi/LiveKit call event receiver
    - GET / — Dashboard UI
    - GET /api/calls — Call log listing
    - GET /api/calls/{call_id} — Single call detail
    - GET /api/context — Global sales context
    - PUT /api/context — Update global sales context
    - GET /api/stats — Cost/duration aggregates
    - GET /api/phone-numbers — Registered phone numbers
    - WS /ws — Real-time event stream
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

logger = structlog.get_logger()

app = FastAPI(title="phone-caller-webhook", version="0.2.0")

_DEFAULT_LOG_DIR = Path.home() / ".config" / "tkbase" / "call_logs"

# Connected WebRTC/browser clients waiting for transfer notifications
_ws_clients: set[WebSocket] = set()

# Notion logger (initialized lazily)
_notion_logger = None

# Drive storage for recordings (initialized lazily)
_drive_storage = None


def _get_notion_logger():
    global _notion_logger
    if _notion_logger is not None:
        return _notion_logger
    try:
        from .notion_logger import NotionLogger
        _notion_logger = NotionLogger()
        return _notion_logger
    except (FileNotFoundError, ImportError):
        return None


def _get_drive_storage():
    global _drive_storage
    if _drive_storage is not None:
        return _drive_storage
    try:
        from .drive_storage import DriveStorage
        _drive_storage = DriveStorage()
        return _drive_storage
    except (FileNotFoundError, ImportError):
        return None


def _ensure_log_dir(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _save_call_log(call_data: dict[str, Any], log_dir: Path | None = None) -> Path:
    """Save call log to a JSON file."""
    target = _ensure_log_dir(log_dir or _DEFAULT_LOG_DIR)
    call_id = call_data.get("call", {}).get("id", f"unknown_{int(time.time())}")
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{call_id}.json"
    filepath = target / filename
    filepath.write_text(json.dumps(call_data, ensure_ascii=False, indent=2))
    return filepath


async def _notify_ws_clients(event: dict[str, Any]) -> None:
    """Send event to all connected WebSocket clients."""
    if not _ws_clients:
        return
    msg = json.dumps(event, ensure_ascii=False)
    disconnected = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            disconnected.add(ws)
    _ws_clients -= disconnected


@app.post("/webhook/vapi")
async def vapi_webhook(request: Request) -> JSONResponse:
    """Receive Vapi webhook events.

    Event types:
    - end-of-call-report: transcript, duration, cost, summary
    - status-update: call status changes
    - transcript: real-time transcript
    - function-call: AI triggered a function (e.g. transferCall)
    """
    payload = await request.json()
    message_type = payload.get("message", {}).get("type", "unknown")

    logger.info(
        "webhook.received",
        type=message_type,
        call_id=payload.get("message", {}).get("call", {}).get("id"),
    )

    if message_type == "end-of-call-report":
        return await _handle_end_of_call(payload["message"])

    if message_type == "status-update":
        return await _handle_status_update(payload["message"])

    if message_type == "function-call":
        return await _handle_function_call(payload["message"])

    if message_type == "transcript":
        message = payload["message"]
        await _notify_ws_clients({
            "type": "transcript",
            "call_id": message.get("call", {}).get("id"),
            "role": message.get("role"),
            "text": message.get("transcript", ""),
        })
        return JSONResponse({"status": "ok"})

    return JSONResponse({"status": "ok"})


async def _handle_end_of_call(message: dict[str, Any]) -> JSONResponse:
    """Handle end-of-call report: save locally + Drive recording + Notion."""
    filepath = _save_call_log(message)

    ended_reason = message.get("endedReason", "unknown")
    duration = message.get("durationSeconds", 0)
    cost = message.get("cost", 0)
    summary = message.get("summary", "")
    call_id = message.get("call", {}).get("id", "")
    recording_url = message.get("recordingUrl", "")
    customer_number = message.get("call", {}).get("customer", {}).get("number", "")

    logger.info(
        "call.completed",
        call_id=call_id,
        ended_reason=ended_reason,
        duration_seconds=duration,
        cost=cost,
        summary=summary[:200] if summary else "",
        log_file=str(filepath),
        has_recording=bool(recording_url),
    )

    # Upload recording to Google Drive
    drive = _get_drive_storage()
    drive_link = ""
    if drive and recording_url:
        try:
            result = await drive.upload_recording(
                recording_url=recording_url,
                call_id=call_id,
                customer_number=customer_number,
                duration_seconds=duration,
                ended_reason=ended_reason,
            )
            drive_link = result.get("web_view_link", "")
            logger.info(
                "drive.recording_saved",
                call_id=call_id,
                file_id=result.get("file_id"),
                link=drive_link,
            )
        except Exception as e:
            logger.error("drive.upload_failed", error=str(e), call_id=call_id)

    # Log to Notion (include Drive recording link)
    notion = _get_notion_logger()
    notion_page_id = None
    if notion:
        try:
            result = await notion.log_call(
                message, recording_url=drive_link,
            )
            notion_page_id = result.get("id")
            logger.info("notion.logged", page_id=notion_page_id, call_id=call_id)
        except Exception as e:
            logger.error("notion.failed", error=str(e), call_id=call_id)

    # Notify WebSocket clients
    await _notify_ws_clients({
        "type": "call_ended",
        "call_id": call_id,
        "ended_reason": ended_reason,
        "duration": duration,
        "summary": summary,
        "recording_link": drive_link,
    })

    return JSONResponse({
        "status": "logged",
        "file": str(filepath),
        "notion_page_id": notion_page_id,
        "recording_link": drive_link,
    })


async def _handle_status_update(message: dict[str, Any]) -> JSONResponse:
    """Handle call status change."""
    call_id = message.get("call", {}).get("id", "")
    status = message.get("status", "unknown")

    logger.info("call.status", call_id=call_id, status=status)

    await _notify_ws_clients({
        "type": "status",
        "call_id": call_id,
        "status": status,
    })

    return JSONResponse({"status": "ok"})


async def _handle_function_call(message: dict[str, Any]) -> JSONResponse:
    """Handle function calls from the AI (e.g. transferCall).

    When transfer_mode is "webrtc", this notifies connected browsers
    so the user can pick up in the browser instead of phone.
    """
    fn_name = message.get("functionCall", {}).get("name", "")
    call_id = message.get("call", {}).get("id", "")

    if fn_name == "transferCall":
        logger.info("call.transfer_requested", call_id=call_id)
        await _notify_ws_clients({
            "type": "transfer_requested",
            "call_id": call_id,
            "message": "顧客が担当者との会話を希望しています",
        })

    return JSONResponse({"status": "ok"})


# --- WebSocket for real-time browser notifications ---

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket for real-time call notifications.

    Browser clients connect here to receive:
    - transfer_requested: AI wants to transfer the call
    - transcript: real-time conversation text
    - call_ended: call completed with summary
    - status: call status updates
    """
    await websocket.accept()
    _ws_clients.add(websocket)
    logger.info("ws.connected", total_clients=len(_ws_clients))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _ws_clients.discard(websocket)
        logger.info("ws.disconnected", total_clients=len(_ws_clients))


# --- Dashboard & REST API ---

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> FileResponse:
    """Serve the dashboard SPA."""
    return FileResponse(_STATIC_DIR / "dashboard.html")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "status": "healthy",
        "ws_clients": len(_ws_clients),
    })


@app.get("/api/calls")
async def api_list_calls() -> JSONResponse:
    """List all call logs, most recent first."""
    log_dir = _DEFAULT_LOG_DIR
    if not log_dir.exists():
        return JSONResponse([])

    calls: list[dict[str, Any]] = []
    for f in sorted(log_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            call_obj = data.get("call", data)
            calls.append({
                "file": f.name,
                "call_id": call_obj.get("id", f.stem),
                "customer_number": (
                    call_obj.get("customer", {}).get("number", "")
                    or data.get("customer_number", "")
                ),
                "ended_reason": data.get("endedReason", data.get("ended_reason", "")),
                "duration": data.get("durationSeconds", data.get("duration", 0)),
                "cost": data.get("cost", 0),
                "summary": data.get("summary", ""),
                "timestamp": f.name[:15],  # YYYYMMDD_HHMMSS
            })
        except (json.JSONDecodeError, OSError):
            continue

    return JSONResponse(calls)


@app.get("/api/calls/{call_id}")
async def api_get_call(call_id: str) -> JSONResponse:
    """Get a single call's full data including transcript."""
    log_dir = _DEFAULT_LOG_DIR
    if not log_dir.exists():
        return JSONResponse({"error": "not found"}, status_code=404)

    for f in log_dir.glob("*.json"):
        if call_id in f.name:
            data = json.loads(f.read_text(encoding="utf-8"))
            return JSONResponse(data)

    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/api/context")
async def api_get_context() -> JSONResponse:
    """Get global sales context."""
    from .prompts import load_context
    return JSONResponse(load_context())


@app.put("/api/context")
async def api_update_context(request: Request) -> JSONResponse:
    """Update global sales context."""
    from .prompts import save_context
    body = await request.json()
    save_context(body)
    return JSONResponse({"status": "saved"})


@app.get("/api/stats")
async def api_stats() -> JSONResponse:
    """Aggregate cost and call stats by date."""
    log_dir = _DEFAULT_LOG_DIR
    if not log_dir.exists():
        return JSONResponse({"daily": [], "totals": {"calls": 0, "cost": 0, "duration": 0}})

    daily: dict[str, dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "cost": 0.0, "duration": 0.0}
    )
    total_calls = 0
    total_cost = 0.0
    total_duration = 0.0

    for f in sorted(log_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            date_str = f.name[:8]  # YYYYMMDD
            date_key = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
            cost = float(data.get("cost", 0))
            dur = float(data.get("durationSeconds", data.get("duration", 0)))
            daily[date_key]["calls"] += 1
            daily[date_key]["cost"] += cost
            daily[date_key]["duration"] += dur
            total_calls += 1
            total_cost += cost
            total_duration += dur
        except (json.JSONDecodeError, OSError, ValueError):
            continue

    return JSONResponse({
        "daily": [
            {"date": k, **v} for k, v in sorted(daily.items())
        ],
        "totals": {
            "calls": total_calls,
            "cost": round(total_cost, 4),
            "duration": round(total_duration, 1),
        },
    })


@app.get("/api/phone-numbers")
async def api_phone_numbers() -> JSONResponse:
    """List registered phone numbers."""
    try:
        from .phone_router import PhoneRouter
        router = PhoneRouter()
        numbers = router.list_numbers()
        return JSONResponse({
            prefix: entry.to_dict()
            for prefix, entry in numbers.items()
        })
    except Exception:
        return JSONResponse({})
