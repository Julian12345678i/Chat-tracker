import json
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext


# Where the public server lives. For testing on your own PC use "127.0.0.1" and 5000.
DEFAULT_HOST = "thomas.proxy.rlwy.net"
DEFAULT_PORT = 22078


class ChatApp:
    def __init__(self, root):
        self.root = root
        self.root.title("TCP Chat")
        self.root.geometry("480x560")

        self.sock = None
        self.reader = None
        self.username = None
        self.incoming = queue.Queue()  # receiver thread -> UI thread
        self.mute_end = 0
        self.mute_job = None

        # typing indicator (UDP, with TCP fallback)
        self.udp = None
        self.udp_ready = False
        self.server_host = None
        self.server_port = None
        self.typing = {}            # username -> time until which we show "is typing"
        self.typing_active = False  # are WE currently marked as typing?
        self.typing_job = None
        self.last_typing_sent = 0

        self.login_frame = tk.Frame(root, padx=30, pady=30)
        self.chat_frame = tk.Frame(root)

        self.build_login()
        self.build_chat()
        self.login_frame.pack(fill="both", expand=True)

        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- Login screen ----------
    def build_login(self):
        f = self.login_frame
        tk.Label(f, text="Join the Chat", font=("Segoe UI", 18, "bold")).pack(pady=(0, 20))

        tk.Label(f, text="Server address").pack(anchor="w")
        self.host_entry = tk.Entry(f, font=("Segoe UI", 11))
        self.host_entry.insert(0, DEFAULT_HOST)
        self.host_entry.pack(fill="x", pady=(0, 10))

        tk.Label(f, text="Port").pack(anchor="w")
        self.port_entry = tk.Entry(f, font=("Segoe UI", 11))
        self.port_entry.insert(0, str(DEFAULT_PORT))
        self.port_entry.pack(fill="x", pady=(0, 10))

        tk.Label(f, text="Username").pack(anchor="w")
        self.name_entry = tk.Entry(f, font=("Segoe UI", 11))
        self.name_entry.pack(fill="x", pady=(0, 20))
        self.name_entry.bind("<Return>", lambda e: self.connect())

        tk.Button(f, text="Connect", font=("Segoe UI", 11, "bold"),
                  command=self.connect).pack(fill="x")

        self.status = tk.Label(f, text="", fg="red", wraplength=400)
        self.status.pack(pady=10)

    # ---------- Chat screen ----------
    def build_chat(self):
        f = self.chat_frame

        top = tk.Frame(f, padx=10, pady=8)
        top.pack(fill="x")
        self.title_label = tk.Label(top, text="", font=("Segoe UI", 12, "bold"))
        self.title_label.pack(side="left")
        tk.Button(top, text="Leave", command=self.leave).pack(side="right")

        self.chat_box = scrolledtext.ScrolledText(
            f, state="disabled", wrap="word", font=("Segoe UI", 10), padx=8, pady=8
        )
        self.chat_box.pack(fill="both", expand=True, padx=10)
        self.chat_box.tag_config("info", foreground="gray", font=("Segoe UI", 9, "italic"))
        self.chat_box.tag_config("me", foreground="#0b5cad")
        self.chat_box.tag_config("name", font=("Segoe UI", 10, "bold"))
        self.chat_box.tag_config("flag", foreground="white", background="#c62828",
                                 font=("Segoe UI", 8, "bold"))

        self.typing_label = tk.Label(f, text="", fg="gray", anchor="w",
                                     font=("Segoe UI", 9, "italic"))
        self.typing_label.pack(fill="x", padx=12)

        self.mute_label = tk.Label(f, text="", fg="#c62828", font=("Segoe UI", 10, "bold"))
        self.mute_label.pack(fill="x")

        bottom = tk.Frame(f, padx=10, pady=10)
        bottom.pack(fill="x")
        self.msg_entry = tk.Entry(bottom, font=("Segoe UI", 11))
        self.msg_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.msg_entry.bind("<Return>", lambda e: self.send_message())
        self.msg_entry.bind("<KeyRelease>", self.on_key)
        self.send_btn = tk.Button(bottom, text="Send", command=self.send_message)
        self.send_btn.pack(side="right", padx=(8, 0))

    # ---------- Networking ----------
    def send_json(self, message):
        self.sock.sendall((json.dumps(message) + "\n").encode("utf-8"))

    def connect(self):
        host = self.host_entry.get().strip()
        name = self.name_entry.get().strip()
        try:
            port = int(self.port_entry.get().strip())
        except ValueError:
            self.status.config(text="Port must be a number")
            return
        if not name:
            self.status.config(text="Please enter a username")
            return

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((host, port))
            sock.settimeout(None)
            reader = sock.makefile("r", encoding="utf-8")
            sock.sendall((json.dumps({"type": "join", "username": name}) + "\n").encode("utf-8"))
            first_line = reader.readline()
            if not first_line:
                sock.close()
                self.status.config(
                    text="Reached the address, but the server closed the connection. "
                         "The server may be offline or restarting - try again in a moment."
                )
                return
            reply = json.loads(first_line)
        except (OSError, json.JSONDecodeError) as e:
            self.status.config(text=f"Could not connect: {e}")
            return

        if reply.get("type") == "error":
            self.status.config(text=reply.get("text", "Join failed"))
            sock.close()
            return

        self.sock, self.reader, self.username = sock, reader, name
        self.status.config(text="")
        self.login_frame.pack_forget()
        self.chat_frame.pack(fill="both", expand=True)
        self.title_label.config(text=f"Chatting as {name}")
        self.add_line(reply.get("text", ""), "info")

        self.server_host, self.server_port = host, port
        self.setup_udp(host, port, reply.get("token"))

        threading.Thread(target=self.receive_loop, daemon=True).start()
        self.root.after(100, self.process_queue)
        self.msg_entry.focus()

    def receive_loop(self):
        try:
            for line in self.reader:
                try:
                    self.incoming.put(json.loads(line))
                except json.JSONDecodeError:
                    pass
        except (OSError, ValueError):
            pass
        self.incoming.put({"type": "disconnected"})

    def process_queue(self):
        # Runs on the UI thread, so it is safe to touch widgets here
        while not self.incoming.empty():
            msg = self.incoming.get()
            kind = msg.get("type")
            if kind == "typing":
                who = msg.get("from")
                if who and who != self.username:
                    if msg.get("typing"):
                        self.typing[who] = time.time() + 5  # UDP can lose packets, so expire
                    else:
                        self.typing.pop(who, None)
            elif kind == "message":
                sender = msg.get("from")
                self.typing.pop(sender, None)
                self.add_message(sender, msg.get("text", ""), sender == self.username,
                                 msg.get("flagged", False))
            elif kind == "muted":
                self.add_line(msg.get("text", ""), "info")
                self.start_mute(msg.get("seconds", 0))
            elif kind == "disconnected":
                if self.sock:
                    self.add_line("Disconnected from server.", "info")
                    self.close_socket()
                return
            else:
                self.add_line(msg.get("text", ""), "info")
        self.update_typing_label()
        if self.sock:
            self.root.after(100, self.process_queue)

    def send_message(self):
        text = self.msg_entry.get().strip()
        if not text or not self.sock:
            return
        try:
            self.send_json({"type": "message", "text": text})
            self.msg_entry.delete(0, "end")
            self.stop_typing()
        except OSError:
            self.add_line("Could not send message.", "info")

    # ---------- Typing indicator (UDP, with TCP fallback) ----------
    def setup_udp(self, host, port, token):
        self.udp_ready = False
        try:
            target = (socket.gethostbyname(host), port)
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            return
        self.udp = udp
        self.udp_target = target
        threading.Thread(target=self.udp_loop, args=(udp, token), daemon=True).start()

    def udp_loop(self, udp, token):
        udp.settimeout(1.0)
        attempts = 0
        hello = {"type": "udp_hello", "username": self.username, "token": token}
        while self.sock and self.udp is udp:
            if not self.udp_ready and attempts < 5:
                attempts += 1  # UDP has no delivery guarantee, so try a few times
                try:
                    udp.sendto(json.dumps(hello).encode("utf-8"), self.udp_target)
                except OSError:
                    pass
            try:
                data, _ = udp.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if msg.get("type") == "udp_ok":
                self.udp_ready = True
            elif msg.get("type") == "typing":
                self.incoming.put(msg)  # the main thread will draw it

    def send_typing(self, flag):
        if not self.sock:
            return
        self.typing_active = flag
        msg = {"type": "typing", "typing": flag}
        if self.udp_ready and self.udp:
            try:
                msg["username"] = self.username
                self.udp.sendto(json.dumps(msg).encode("utf-8"), self.udp_target)
                return
            except OSError:
                msg.pop("username", None)
        try:
            self.send_json(msg)  # fallback over TCP
        except OSError:
            pass

    def on_key(self, event):
        if event.keysym in ("Return", "Shift_L", "Shift_R", "Control_L", "Control_R",
                            "Alt_L", "Alt_R", "Caps_Lock", "Tab"):
            return
        if not self.msg_entry.get().strip():
            self.stop_typing()
            return
        now = time.time()
        if now - self.last_typing_sent > 2.0:  # do not flood the network
            self.send_typing(True)
            self.last_typing_sent = now
        if self.typing_job:
            self.root.after_cancel(self.typing_job)
        self.typing_job = self.root.after(3000, self.stop_typing)

    def stop_typing(self):
        if self.typing_job:
            self.root.after_cancel(self.typing_job)
            self.typing_job = None
        if self.typing_active:
            self.send_typing(False)
        self.typing_active = False
        self.last_typing_sent = 0

    def update_typing_label(self):
        now = time.time()
        for name, until in list(self.typing.items()):
            if until < now:
                del self.typing[name]
        names = sorted(self.typing)
        if not names:
            text = ""
        elif len(names) == 1:
            text = f"{names[0]} is typing..."
        elif len(names) == 2:
            text = f"{names[0]} and {names[1]} are typing..."
        else:
            text = "Several people are typing..."
        if text != self.typing_label.cget("text"):
            self.typing_label.config(text=text)

    # ---------- Mute countdown ----------
    def start_mute(self, seconds):
        self.stop_typing()
        self.mute_end = time.time() + seconds
        self.msg_entry.config(state="disabled")
        self.send_btn.config(state="disabled")
        if self.mute_job:
            self.root.after_cancel(self.mute_job)
        self.tick_mute()

    def tick_mute(self):
        remaining = int(self.mute_end - time.time() + 0.99)
        if remaining <= 0 or not self.sock:
            self.end_mute()
            return
        minutes, secs = divmod(remaining, 60)
        self.mute_label.config(text=f"Muted - you can type again in {minutes}:{secs:02d}")
        self.mute_job = self.root.after(500, self.tick_mute)

    def end_mute(self):
        self.mute_job = None
        self.mute_label.config(text="")
        self.msg_entry.config(state="normal")
        self.send_btn.config(state="normal")
        if self.sock:
            self.msg_entry.focus()

    # ---------- Display helpers ----------
    def add_line(self, text, tag):
        self.chat_box.config(state="normal")
        self.chat_box.insert("end", text + "\n", tag)
        self.chat_box.config(state="disabled")
        self.chat_box.see("end")

    def add_message(self, sender, text, mine, flagged=False):
        self.chat_box.config(state="normal")
        if flagged:
            self.chat_box.insert("end", "\u26a0 FLAGGED ", ("flag",))
        self.chat_box.insert("end", f"{sender}: ", ("name", "me") if mine else ("name",))
        self.chat_box.insert("end", text + "\n", ("me",) if mine else ())
        self.chat_box.config(state="disabled")
        self.chat_box.see("end")

    # ---------- Cleanup ----------
    def close_socket(self):
        udp, self.udp = self.udp, None
        self.udp_ready = False
        if udp:
            try:
                udp.close()
            except OSError:
                pass
        self.typing.clear()
        sock, self.sock = self.sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def leave(self):
        self.close_socket()
        if self.mute_job:
            self.root.after_cancel(self.mute_job)
        self.end_mute()
        self.chat_frame.pack_forget()
        self.chat_box.config(state="normal")
        self.chat_box.delete("1.0", "end")
        self.chat_box.config(state="disabled")
        self.login_frame.pack(fill="both", expand=True)

    def on_close(self):
        self.close_socket()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    ChatApp(root)
    root.mainloop()
