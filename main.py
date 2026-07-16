"""
Snappic Webhook Dashboard
--------------------------
Receives Snappic webhooks (session / share / survey / competition_win),
verifies the HMAC-SHA256 signature, stores events in SQLite, and pushes
live updates to a web dashboard over WebSocket.

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000

Then tunnel port 8000 with ngrok and point Snappic's webhook URL at
<ngrok-url>/webhook/snappic
"""

import csv
import hashlib
import hmac
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "snappic_dashboard.db"

# Secret key from Snappic's webhook settings. If unset, signature
# verification is skipped (useful for local testing with "Send Test").
WEBHOOK_SECRET = os.environ.get("SNAPPIC_WEBHOOK_SECRET", "")

app = FastAPI(title="Snappic Webhook Dashboard")

VALID_TYPES = {"session", "share", "survey", "competition_win"}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            type TEXT NOT NULL,
            event_id INTEGER,
            session_id TEXT,
            session_type TEXT,
            device_name TEXT,
            direct_url TEXT,
            site_url TEXT,
            summary TEXT,
            raw_json TEXT NOT NULL
        )
        """
    )
    # Migration-safe: add device_name if this DB predates it.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    if "device_name" not in existing_cols:
        conn.execute("ALTER TABLE events ADD COLUMN device_name TEXT")

    # Session-level lookup so share/survey/competition_win events (which
    # don't carry device info themselves) can still be filtered by device
    # via the session_id they share with their originating session event.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            device_name TEXT,
            device_id INTEGER,
            session_type TEXT,
            event_id INTEGER,
            first_seen_at TEXT
        )
        """
    )

    # Friendly, support-agent-assigned names for Snappic's numeric event_id
    # (Snappic itself only sends a numeric id, never a name).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_aliases (
            event_id INTEGER PRIMARY KEY,
            alias TEXT NOT NULL,
            updated_at TEXT
        )
        """
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_event_id ON events(event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_device_name ON events(device_name)")
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def verify_signature(body: bytes, signature: Optional[str]) -> bool:
    if not WEBHOOK_SECRET:
        # No secret configured (dev mode) — accept everything.
        return True
    if not signature:
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    # Some providers prefix the header, e.g. "sha256=...". Handle both.
    candidate = signature.split("=", 1)[-1] if "=" in signature else signature
    return hmac.compare_digest(expected, candidate)


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Payload summarizing
# ---------------------------------------------------------------------------

def summarize(payload: dict) -> tuple[str, str, str, str, str]:
    """Returns (session_id, session_type, direct_url, site_url, summary)."""
    session = payload.get("session") or {}
    session_id = session.get("id", "")
    session_type = session.get("type", "")
    direct_url = session.get("direct_url", "")
    site_url = session.get("site_url", "")

    t = payload.get("type")
    if t == "session":
        device = session.get("device") or {}
        summary = f"New {session_type or 'media'} captured on {device.get('name', 'unknown device')}"
    elif t == "share":
        share = payload.get("share") or {}
        summary = f"Shared via {share.get('type', '?')} to {share.get('value', '?')}"
    elif t == "survey":
        fields = []
        for section in (payload.get("survey") or {}).get("data_capture", {}).get("sections", []):
            for f in section.get("fields", []):
                fields.append(f"{f.get('field_title')}: {f.get('value')}")
        summary = "; ".join(fields) if fields else "Survey completed"
    elif t == "competition_win":
        prize = payload.get("prize") or {}
        winner = payload.get("winner") or {}
        summary = f"Won \u2018{prize.get('name', '?')}\u2019 \u2014 {winner.get('type', '?')}: {winner.get('value', '?')}"
    else:
        summary = ""
    return session_id, session_type, direct_url, site_url, summary


def resolve_device(conn: sqlite3.Connection, payload: dict, session_id: str) -> Optional[str]:
    """
    Returns the device name for this event. Session webhooks carry device
    info directly; other webhook types don't, so we look it up from the
    most recent session webhook seen for the same session_id.
    """
    session = payload.get("session") or {}
    device = session.get("device") or {}
    device_name = device.get("name")
    if device_name:
        return device_name
    if not session_id:
        return None
    row = conn.execute("SELECT device_name FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return row["device_name"] if row else None


def upsert_session(conn: sqlite3.Connection, payload: dict, session_id: str, session_type: str,
                    event_id: Optional[int], received_at: str) -> None:
    session = payload.get("session") or {}
    device = session.get("device") or {}
    if payload.get("type") != "session" or not session_id:
        return
    conn.execute(
        """INSERT INTO sessions (session_id, device_name, device_id, session_type, event_id, first_seen_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
             device_name=excluded.device_name,
             device_id=excluded.device_id,
             session_type=excluded.session_type,
             event_id=excluded.event_id""",
        (session_id, device.get("name"), device.get("id"), session_type, event_id, received_at),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/webhook/snappic")
async def receive_webhook(request: Request, x_signature: Optional[str] = Header(default=None)):
    body = await request.body()

    if not verify_signature(body, x_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("type", "unknown")
    event_id = payload.get("event_id")
    session_id, session_type, direct_url, site_url, summary = summarize(payload)
    received_at = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    upsert_session(conn, payload, session_id, session_type, event_id, received_at)
    device_name = resolve_device(conn, payload, session_id)

    cur = conn.execute(
        """INSERT INTO events
           (received_at, type, event_id, session_id, session_type, device_name, direct_url, site_url, summary, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (received_at, event_type, event_id, session_id, session_type, device_name, direct_url, site_url, summary, json.dumps(payload)),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()

    event_record = {
        "id": row_id,
        "received_at": received_at,
        "type": event_type,
        "event_id": event_id,
        "session_id": session_id,
        "session_type": session_type,
        "device_name": device_name,
        "direct_url": direct_url,
        "site_url": site_url,
        "summary": summary,
        "raw_json": json.dumps(payload),
    }
    await manager.broadcast({"kind": "new_event", "event": event_record})

    # Must return 2xx or Snappic will retry (3x, 10s apart).
    return JSONResponse({"status": "ok"}, status_code=200)


@app.get("/api/events")
def list_events(
    limit: int = 50,
    before_id: Optional[int] = None,
    type: Optional[str] = None,
    event_id: Optional[int] = None,
    device_name: Optional[str] = None,
):
    conn = get_conn()
    query = "SELECT * FROM events WHERE 1=1"
    params: list = []
    if type and type in VALID_TYPES:
        query += " AND type = ?"
        params.append(type)
    if event_id is not None:
        query += " AND event_id = ?"
        params.append(event_id)
    if device_name:
        query += " AND device_name = ?"
        params.append(device_name)
    if before_id:
        query += " AND id < ?"
        params.append(before_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/filters")
def get_filters():
    """Distinct event_ids (with any assigned alias) and device_names, for populating filter dropdowns."""
    conn = get_conn()
    event_rows = conn.execute(
        "SELECT event_id, COUNT(*) c FROM events WHERE event_id IS NOT NULL GROUP BY event_id ORDER BY event_id"
    ).fetchall()
    aliases = {r["event_id"]: r["alias"] for r in conn.execute("SELECT event_id, alias FROM event_aliases")}
    device_rows = conn.execute(
        "SELECT device_name, COUNT(*) c FROM events WHERE device_name IS NOT NULL AND device_name != '' "
        "GROUP BY device_name ORDER BY device_name"
    ).fetchall()
    conn.close()
    return {
        "events": [
            {"event_id": r["event_id"], "alias": aliases.get(r["event_id"]), "count": r["c"]}
            for r in event_rows
        ],
        "devices": [{"device_name": r["device_name"], "count": r["c"]} for r in device_rows],
    }


@app.post("/api/event-alias")
async def set_event_alias(request: Request):
    """Assign (or clear) a friendly name for a Snappic event_id — Snappic itself only sends a number."""
    body = await request.json()
    event_id = body.get("event_id")
    alias = (body.get("alias") or "").strip()
    if event_id is None:
        raise HTTPException(status_code=400, detail="event_id is required")

    conn = get_conn()
    if alias:
        conn.execute(
            """INSERT INTO event_aliases (event_id, alias, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(event_id) DO UPDATE SET alias=excluded.alias, updated_at=excluded.updated_at""",
            (event_id, alias, datetime.now(timezone.utc).isoformat()),
        )
    else:
        conn.execute("DELETE FROM event_aliases WHERE event_id = ?", (event_id,))
    conn.commit()
    conn.close()
    return {"status": "ok", "event_id": event_id, "alias": alias or None}


@app.get("/api/export/surveys.csv")
def export_surveys_csv(event_id: Optional[int] = None, device_name: Optional[str] = None):
    """Tidy/long-format CSV: one row per survey field, so it holds up regardless of which
    fields a given event's survey happens to use."""
    conn = get_conn()
    query = "SELECT * FROM events WHERE type = 'survey'"
    params: list = []
    if event_id is not None:
        query += " AND event_id = ?"
        params.append(event_id)
    if device_name:
        query += " AND device_name = ?"
        params.append(device_name)
    query += " ORDER BY id"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "received_at", "event_id", "device_name", "session_id", "site_url",
        "field_title", "field_type", "value",
    ])
    for r in rows:
        payload = json.loads(r["raw_json"])
        sections = (payload.get("survey") or {}).get("data_capture", {}).get("sections", [])
        fields = [f for section in sections for f in section.get("fields", [])]
        base = [r["received_at"], r["event_id"], r["device_name"] or "", r["session_id"], r["site_url"] or ""]
        if not fields:
            writer.writerow(base + ["", "", ""])
        else:
            for f in fields:
                writer.writerow(base + [f.get("field_title", ""), f.get("field_type", ""), f.get("value", "")])

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=survey_responses.csv"},
    )


