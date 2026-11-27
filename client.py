import json
import os
import socket
import sys
import threading

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5000


def send_json(sock, message):
    sock.sendall((json.dumps(message) + "\n").encode("utf-8"))


def show(msg):
    kind = msg.get("type")
    if kind == "typing":
        return  # typing notices are only shown by the GUI client
    if kind == "message":
        flag = " [FLAGGED]" if msg.get("flagged") else ""
        print(f"\r[{msg.get('from')}{flag}] {msg.get('text')}")
    else:
        print(f"\r* {msg.get('text')}")


def receive_loop(reader):
    for line in reader:
        try:
            show(json.loads(line))
        except json.JSONDecodeError:
            pass
    print("\r* Disconnected from server.")
    os._exit(0)


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((HOST, PORT))
    except OSError as e:
        print(f"Could not connect to {HOST}:{PORT} - {e}")
        return
    reader = sock.makefile("r", encoding="utf-8")

    # Keep asking until the server accepts the username
    while True:
        name = input("Choose a username: ").strip()
        send_json(sock, {"type": "join", "username": name})
        reply = json.loads(reader.readline())
        show(reply)
        if reply.get("type") != "error":
            break

    threading.Thread(target=receive_loop, args=(reader,), daemon=True).start()
    print("Type a message and press Enter. Type /quit to leave.")

    try:
        while True:
            text = input()
            if text.strip() == "/quit":
                break
            if text.strip():
                send_json(sock, {"type": "message", "text": text})
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        sock.close()


if __name__ == "__main__":
    main()
