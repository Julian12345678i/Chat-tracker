import json
import math
import os
import re
import secrets
import socket
import threading
import time

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 5000))  # Railway can set PORT; default 5000 on your PC

clients = {}
strikes = {}  # username -> number of times they used blocked words
mute_until = {}  # username -> time (seconds since epoch) when they can type again

# Mute settings: strike 1 = 1 minute, strike 2 = 2 minutes, ... capped at 10 minutes
MUTE_STEP_SECONDS = 60
MAX_MUTE_SECONDS = 600
clients_lock = threading.Lock()

# ---------- UDP typing indicator ----------
# Typing notices are tiny and unimportant if one is lost, which makes UDP a good fit:
# no connection, no waiting for confirmation. If a client's UDP packets can't get through
# (for example on Railway, which only offers a public TCP proxy), the same notice is
# sent over the normal TCP connection instead.
udp_sock = None
udp_addrs = {}   # username -> (ip, port) of their registered UDP address
udp_tokens = {}  # username -> secret token given at join, so nobody can fake a registration

# ---------- Word filter ----------
# Add or remove words here. Insults are included too.
BAD_WORDS = [
    "fuck", "shit", "bitch", "asshole", "ass", "bastard", "dick", "cunt",
    "damn", "piss", "slut", "whore", "idiot", "stupid", "dumb", "moron",
    "loser", "retard", "trash", "ugly", "fool",
]

# Racist slurs and phrases. Add more here if you spot any that slip through.
RACIST_WORDS = [
    "nigger", "nigga", "niggaz", "chink", "gook", "spic", "wetback", "kike",
    "coon", "darkie", "towelhead", "raghead", "beaner", "paki", "jap", "honky",
    "zipperhead", "porch monkey", "sand nigger",
    "go back to africa", "go back to your country",
]

BAD_WORDS = BAD_WORDS + RACIST_WORDS


def _to_regex(word):
    # Phrases: allow any amount of whitespace between the words
    return r"\s+".join(re.escape(part) for part in word.split())


_pattern = re.compile(
    r"\b(" + "|".join(sorted(map(_to_regex, BAD_WORDS), key=len, reverse=True))
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


def send_udp(addr, message):
    try:
        udp_sock.sendto(json.dumps(message).encode("utf-8"), addr)
    except (OSError, AttributeError):
        pass


def relay_typing(username, is_typing):
    """Tell everyone else that username started or stopped typing."""
    message = {"type": "typing", "from": username, "typing": is_typing}
    with clients_lock:
        targets = [(n, c, udp_addrs.get(n)) for n, c in clients.items() if n != username]
    for _name, conn, udp_addr in targets:
        if udp_addr:
            send_udp(udp_addr, message)      # fast path: UDP
        else:
            try:
                send_json(conn, message)     # fallback: normal TCP connection
            except OSError:
                pass


def is_muted(username):
    with clients_lock:
        return mute_until.get(username, 0) > time.time()


def udp_loop():
    """Receives UDP datagrams: registrations and typing notices."""
    while True:
        try:
            data, addr = udp_sock.recvfrom(2048)
        except OSError:
            break
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(msg, dict):
            continue
        kind = msg.get("type")
        name = str(msg.get("username", "")).strip()

        with clients_lock:
            known = name in clients
            registered = udp_addrs.get(name)
            token_ok = known and msg.get("token") == udp_tokens.get(name)

        if kind == "udp_hello" and token_ok:
            with clients_lock:
                udp_addrs[name] = addr
            send_udp(addr, {"type": "udp_ok"})
            print(f"[UDP] {name} registered {addr}")
        elif kind == "typing" and known and registered == addr and not is_muted(name):
            relay_typing(name, bool(msg.get("typing")))


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
                token = secrets.token_hex(8)
                with clients_lock:
                    udp_tokens[username] = token
                send_json(conn, {"type": "info", "text": f"Welcome, {username}!", "token": token})
                left = math.ceil(mute_until.get(username, 0) - time.time())
                if left > 0:
                    send_json(conn, {"type": "muted", "seconds": left,
                                     "text": f"You are still muted for {describe(left)}."})
                broadcast({"type": "info", "text": f"{username} joined the chat"}, exclude=username)
                print(f"[JOINED] {username}")

            elif kind == "typing":
                if not is_muted(username):
                    relay_typing(username, bool(msg.get("typing")))

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
                udp_addrs.pop(username, None)
                udp_tokens.pop(username, None)
            relay_typing(username, False)
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

    # UDP socket on the same port number (TCP and UDP ports are separate, so this is allowed)
    global udp_sock
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.bind((HOST, PORT))
    threading.Thread(target=udp_loop, daemon=True).start()

    print(f"[SERVER] Listening on port {PORT}... (press Ctrl+C to stop)")
    print(f"[SERVER] UDP typing listener on port {PORT}")
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
        udp_sock.close()


if __name__ == "__main__":
    main()