@app.get("/api/stats")
def get_stats():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    by_type = {r["type"]: r["c"] for r in conn.execute("SELECT type, COUNT(*) c FROM events GROUP BY type")}
    by_session_type = {
        r["session_type"]: r["c"]
        for r in conn.execute(
            "SELECT session_type, COUNT(*) c FROM events WHERE type='session' GROUP BY session_type"
        )
    }

    share_rows = conn.execute("SELECT raw_json FROM events WHERE type='share'").fetchall()
    share_channels: dict = {}
    for r in share_rows:
        ch = (json.loads(r["raw_json"]).get("share") or {}).get("type", "unknown")
        share_channels[ch] = share_channels.get(ch, 0) + 1

    survey_rows = conn.execute("SELECT raw_json FROM events WHERE type='survey'").fetchall()
    ratings = []
    for r in survey_rows:
        payload = json.loads(r["raw_json"])
        for section in (payload.get("survey") or {}).get("data_capture", {}).get("sections", []):
            for f in section.get("fields", []):
                if f.get("field_type") == "rating":
                    try:
                        ratings.append(float(f.get("value")))
                    except (TypeError, ValueError):
                        pass
    avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None

    prize_wins = conn.execute("SELECT COUNT(*) c FROM events WHERE type='competition_win'").fetchone()["c"]
    conn.close()

    return {
        "total": total,
        "by_type": by_type,
        "by_session_type": by_session_type,
        "share_channels": share_channels,
        "avg_survey_rating": avg_rating,
        "survey_responses": len(survey_rows),
        "prize_wins": prize_wins,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep-alive; incoming messages are ignored
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    html_path = BASE_DIR / "static" / "dashboard.html"
    return HTMLResponse(html_path.read_text())


@app.get("/health")
def health():
    return {"status": "ok"}