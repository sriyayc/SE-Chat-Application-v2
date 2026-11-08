"""Issues #6 and #8: the Tkinter client against a real server
(CHAT-F-001, F-002, F-004, F-005, F-006, F-013, CHAT-NF-004; TC-UX-01).

Skipped where no display is available (for example on CI).
"""
import gc
import time

import pytest

tk = pytest.importorskip("tkinter")

from client.ui.app import ROOM, USER, ChatApp, describe_error  # noqa: E402


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display available ({e})")
    root.withdraw()
    yield root
    root.destroy()
    # Collect the app's Tk variables here, on the main thread. Left to the garbage
    # collector they can be freed on a server thread, which Tkinter does not allow.
    gc.collect()


def pump(root, condition, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if condition():
            return True
        time.sleep(0.01)
    return False


def chat_text(app):
    return app.log.get("1.0", "end")


def lines_with_tag(app, tag):
    ranges = app.log.tag_ranges(tag)
    return [app.log.get(start, end) for start, end in zip(ranges[0::2], ranges[1::2])]


def make_app(root, address, username, password="password-123"):
    app = ChatApp(root, *address)
    app.user_var.set(username)
    app.password_var.set(password)
    return app


def test_register_chat_and_logout(root, server_address, connect):
    app = make_app(root, server_address, "alice")
    app.register()
    assert pump(root, lambda: app.username == "alice"), app.login_message.cget("text")
    assert app.chat_frame.winfo_manager() == "pack"
    assert app.status_var.get() == "Connected as alice"
    assert app.password_var.get() == ""

    bob = connect().sign_in("bob")

    app.open_conversation(USER, "bob")
    app.text_var.set("hello bob")
    app.send_message()
    assert app.text_var.get() == ""
    event = bob.recv()
    assert event["data"] == {**event["data"], "from": "alice", "text": "hello bob"}
    assert any("hello bob" in line for line in lines_with_tag(app, "me"))

    assert pump(root, lambda: "bob" in app.online)
    bob.request("MSG", {"to": "alice", "text": "hi alice"})
    assert pump(root, lambda: "hi alice" in chat_text(app))
    assert any("hi alice" in line for line in lines_with_tag(app, "them"))

    carol = connect()
    carol.reply_to(carol.request("REGISTER", {"username": "carol", "password": "password-123"}))
    app.open_conversation(USER, "carol")
    app.text_var.set("anyone there?")
    app.send_message()
    assert pump(root, lambda: "carol is offline — your message was saved" in chat_text(app))

    app.logout()
    assert pump(root, lambda: app.login_frame.winfo_manager() == "pack")
    assert app.login_message.cget("text") == "You have logged out."
    assert app.status_var.get() == "Not connected"


def test_failed_login_shows_generic_message(root, server_address):
    app = make_app(root, server_address, "nobody")
    app.login()
    assert pump(root, lambda: "Login failed" in app.login_message.cget("text"))
    assert str(app.login_button.cget("state")) == "normal"


def test_unreachable_server_is_reported(root):
    app = make_app(root, ("127.0.0.1", 1), "alice")
    app.login()
    assert pump(root, lambda: "Could not connect" in app.login_message.cget("text"))
    assert app.status_var.get() == "Not connected"


def test_server_going_away_returns_to_login(root, make_server):
    server, address = make_server()
    app = make_app(root, address, "alice")
    app.register()
    assert pump(root, lambda: app.username == "alice")
    server.stop()
    assert pump(root, lambda: "Disconnected from the server" in app.login_message.cget("text"))


def test_empty_fields_are_caught_locally(root, server_address):
    app = make_app(root, server_address, "")
    app.login()
    assert app.login_message.cget("text") == "Enter a username and password."
    assert app.conn is None


def test_describe_error_hides_internals():
    assert describe_error({"code": "AUTH_FAILED", "reason": "x"}).startswith("Login failed")
    assert describe_error({"code": "NOT_FOUND", "reason": "there is no user called bob"}) == "there is no user called bob"
    assert describe_error({"code": "WHATEVER"}) == "Request failed (WHATEVER)"


def test_rooms_presence_and_unread_markers(root, server_address, connect):
    bob = connect().sign_in("bob")
    bob.reply_to(bob.request("CREATE_ROOM", {"room": "general"}))

    app = make_app(root, server_address, "alice")
    app.register()
    assert pump(root, lambda: app.username == "alice")
    assert app.online == {"alice", "bob"}
    assert app.rooms["general"]["joined"] is False
    assert app.user_list.get(0, "end") == ("   bob",)

    app.join_room("general")
    assert pump(root, lambda: app.rooms["general"]["joined"])
    assert app.current == (ROOM, "general")
    assert bob.next_event("join")["data"] == {"room": "general", "user": "alice"}

    app.text_var.set("hello room")
    app.send_message()
    assert bob.recv()["data"]["text"] == "hello room"
    bob.request("MSG", {"room": "general", "text": "hi alice"})
    assert pump(root, lambda: "hi alice" in chat_text(app))

    # A direct message for a conversation that is not open is marked unread.
    bob.request("MSG", {"to": "alice", "text": "psst"})
    assert pump(root, lambda: (USER, "bob") in app.unread)
    assert app.user_list.get(0) == "• bob"
    app.open_conversation(USER, "bob")
    assert "psst" in chat_text(app)
    assert (USER, "bob") not in app.unread

    app.create_room("random")
    assert pump(root, lambda: app.current == (ROOM, "random"))
    app.leave_room("random")
    assert pump(root, lambda: not app.rooms["random"]["joined"])

    bob.close()
    assert pump(root, lambda: "bob" not in app.online)
    app.open_conversation(USER, "bob")
    assert "bob is now offline." in chat_text(app)


def test_history_loads_when_a_conversation_opens(root, server_address, connect):
    alice_raw = connect().sign_in("alice")
    alice_raw.close()
    bob = connect().sign_in("bob")
    bob.reply_to(bob.request("MSG", {"to": "alice", "text": "sent while you were away"}))

    app = make_app(root, server_address, "alice")
    app.login()
    assert pump(root, lambda: app.username == "alice")
    app.open_conversation(USER, "bob")
    assert pump(root, lambda: "sent while you were away" in chat_text(app))

    # A live message is not duplicated by a second history load.
    bob.request("MSG", {"to": "alice", "text": "and now live"})
    assert pump(root, lambda: "and now live" in chat_text(app))
    app.open_conversation(USER, "bob")
    root.update()
    assert chat_text(app).count("sent while you were away") == 1
    assert chat_text(app).count("and now live") == 1


def test_admin_commands_and_being_deactivated(root, make_server, connect):
    server, address = make_server()
    first = connect(address)
    first.reply_to(first.request("REGISTER", {"username": "root", "password": "password-123"}))
    server.repo.set_user_role("root", "ADMIN")
    bob = connect(address).sign_in("bob")
    bob.reply_to(bob.request("CREATE_ROOM", {"room": "general"}))

    app = make_app(root, address, "root")
    app.login()
    assert pump(root, lambda: app.username == "root")
    assert app.status_var.get() == "Connected as root (administrator)"

    app.text_var.set("/admin rename_room general lobby")
    app.send_message()
    assert pump(root, lambda: "lobby" in app.rooms and "general" not in app.rooms)

    app.text_var.set("/admin deactivate bob")
    app.send_message()
    assert pump(root, lambda: "Done: deactivate bob" in chat_text(app))
    assert bob.next_event("account")["data"]["status"] == "DEACTIVATED"


def test_client_returns_to_login_when_deactivated(root, make_server, connect):
    server, address = make_server()
    first = connect(address)
    first.reply_to(first.request("REGISTER", {"username": "root", "password": "password-123"}))
    server.repo.set_user_role("root", "ADMIN")
    admin_client = connect(address).sign_in("root")

    app = make_app(root, address, "alice")
    app.register()
    assert pump(root, lambda: app.username == "alice")
    admin_client.reply_to(admin_client.request("ADMIN", {"action": "deactivate", "target": "alice"}))
    assert pump(root, lambda: app.login_frame.winfo_manager() == "pack")
    assert app.login_message.cget("text") == "Your account was deactivated by an administrator."
