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

        self.mute_label = tk.Label(f, text="", fg="#c62828", font=("Segoe UI", 10, "bold"))
        self.mute_label.pack(fill="x")

        bottom = tk.Frame(f, padx=10, pady=10)
        bottom.pack(fill="x")
        self.msg_entry = tk.Entry(bottom, font=("Segoe UI", 11))
        self.msg_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.msg_entry.bind("<Return>", lambda e: self.send_message())
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
            reply = json.loads(reader.readline())
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
            if kind == "message":
                sender = msg.get("from")
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
        if self.sock:
            self.root.after(100, self.process_queue)

    def send_message(self):
        text = self.msg_entry.get().strip()
        if not text or not self.sock:
            return
        try:
            self.send_json({"type": "message", "text": text})
            self.msg_entry.delete(0, "end")
        except OSError:
            self.add_line("Could not send message.", "info")

    # ---------- Mute countdown ----------
    def start_mute(self, seconds):
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
