
import json
import secrets
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

app = FastAPI(title="OG Chat Web")

app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# Temporary in-memory room storage
rooms = {
    "lobby": {
        "private": False,
        "users": {}
    }
}


class RoomRequest(BaseModel):
    private: bool = False


@app.get("/")
async def home():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "online", "app": "OG Chat"}


# Create public or private rooms
@app.post("/api/rooms")
async def create_room(request: RoomRequest):
    room_id = secrets.token_urlsafe(8)

    rooms[room_id] = {
        "private": request.private,
        "users": {}
    }

    return {
        "room_id": room_id,
        "private": request.private,
        "invite_link": f"/?room={room_id}"
    }


# Get room information
@app.get("/api/rooms/{room_id}")
async def get_room(room_id: str):
    room = rooms.get(room_id)

    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    return {
        "room_id": room_id,
        "private": room["private"],
        "users": list(room["users"].keys()),
        "online": len(room["users"])
    }


# Send an event to everyone in a room
async def broadcast(room_id, message, exclude=None):
    room = rooms.get(room_id)

    if not room:
        return

    users = list(room["users"].items())

    for username, connection in users:
        if username == exclude:
            continue

        try:
            await connection.send_json(message)
        except Exception:
            room["users"].pop(username, None)


# WebSocket connection for each room
@app.websocket("/ws/{room_id}")
async def chat_socket(websocket: WebSocket, room_id: str, username: str):

    await websocket.accept()

    room = rooms.get(room_id)

    if not room:
        await websocket.close(code=4404, reason="Room not found")
        return

    username = username.strip()[:24]

    if not username:
        await websocket.close(code=4400, reason="Invalid username")
        return

    if username in room["users"]:
        await websocket.close(code=4409, reason="Username already in use")
        return

    room["users"][username] = websocket

    await websocket.send_json({
        "type": "welcome",
        "username": username,
        "room": room_id,
        "users": list(room["users"].keys())
    })

    await broadcast(room_id, {
        "type": "system",
        "text": f"{username} joined the room"
    }, exclude=username)

    try:
        while True:
            data = await websocket.receive_json()

            kind = data.get("type")

            if kind == "message":
                message = str(data.get("text", "")).strip()

                if not message:
                    continue

                await broadcast(room_id, {
                    "type": "message",
                    "from": username,
                    "text": message[:2000]
                })

            elif kind == "typing":
                await broadcast(room_id, {
                    "type": "typing",
                    "from": username,
                    "typing": bool(data.get("typing"))
                }, exclude=username)

    except WebSocketDisconnect:
        pass

    except Exception as error:
        print(f"[WEBSOCKET ERROR] {error}")

    finally:
        if room["users"].get(username) is websocket:
            room["users"].pop(username, None)

            await broadcast(room_id, {
                "type": "system",
                "text": f"{username} left the room"
            })
