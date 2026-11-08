"""Tkinter chat client: login/registration screen, chat screen and connection status bar.

The chat screen has a sidebar with the rooms and the users who are online, and one
conversation per room or person. Tkinter must only be touched from the main thread
(SAD risk R8): the network handler and the connect worker put events on a queue,
and _poll drains it every 50 ms.
"""
import queue
import threading
import time
import tkinter as tk
from tkinter import simpledialog, ttk

from client.net.connection import ChatConnection

POLL_MS = 50
MAX_TEXT_CHARS = 2000

STATUS_COLOURS = {"connected": "#1a7f37", "busy": "#9a6700", "offline": "#cf222e"}

FRIENDLY_ERRORS = {
    "AUTH_FAILED": "Login failed. Check your username and password.",
    "SESSION_EXPIRED": "Your session has expired. Please log in again.",
    "SERVER_BUSY": "The server is full. Try again in a moment.",
}

ROOM, USER = "room", "user"


def describe_error(message):
    """Turn an ERROR message into text for the user, without exposing server internals."""
    code = message.get("code")
    return FRIENDLY_ERRORS.get(code) or message.get("reason") or f"Request failed ({code})"


def clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def conversation_title(conversation):
    kind, name = conversation
    return f"# {name}" if kind == ROOM else name


