"""Webhook endpoint for Vapi call events with Notion logging.

Run standalone:
    uvicorn tkbase_phone_caller.webhook:app --host 0.0.0.0 --port 8000

Vapi sends POST requests to your server URL with call events including
end-of-call reports with transcript, duration, and outcome.

Logs are saved to:
    1. Local JSON files (~/.config/tkbase/call_logs/)
    2. Notion database (if configured)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

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


# --- Browser dashboard for WebRTC mode ---

_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Phone Caller Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, sans-serif; background: #0a0a0a; color: #e0e0e0; padding: 2rem; }
h1 { font-size: 1.5rem; margin-bottom: 1.5rem; color: #fff; }
.status { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1rem; }
.dot { width: 10px; height: 10px; border-radius: 50%; background: #444; }
.dot.connected { background: #4ade80; }
.dot.alert { background: #f97316; animation: pulse 1s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
#log { background: #111; border: 1px solid #333; border-radius: 8px; padding: 1rem;
       max-height: 70vh; overflow-y: auto; font-family: monospace; font-size: 0.85rem; line-height: 1.6; }
.entry { padding: 0.25rem 0; border-bottom: 1px solid #1a1a1a; }
.entry.transfer { color: #f97316; font-weight: bold; }
.entry.ended { color: #4ade80; }
.entry.transcript { color: #888; }
.time { color: #555; margin-right: 0.5rem; }
.alert-banner { display: none; background: #f97316; color: #000; padding: 1rem;
                border-radius: 8px; margin-bottom: 1rem; font-weight: bold; font-size: 1.1rem; }
.alert-banner.show { display: block; }
</style>
</head>
<body>
<h1>Phone Caller Dashboard</h1>
<div class="status">
  <div class="dot" id="statusDot"></div>
  <span id="statusText">Connecting...</span>
</div>
<div class="alert-banner" id="alertBanner"></div>
<div id="log"></div>
<script>
const log = document.getElementById('log');
const dot = document.getElementById('statusDot');
const statusText = document.getElementById('statusText');
const alertBanner = document.getElementById('alertBanner');
let ws;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => {
    dot.className = 'dot connected';
    statusText.textContent = 'Connected — waiting for calls';
  };
  ws.onclose = () => {
    dot.className = 'dot';
    statusText.textContent = 'Disconnected — reconnecting...';
    setTimeout(connect, 3000);
  };
  ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    const time = new Date().toLocaleTimeString('ja-JP');
    let cls = 'entry';
    let text = '';
    if (data.type === 'transfer_requested') {
      cls = 'entry transfer';
      text = `TRANSFER: ${data.message} (${data.call_id})`;
      dot.className = 'dot alert';
      alertBanner.textContent = data.message;
      alertBanner.className = 'alert-banner show';
      if (Notification.permission === 'granted') {
        new Notification('Transfer Request', { body: data.message });
      }
    } else if (data.type === 'call_ended') {
      cls = 'entry ended';
      text = `ENDED: ${data.ended_reason} (${data.duration}s) — ${data.summary || 'no summary'}`;
      dot.className = 'dot connected';
      alertBanner.className = 'alert-banner';
    } else if (data.type === 'transcript') {
      cls = 'entry transcript';
      text = `[${data.role}] ${data.text}`;
    } else if (data.type === 'status') {
      text = `STATUS: ${data.status} (${data.call_id})`;
    }
    if (text) {
      const el = document.createElement('div');
      el.className = cls;
      el.innerHTML = `<span class="time">${time}</span>${text}`;
      log.appendChild(el);
      log.scrollTop = log.scrollHeight;
    }
  };
}

if ('Notification' in window && Notification.permission === 'default') {
  Notification.requestPermission();
}
connect();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    """Serve the real-time call monitoring dashboard."""
    return _DASHBOARD_HTML


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "status": "healthy",
        "ws_clients": len(_ws_clients),
    })
