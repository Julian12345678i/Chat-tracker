import json
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

# ==============================
# CONFIGURATION
# ==============================

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

app = FastAPI(title="OG Chat")

if not WEB_DIR.exists():
    WEB_DIR.mkdir(parents=True)

app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# ==============================
# ROOM STORAGE
# ==============================

rooms = {
    "lobby": {
        "private": False,
        "users": {}
    }
}


class RoomRequest(BaseModel):
    private: bool = False


# ==============================
# WEBSITE ROUTES
# ==============================

@app.get("/")
async def home():
    # This serves your index.html when you go to http://127.0.0.1:8000
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/ice")
async def ice_config():
    """STUN always; TURN if TURN_URL/TURN_USERNAME/TURN_CREDENTIAL are set.

    Calls are WebRTC: audio travels peer-to-peer over UDP (SRTP). Railway
    cannot relay UDP, so a TURN server is the fallback for users behind
    strict NATs / mobile carrier networks.
    """
    servers = [{"urls": "stun:stun.l.google.com:19302"}]

    turn_url = os.environ.get("TURN_URL")
    if turn_url:
        servers.append({
            "urls": [u.strip() for u in turn_url.split(",") if u.strip()],
            "username": os.environ.get("TURN_USERNAME", ""),
            "credential": os.environ.get("TURN_CREDENTIAL", "")
        })

    return {"iceServers": servers}


@app.get("/health")
async def health():
    return {
        "status": "online",
        "app": "OG Chat"
    }


# ==============================
# ROOM MANAGEMENT
# ==============================

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


@app.get("/api/rooms")
async def list_rooms():

    return [
        {
            "room_id": room_id,
            "private": room["private"],
            "online": len(room["users"])
        }
        for room_id, room in rooms.items()
        if not room["private"]
    ]


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


# ==============================
# MESSAGE BROADCAST
# ==============================

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
            if room["users"].get(username) is connection:
                room["users"].pop(username, None)


async def send_to_user(room_id, target, message):

    room = rooms.get(room_id)

    if not room:
        return False

    connection = room["users"].get(target)

    if connection is None:
        return False

    try:
        await connection.send_json(message)

    except Exception:
        if room["users"].get(target) is connection:
            room["users"].pop(target, None)
        return False

    return True


# ==============================
# WEBSOCKET CHAT
# ==============================

@app.websocket("/ws/{room_id}")
async def chat_socket(
    websocket: WebSocket,
    room_id: str,
    username: str
):

    await websocket.accept()

    room = rooms.get(room_id)

    if not room:
        await websocket.close(code=4404)
        return

    username = username.strip()[:24]

    if not username:
        await websocket.close(code=4400)
        return

    if username in room["users"]:
        await websocket.close(code=4409)
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

            # Normal messages
            if kind == "message":

                message = str(data.get("text", "")).strip()

                if not message:
                    continue

                await broadcast(room_id, {
                    "type": "message",
                    "from": username,
                    "text": message[:2000]
                })

            # Typing notifications
            elif kind == "typing":

                await broadcast(room_id, {
                    "type": "typing",
                    "from": username,
                    "typing": bool(data.get("typing"))
                }, exclude=username)

            # WebRTC signaling - forwarded only to the target user
            elif kind in {
                "webrtc_offer",
                "webrtc_answer",
                "webrtc_ice_candidate",
                "webrtc_hangup",
                "call_decline"
            }:

                target = str(data.get("to", "")).strip()

                if not target:
                    continue

                signal = {
                    "type": kind,
                    "from": username,
                    "to": target
                }

                if kind == "webrtc_offer":
                    signal["offer"] = data.get("offer")

                elif kind == "webrtc_answer":
                    signal["answer"] = data.get("answer")

                elif kind == "webrtc_ice_candidate":
                    signal["candidate"] = data.get("candidate")

                delivered = await send_to_user(room_id, target, signal)

                if not delivered:
                    await websocket.send_json({
                        "type": "error",
                        "text": f"User {target} is not in this room"
                    })

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


# ==============================
# SERVER STARTUP
# ==============================

@app.on_event("startup")
async def startup():
    print("==============================")
    print("       OG CHAT ONLINE")
    print("==============================")
    print("Web server started")
    print("WebSocket chat ready")
    print("Public lobby available")
    print("==============================")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    uvicorn.run(app, host=host, port=port)