from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3
import uuid
import json
import os
from datetime import datetime
from contextlib import asynccontextmanager

# ------------ Database Setup ---------------------

DB_PATH = "feedback.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            admin_token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_open INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            message TEXT NOT NULL,
            emoji TEXT DEFAULT '💬',
            created_at TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES rooms(id)
        )
    """)
    conn.commit()
    conn.close()

# ------------ WebSocket Management -----------------------------

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, room_id: str):
        await websocket.accept()
        if room_id not in self.active_connections:
            self.active_connections[room_id] = []
        self.active_connections[room_id].append(websocket)

    def disconnect(self, websocket: WebSocket, room_id: str):
        if room_id in self.active_connections:
            self.active_connections[room_id].remove(websocket)

    async def broadcast(self, room_id: str, message: dict):
        if room_id in self.active_connections:
            dead = []
            for ws in self.active_connections[room_id]:
                try:
                    await ws.send_text(json.dumps(message))
                except:
                    dead.append(ws)
            for ws in dead:
                self.active_connections[room_id].remove(ws)

    def count(self, room_id: str) -> int:
        return len(self.active_connections.get(room_id, []))

manager = ConnectionManager()

# ------------ App Setup -----------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Anonymous Feedback Board", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend static files
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/static", StaticFiles(directory=frontend_path), name="static")

# ------------ Schemas -----------------------------

class CreateRoom(BaseModel):
    title: str
    description: Optional[str] = ""

class SubmitFeedback(BaseModel):
    message: str
    emoji: Optional[str] = "💬"

class ToggleRoom(BaseModel):
    admin_token: str

# ------------ Routes -----------------------------

@app.get("/")
def root():
    return FileResponse(os.path.join(frontend_path, "index.html"))

@app.get("/room")
def room_page():
    return FileResponse(os.path.join(frontend_path, "room.html"))

@app.get("/admin")
def admin_page():
    return FileResponse(os.path.join(frontend_path, "admin.html"))


@app.post("/api/rooms")
def create_room(body: CreateRoom, db: sqlite3.Connection = Depends(get_db)):
    room_id = str(uuid.uuid4())[:8].upper()
    admin_token = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    db.execute(
        "INSERT INTO rooms (id, title, description, admin_token, created_at) VALUES (?, ?, ?, ?, ?)",
        (room_id, body.title, body.description, admin_token, now)
    )
    db.commit()
    return {
        "room_id": room_id,
        "admin_token": admin_token,
        "admin_url": f"/admin?id={room_id}&token={admin_token}",
        "room_url": f"/room?id={room_id}"
    }


@app.get("/api/rooms/{room_id}")
def get_room(room_id: str, db: sqlite3.Connection = Depends(get_db)):
    room = db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    count = db.execute("SELECT COUNT(*) as c FROM feedback WHERE room_id = ?", (room_id,)).fetchone()["c"]
    return {
        "id": room["id"],
        "title": room["title"],
        "description": room["description"],
        "created_at": room["created_at"],
        "is_open": bool(room["is_open"]),
        "feedback_count": count,
        "live_viewers": manager.count(room_id)
    }


@app.get("/api/rooms/{room_id}/feedback")
def get_feedback(room_id: str, token: str, db: sqlite3.Connection = Depends(get_db)):
    room = db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if room["admin_token"] != token:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    rows = db.execute(
        "SELECT * FROM feedback WHERE room_id = ? ORDER BY created_at DESC", (room_id,)
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/rooms/{room_id}/feedback")
async def submit_feedback(room_id: str, body: SubmitFeedback, db: sqlite3.Connection = Depends(get_db)):
    room = db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if not room["is_open"]:
        raise HTTPException(status_code=403, detail="Room is closed")
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(body.message) > 1000:
        raise HTTPException(status_code=400, detail="Message too long (max 1000 chars)")

    feedback_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    db.execute(
        "INSERT INTO feedback (id, room_id, message, emoji, created_at) VALUES (?, ?, ?, ?, ?)",
        (feedback_id, room_id, body.message.strip(), body.emoji, now)
    )
    db.commit()

    payload = {
        "type": "new_feedback",
        "feedback": {
            "id": feedback_id,
            "message": body.message.strip(),
            "emoji": body.emoji,
            "created_at": now
        }
    }
    await manager.broadcast(room_id, payload)
    return {"success": True, "id": feedback_id}


@app.post("/api/rooms/{room_id}/toggle")
async def toggle_room(room_id: str, body: ToggleRoom, db: sqlite3.Connection = Depends(get_db)):
    room = db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if room["admin_token"] != body.admin_token:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    new_status = 0 if room["is_open"] else 1
    db.execute("UPDATE rooms SET is_open = ? WHERE id = ?", (new_status, room_id))
    db.commit()
    await manager.broadcast(room_id, {"type": "room_status", "is_open": bool(new_status)})
    return {"is_open": bool(new_status)}


@app.delete("/api/rooms/{room_id}/feedback/{feedback_id}")
async def delete_feedback(room_id: str, feedback_id: str, token: str, db: sqlite3.Connection = Depends(get_db)):
    room = db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if not room or room["admin_token"] != token:
        raise HTTPException(status_code=403, detail="Unauthorized")
    db.execute("DELETE FROM feedback WHERE id = ? AND room_id = ?", (feedback_id, room_id))
    db.commit()
    await manager.broadcast(room_id, {"type": "delete_feedback", "id": feedback_id})
    return {"success": True}


#   --------- WebSocket Endpoint -----------------------------

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await manager.connect(websocket, room_id)
    await manager.broadcast(room_id, {"type": "viewer_count", "count": manager.count(room_id)})
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        manager.disconnect(websocket, room_id)
        await manager.broadcast(room_id, {"type": "viewer_count", "count": manager.count(room_id)})