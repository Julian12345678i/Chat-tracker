import json
import math
import re
import socket
import threading
import time

HOST = "0.0.0.0"
PORT = 5000

clients = {}
strikes = {}  # username -> number of times they used blocked words
mute_until = {}  # username -> time (seconds since epoch) when they can type again

# Mute settings: strike 1 = 1 minute, strike 2 = 2 minutes, ... capped at 10 minutes
MUTE_STEP_SECONDS = 60
MAX_MUTE_SECONDS = 600
clients_lock = threading.Lock()

# ---------- Word filter ----------
# Add or remove words here. Insults are included too.
BAD_WORDS = [
    "fuck", "shit", "bitch", "asshole", "ass", "bastard", "dick", "cunt",
    "damn", "piss", "slut", "whore", "idiot", "stupid", "dumb", "moron",
    "loser", "retard", "trash", "ugly", "fool",
]

_pattern = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, BAD_WORDS), key=len, reverse=True))
    + r")(s|es|ed|er|ers|ing|in|y)?\b",
    re.IGNORECASE,
)


def _mask(match):
    base = match.group(1)
    full = match.group(0)
    keep = (len(base) - 1) // 2  # fuck -> f***, bitch -> bi***
    return full[:keep] + "*" * (len(full) - keep)


def censor(text):
    """Returns (cleaned_text, number_of_bad_words_found)."""
    return _pattern.subn(_mask, text)


def mute_length(strike_count):
    return min(strike_count * MUTE_STEP_SECONDS, MAX_MUTE_SECONDS)


def describe(seconds):
    minutes, secs = divmod(int(seconds), 60)
    parts = []
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    if secs:
        parts.append(f"{secs} second" + ("s" if secs != 1 else ""))
    return " ".join(parts) or "0 seconds"


# ---------- Networking ----------
def send_json(sock, message):
    sock.sendall((json.dumps(message) + "\n").encode("utf-8"))


def broadcast(message, exclude=None):
    with clients_lock:
        for name, sock in list(clients.items()):
            if name == exclude:
                continue
            try:
                send_json(sock, message)
            except OSError:
                pass


def handle_client(conn, addr):
    username = None
    print(f"[NEW CONNECTION] {addr}")
    try:
        reader = conn.makefile("r", encoding="utf-8")
        for line in reader:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                send_json(conn, {"type": "error", "text": "Invalid message format"})
                continue

            kind = msg.get("type")

            if username is None:
                if kind != "join":
                    send_json(conn, {"type": "error", "text": "Send a join message first"})
                    continue
                name = str(msg.get("username", "")).strip()
                if not name:
                    send_json(conn, {"type": "error", "text": "Username cannot be empty"})
                    continue
                clean_name, bad = censor(name)
                if bad:
                    send_json(conn, {"type": "error", "text": "That username is not allowed"})
                    continue
                with clients_lock:
                    taken = name in clients
                    if not taken:
                        clients[name] = conn
                if taken:
                    send_json(conn, {"type": "error", "text": "Username already taken"})
                    continue
                username = name
                send_json(conn, {"type": "info", "text": f"Welcome, {username}!"})
                left = math.ceil(mute_until.get(username, 0) - time.time())
                if left > 0:
                    send_json(conn, {"type": "muted", "seconds": left,
                                     "text": f"You are still muted for {describe(left)}."})
                broadcast({"type": "info", "text": f"{username} joined the chat"}, exclude=username)
                print(f"[JOINED] {username}")

            elif kind == "message":
                clean, bad = censor(msg.get("text", ""))
                now = time.time()

                with clients_lock:
                    was_muted = mute_until.get(username, 0) > now
                    if bad:
                        strikes[username] = strikes.get(username, 0) + 1
                        mute_until[username] = now + mute_length(strikes[username])
                    count = strikes.get(username, 0)
                    remaining = max(0, math.ceil(mute_until.get(username, 0) - now))

                if was_muted:
                    # Muted: message is dropped. Cursing again extends the mute.
                    if bad:
                        print(f"[FLAGGED while muted] {username} (strike {count})")
                        text = f"You tried to curse while muted. Mute extended to {describe(remaining)}."
                    else:
                        text = f"You are muted for {describe(remaining)}."
                    send_json(conn, {"type": "muted", "seconds": remaining, "text": text})
                    continue

                if bad:
                    print(f"[FLAGGED] {username} (strike {count}, muted {remaining}s)")
                    broadcast({
                        "type": "info",
                        "text": f"{username} was flagged for inappropriate language "
                                f"(strike {count}) and is muted for {describe(remaining)}",
                    })

                broadcast({
                    "type": "message",
                    "from": username,
                    "text": clean,
                    "flagged": count > 0,
                    "strikes": count,
                })

                if bad:
                    send_json(conn, {
                        "type": "muted",
                        "seconds": remaining,
                        "text": f"Warning: inappropriate language is not allowed. "
                                f"You are muted for {describe(remaining)}.",
                    })

    except OSError:
        pass
    finally:
        if username:
            with clients_lock:
                clients.pop(username, None)
            broadcast({"type": "info", "text": f"{username} left the chat"})
            print(f"[LEFT] {username}")
        conn.close()
        print(f"[DISCONNECTED] {addr}")


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen()
    server.settimeout(1.0)
    print(f"[SERVER] Listening on port {PORT}... (press Ctrl+C to stop)")
    try:
        while True:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down.")
    finally:
        server.close()


if __name__ == "__main__":
    main()