class ChatApp:
    def __init__(self, root, host="127.0.0.1", port=5050):
        self.root = root
        self.conn = None
        self.events = queue.Queue()
        self.username = None
        self.on_connected = None      # what to send once the connect worker succeeds
        self.pending_login = None     # credentials to log in with after REGISTER succeeds
        self.logging_out = False
        self.last_seq = {}            # (conversation, sender) -> last seq seen (CHAT-F-008)
        self.online = set()
        self.rooms = {}               # room name -> {"joined": bool, "members": int, "status": str}
        self.lines = {}               # conversation -> [(text, tag, meta)]
        self.unread = set()
        self.current = None           # (ROOM, name) or (USER, name)
        self._room_names = []         # what each sidebar row shows, in order
        self._user_names = []

        root.title("Chat")
        root.minsize(720, 460)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        self.host_var = tk.StringVar(value=host)
        self.port_var = tk.StringVar(value=str(port))
        self.user_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.text_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.title_var = tk.StringVar()

        self._build_status_bar()
        self._build_login()
        self._build_chat()
        self.show_login()
        self.set_status("Not connected", "offline")
        root.after(POLL_MS, self._poll)

    # ---------- layout ----------

    def _build_status_bar(self):
        bar = ttk.Frame(self.root, padding=(10, 4))
        bar.pack(side="bottom", fill="x")
        ttk.Separator(self.root).pack(side="bottom", fill="x")
        self.status_dot = tk.Label(bar, text="●", font=("TkDefaultFont", 11))
        self.status_dot.pack(side="left")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left", padx=(4, 0))

    def _build_login(self):
        self.login_frame = frame = ttk.Frame(self.root, padding=24)
        ttk.Label(frame, text="Chat", font=("TkDefaultFont", 16, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 16))
        fields = (("Server", self.host_var, {}), ("Port", self.port_var, {}),
                  ("Username", self.user_var, {}), ("Password", self.password_var, {"show": "•"}))
        self.login_entries = []
        for row, (label, var, options) in enumerate(fields, start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 12))
            entry = ttk.Entry(frame, textvariable=var, width=32, **options)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            entry.bind("<Return>", lambda _e: self.login())
            self.login_entries.append(entry)
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(16, 0))
        self.register_button = ttk.Button(buttons, text="Register", command=self.register)
        self.register_button.pack(side="left", padx=(0, 8))
        self.login_button = ttk.Button(buttons, text="Log in", command=self.login)
        self.login_button.pack(side="left")
        self.login_message = ttk.Label(frame, text="", foreground=STATUS_COLOURS["offline"], wraplength=420)
        self.login_message.grid(row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))
        frame.columnconfigure(1, weight=1)

    def _build_chat(self):
        self.chat_frame = frame = ttk.Frame(self.root, padding=10)

        sidebar = ttk.Frame(frame, width=210)
        sidebar.pack(side="left", fill="y", padx=(0, 10))
        sidebar.pack_propagate(False)
        ttk.Label(sidebar, text="Rooms", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.room_list = tk.Listbox(sidebar, height=8, activestyle="none", exportselection=False)
        self.room_list.pack(fill="both", expand=True, pady=(2, 4))
        self.room_list.bind("<<ListboxSelect>>", lambda _e: self._open_selected_room())
        room_buttons = ttk.Frame(sidebar)
        room_buttons.pack(fill="x")
        for column, (text, command) in enumerate((("New", self.ask_new_room), ("Join", self.join_selected_room),
                                                  ("Leave", self.leave_selected_room))):
            ttk.Button(room_buttons, text=text, width=1, command=command).grid(
                row=0, column=column, sticky="ew", padx=(0 if column == 0 else 4, 0))
            room_buttons.columnconfigure(column, weight=1)

        # Packed before the user list so it keeps its space when the window is small.
        ttk.Button(sidebar, text="Message someone…", command=self.ask_direct_message).pack(side="bottom", fill="x")
        ttk.Label(sidebar, text="Online", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(12, 0))
        self.user_list = tk.Listbox(sidebar, height=8, activestyle="none", exportselection=False)
        self.user_list.pack(fill="both", expand=True, pady=(2, 4))
        self.user_list.bind("<<ListboxSelect>>", lambda _e: self._open_selected_user())

        main = ttk.Frame(frame)
        main.pack(side="left", fill="both", expand=True)
        top = ttk.Frame(main)
        top.pack(fill="x")
        ttk.Label(top, textvariable=self.title_var, font=("TkDefaultFont", 11, "bold")).pack(side="left")
        self.logout_button = ttk.Button(top, text="Log out", command=self.logout)
        self.logout_button.pack(side="right")

        # Packed before the message log so the input row keeps its space when the window is small.
        bottom = ttk.Frame(main)
        bottom.pack(side="bottom", fill="x")
        self.message_entry = ttk.Entry(bottom, textvariable=self.text_var)
        self.message_entry.pack(side="left", fill="x", expand=True)
        self.message_entry.bind("<Return>", lambda _e: self.send_message())
        self.send_button = ttk.Button(bottom, text="Send", command=self.send_message)
        self.send_button.pack(side="left", padx=(8, 0))

        body = ttk.Frame(main)
        body.pack(fill="both", expand=True, pady=8)
        self.log = tk.Text(body, wrap="word", state="disabled", height=12, padx=8, pady=6,
                           relief="solid", borderwidth=1, font=("TkDefaultFont", 10))
        scroll = ttk.Scrollbar(body, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("me", justify="right", foreground="#0b5cad", rmargin=4)
        self.log.tag_configure("them", justify="left", foreground="#1f2328", lmargin1=4, lmargin2=4)
        self.log.tag_configure("meta", font=("TkDefaultFont", 8), foreground="#6e7781")
        self.log.tag_configure("system", justify="center", foreground="#6e7781", font=("TkDefaultFont", 9, "italic"))
        self.log.tag_configure("warning", justify="center", foreground="#9a6700", font=("TkDefaultFont", 9, "italic"))

    # ---------- screens and status ----------

    def show_login(self, message="", colour="offline"):
        self.chat_frame.pack_forget()
        self.login_frame.pack(fill="both", expand=True)
        self.login_message.configure(text=message, foreground=STATUS_COLOURS[colour])
        self._set_login_enabled(True)
        self.root.title("Chat")

    def show_chat(self):
        self.login_frame.pack_forget()
        self.chat_frame.pack(fill="both", expand=True)
        self.root.title(f"Chat — {self.username}")
        self.password_var.set("")
        self.current = None
        self.title_var.set("")
        self._render()
        self.add_line(f"Logged in as {self.username}. Pick a room or a person on the left.", "system")

    def set_status(self, text, state):
        self.status_var.set(text)
        self.status_dot.configure(foreground=STATUS_COLOURS[state])

    def _set_login_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for widget in (self.login_button, self.register_button, *self.login_entries):
            widget.configure(state=state)

    # ---------- sidebar ----------

    def _refresh_sidebar(self):
        self._room_names = sorted(self.rooms, key=str.casefold)
        self.room_list.delete(0, "end")
        for name in self._room_names:
            info = self.rooms[name]
            mark = "• " if (ROOM, name) in self.unread else ("✓ " if info["joined"] else "   ")
            lock = " (read-only)" if info["status"] == "RESTRICTED" else ""
            self.room_list.insert("end", f"{mark}#{name}  ({info['members']}){lock}")
        self._user_names = sorted(self.online - {self.username}, key=str.casefold)
        self.user_list.delete(0, "end")
        for name in self._user_names:
            mark = "• " if (USER, name) in self.unread else "   "
            self.user_list.insert("end", mark + name)

    def _selected(self, listbox, names):
        selection = listbox.curselection()
        return names[selection[0]] if selection else None

    def _open_selected_room(self):
        name = self._selected(self.room_list, self._room_names)
        if name is not None:
            self.open_conversation(ROOM, name)

    def _open_selected_user(self):
        name = self._selected(self.user_list, self._user_names)
        if name is not None:
            self.open_conversation(USER, name)

    def ask_new_room(self):
        name = simpledialog.askstring("New room", "Room name:", parent=self.root)
        if name and name.strip():
            self.create_room(name.strip())

    def ask_direct_message(self):
        name = simpledialog.askstring("Message someone", "Username:", parent=self.root)
        if name and name.strip():
            self.open_conversation(USER, name.strip())

    def join_selected_room(self):
        name = self._selected(self.room_list, self._room_names)
        if name is not None:
            self.join_room(name)

    def leave_selected_room(self):
        name = self._selected(self.room_list, self._room_names)
        if name is not None:
            self.leave_room(name)

    # ---------- conversations ----------

    def open_conversation(self, kind, name):
        self.current = (kind, name)
        self.unread.discard(self.current)
        self.title_var.set(conversation_title(self.current))
        self._render()
        self._refresh_sidebar()
        if kind == ROOM and not self.rooms.get(name, {}).get("joined"):
            self.add_line(f"You are not in #{name}. Click Join to take part.", "system")
        self.message_entry.focus_set()

    def _render(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        for text, tag, meta in self.lines.get(self.current, []):
            self._insert(text, tag, meta)

    def _insert(self, text, tag, meta):
        self.log.configure(state="normal")
        if meta:
            self.log.insert("end", meta + "\n", (tag, "meta"))
        self.log.insert("end", text + "\n\n" if meta else text + "\n", tag)
        self.log.configure(state="disabled")
        self.log.see("end")

    def add_line(self, text, tag, meta=None, conversation=None):
        """Add a line to a conversation (the current one by default), showing it if it is open."""
        conversation = conversation or self.current
        self.lines.setdefault(conversation, []).append((text, tag, meta))
        if conversation == self.current:
            self._insert(text, tag, meta)
        elif conversation is not None and tag in ("them", "warning"):
            self.unread.add(conversation)
            self._refresh_sidebar()

    # ---------- user actions ----------

    def login(self):
        self._start("LOGIN")

    def register(self):
        self._start("REGISTER")

    def _start(self, action):
        username, password = self.user_var.get().strip(), self.password_var.get()
        if not username or not password:
            self.login_message.configure(text="Enter a username and password.")
            return
        self.login_message.configure(text="")
        credentials = {"username": username, "password": password}
        self.pending_login = credentials if action == "REGISTER" else None

        def send():
            self.conn.request(action, credentials)

        if self.conn is not None and self.conn.connected:
            self._set_login_enabled(False)
            send()
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            self.login_message.configure(text="Port must be a number.")
            return
        host = self.host_var.get().strip() or "127.0.0.1"
        self._connect(host, port, send)

    def _connect(self, host, port, then):
        self._set_login_enabled(False)
        self.set_status(f"Connecting to {host}:{port}…", "busy")
        self.events = events = queue.Queue()
        self.conn = conn = ChatConnection(events)
        self.on_connected = then

        def worker():
            try:
                conn.connect(host, port)
                events.put(("connected", f"{host}:{port}"))
            except OSError as e:
                events.put(("connect_failed", f"Could not connect to {host}:{port} ({e.strerror or e})"))

        threading.Thread(target=worker, name="connect", daemon=True).start()

    def _request(self, message_type, payload):
        try:
            self.conn.request(message_type, payload)
            return True
        except (OSError, AttributeError):
            self.add_line("Not connected: the request was not sent.", "warning")
            return False

    def create_room(self, name):
        self._request("CREATE_ROOM", {"room": name})

    def join_room(self, name):
        self._request("JOIN", {"room": name})

    def leave_room(self, name):
        self._request("LEAVE", {"room": name})

    def send_message(self):
        text = self.text_var.get()
        if not text.strip():
            return
        if self.current is None:
            self.add_line("Pick a room or a person on the left first.", "warning")
            return
        if len(text) > MAX_TEXT_CHARS:
            self.add_line(f"Messages can be at most {MAX_TEXT_CHARS} characters.", "warning")
            return
        kind, name = self.current
        if not self._request("MSG", {kind if kind == ROOM else "to": name, "text": text}):
            return
        self.text_var.set("")
        self.add_line(text, "me", meta=f"You  {clock(time.time())}")

    def logout(self):
        if self.conn is None or not self.conn.connected:
            self._back_to_login("You have logged out.", "connected")
            return
        self.logging_out = True
        try:
            self.conn.request("LOGOUT")
        except OSError:
            self._back_to_login("You have logged out.", "connected")

    def quit(self):
        if self.conn is not None:
            self.conn.close()
        self.root.destroy()

    def _back_to_login(self, message, colour="offline"):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        self.username = None
        self.logging_out = False
        self.online.clear()
        self.rooms.clear()
        self.lines.clear()
        self.unread.clear()
        self.last_seq.clear()
        self.current = None
        self.set_status("Not connected", "offline")
        self.show_login(message, colour)

    # ---------- network events (main thread) ----------

    def _poll(self):
        try:
            while True:
                self.handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll)

    def handle_event(self, item):
        kind = item[0]
        if kind == "connected":
            self.set_status(f"Connected to {item[1]}", "busy")
            action, self.on_connected = self.on_connected, None
            if action:
                action()
        elif kind == "connect_failed":
            self.conn = None
            self.set_status("Not connected", "offline")
            self.show_login(item[1])
        elif kind == "disconnected":
            if self.logging_out:
                self._back_to_login("You have logged out.", "connected")
            elif self.username is not None:
                self._back_to_login(f"Disconnected from the server: {item[1]}.")
            else:
                self.conn = None
                self.set_status("Not connected", "offline")
                self._set_login_enabled(True)
        elif kind == "message":
            self._handle_message(item[1], item[2])

    def _handle_message(self, message, request_type):
        mtype = message.get("type")
        if mtype == "ACK":
            self._handle_ack(message, request_type)
        elif mtype == "ERROR":
            self._handle_error(message, request_type)
        elif mtype == "EVENT":
            self._handle_event(message.get("kind"), message.get("data", {}))

    def _handle_ack(self, message, request_type):
        status, data = message.get("status"), message.get("data", {})
        if request_type == "REGISTER":
            credentials, self.pending_login = self.pending_login, None
            self.login_message.configure(text="Account created. Logging in…",
                                         foreground=STATUS_COLOURS["connected"])
            if credentials:
                self.conn.request("LOGIN", credentials)
        elif request_type == "LOGIN":
            self.username = data.get("username")
            self.online = set(data.get("online", []))
            self.rooms = {r["name"]: {"joined": r["joined"], "members": r["members"], "status": r["status"]}
                          for r in data.get("rooms", [])}
            self.set_status(f"Connected as {self.username}", "connected")
            self.show_chat()
            self._refresh_sidebar()
        elif request_type == "LOGOUT":
            self._back_to_login("You have logged out.", "connected")
        elif request_type in ("CREATE_ROOM", "JOIN"):
            name = data["room"]
            self.rooms[name] = {**self.rooms.get(name, {"status": "OPEN"}),
                                "joined": True, "members": len(data.get("members", []))}
            self.open_conversation(ROOM, name)
            verb = "created" if request_type == "CREATE_ROOM" else "joined"
            self.add_line(f"You {verb} #{name}. Members: {', '.join(data.get('members', []))}", "system")
        elif request_type == "LEAVE":
            name = data["room"]
            info = self.rooms.setdefault(name, {"status": "OPEN", "members": 1})
            info.update(joined=False, members=max(0, info["members"] - 1))
            self.add_line(f"You left #{name}.", "system", conversation=(ROOM, name))
            self._refresh_sidebar()
        elif request_type == "MSG" and status == "NOT_DELIVERED":
            target = (ROOM, data["room"]) if "room" in data else (USER, data.get("to"))
            self.add_line(f"{data.get('reason')} — your message was not delivered.", "warning", conversation=target)

    def _handle_error(self, message, request_type):
        text = describe_error(message)
        if message.get("code") == "SESSION_EXPIRED" and self.username is not None:
            self._back_to_login(text)
        elif self.username is None:
            self.pending_login = None
            if message.get("code") == "DUPLICATE":
                text = "That username is already taken."
            self.show_login(text)
        else:
            self.add_line(text, "warning")

    def _handle_event(self, kind, data):
        if kind == "msg":
            self._show_incoming(data)
        elif kind == "presence":
            user = data.get("user")
            if data.get("status") == "online":
                self.online.add(user)
            else:
                self.online.discard(user)
            if (USER, user) in self.lines:
                self.add_line(f"{user} is now {data.get('status')}.", "system", conversation=(USER, user))
            self._refresh_sidebar()
        elif kind == "room" and data.get("action") == "created":
            self.rooms.setdefault(data["room"], {"joined": False, "members": 1, "status": "OPEN"})
            self._refresh_sidebar()
        elif kind in ("join", "leave"):
            name, user = data.get("room"), data.get("user")
            info = self.rooms.setdefault(name, {"joined": True, "members": 0, "status": "OPEN"})
            info["members"] = max(0, info["members"] + (1 if kind == "join" else -1))
            verb = "joined" if kind == "join" else "left"
            self.add_line(f"{user} {verb} the room.", "system", conversation=(ROOM, name))
            self._refresh_sidebar()

    def _show_incoming(self, data):
        sender, seq = data.get("from"), data.get("seq")
        conversation = (ROOM, data["room"]) if "room" in data else (USER, sender)
        key = (conversation, sender)
        last = self.last_seq.get(key)
        if isinstance(seq, int):
            if last is not None and seq <= last:
                self.add_line(f"Message from {sender} arrived out of order.", "warning", conversation=conversation)
            self.last_seq[key] = seq
        if self.current is None:
            self.open_conversation(*conversation)
        self.add_line(data.get("text", ""), "them",
                      meta=f"{sender}  {clock(data.get('ts', time.time()))}", conversation=conversation)
