"""Issue #9: persistent chat history (CHAT-F-011, CHAT-F-012, CHAT-NF-006, CHAT-SR-003;
TC-HIST-01, TC-HIST-02, TC-DATA-01)."""
import sqlite3

import pytest


def dm(client, to, text):
    return client.reply_to(client.request("MSG", {"to": to, "text": text}))


def history(client, **payload):
    return client.reply_to(client.request("HISTORY", payload))


def texts(reply):
    return [m["text"] for m in reply["data"]["messages"]]


def test_direct_history_both_directions(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    dm(alice, "bob", "one")
    dm(bob, "alice", "two")
    dm(alice, "bob", "three")

    reply = history(alice, **{"with": "BOB"})
    assert reply["type"] == "ACK" and reply["status"] == "HISTORY"
    assert reply["data"]["with"] == "bob"
    assert texts(reply) == ["one", "two", "three"]
    first = reply["data"]["messages"][0]
    assert first["from"] == "alice" and first["to"] == "bob"
    assert isinstance(first["id"], int) and isinstance(first["ts"], float)
    assert texts(history(bob, **{"with": "alice"})) == ["one", "two", "three"]


def test_message_to_offline_user_is_stored(connect):
    alice = connect().sign_in("alice")
    connect().sign_in("bob").close()
    while alice.next_event("presence")["data"]["status"] != "offline":
        pass
    ack = dm(alice, "bob", "read this later")
    assert ack["status"] == "STORED_NOT_DELIVERED"

    bob = connect().sign_in("bob")
    assert texts(history(bob, **{"with": "alice"})) == ["read this later"]


def test_room_history_for_members(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    alice.reply_to(alice.request("CREATE_ROOM", {"room": "general"}))
    alice.reply_to(alice.request("MSG", {"room": "general", "text": "before bob joined"}))
    bob.reply_to(bob.request("JOIN", {"room": "general"}))
    bob.reply_to(bob.request("MSG", {"room": "general", "text": "hi all"}))

    reply = history(bob, room="general")
    assert reply["data"]["room"] == "general"
    assert texts(reply) == ["before bob joined", "hi all"]
    assert [m["from"] for m in reply["data"]["messages"]] == ["alice", "bob"]


def test_non_members_cannot_read_room_history(connect):
    alice, eve = connect().sign_in("alice"), connect().sign_in("eve")
    alice.reply_to(alice.request("CREATE_ROOM", {"room": "secret"}))
    alice.reply_to(alice.request("MSG", {"room": "secret", "text": "private"}))
    reply = history(eve, room="secret")
    assert reply["type"] == "ERROR" and reply["code"] == "FORBIDDEN"
    assert "private" not in str(reply)


def test_leaving_a_room_removes_access_to_its_history(connect):
    alice = connect().sign_in("alice")
    alice.reply_to(alice.request("CREATE_ROOM", {"room": "general"}))
    alice.reply_to(alice.request("LEAVE", {"room": "general"}))
    assert history(alice, room="general")["code"] == "FORBIDDEN"


def test_third_party_cannot_read_a_direct_conversation(connect):
    alice, bob, eve = (connect().sign_in(n) for n in ("alice", "bob", "eve"))
    dm(alice, "bob", "between us")
    # Eve can only ever ask for her own conversations, so she sees nothing of Alice and Bob's.
    assert texts(history(eve, **{"with": "alice"})) == []
    assert texts(history(eve, **{"with": "bob"})) == []


def test_history_errors(connect):
    alice = connect().sign_in("alice")
    assert history(alice, **{"with": "ghost"})["code"] == "NOT_FOUND"
    assert history(alice, **{"with": "alice"})["code"] == "INVALID_MESSAGE"
    assert history(alice, room="nowhere")["code"] == "NOT_FOUND"
    assert history(alice, room="general", limit=0)["code"] == "INVALID_MESSAGE"
    anonymous = connect()
    assert history(anonymous, room="general")["code"] == "SESSION_EXPIRED"


def test_paging_with_limit_and_before_ts(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    for i in range(7):
        dm(alice, "bob", str(i))

    newest = history(alice, limit=3, **{"with": "bob"})
    assert texts(newest) == ["4", "5", "6"]
    assert newest["data"]["more"] is True
    older = history(alice, limit=3, before_ts=newest["data"]["messages"][0]["ts"], **{"with": "bob"})
    assert texts(older) == ["1", "2", "3"]
    oldest = history(alice, limit=3, before_ts=older["data"]["messages"][0]["ts"], **{"with": "bob"})
    assert texts(oldest) == ["0"]
    assert oldest["data"]["more"] is False


def test_history_survives_a_server_restart(make_server, connect):
    server, address = make_server()
    alice, bob = connect(address).sign_in("alice"), connect(address).sign_in("bob")
    alice.reply_to(alice.request("CREATE_ROOM", {"room": "general"}))
    bob.reply_to(bob.request("JOIN", {"room": "general"}))
    alice.reply_to(alice.request("MSG", {"room": "general", "text": "remember me"}))
    dm(bob, "alice", "and me")
    alice.close()
    bob.close()
    server.stop()

    _, address = make_server()  # same database file
    alice = connect(address)
    reply = alice.reply_to(alice.request("LOGIN", {"username": "alice", "password": "password-123"}))
    alice.session = reply["data"]["session"]
    assert texts(history(alice, room="general")) == ["remember me"]
    assert texts(history(alice, **{"with": "bob"})) == ["and me"]


def test_stored_messages_keep_referential_integrity(make_server, connect):
    server, address = make_server()
    alice, bob = connect(address).sign_in("alice"), connect(address).sign_in("bob")
    alice.reply_to(alice.request("CREATE_ROOM", {"room": "general"}))
    alice.reply_to(alice.request("MSG", {"room": "general", "text": "room"}))
    dm(alice, "bob", "direct")
    server.repo.flush()
    with sqlite3.connect(server.config["db_path"]) as db:
        db.execute("PRAGMA foreign_keys = ON")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        rows = db.execute("SELECT RoomID IS NULL, RecipientID IS NULL FROM Message").fetchall()
        assert sorted(rows) == [(0, 1), (1, 0)]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO Message (SenderID, RoomID, RecipientID, Content, SenderSeq, Timestamp) "
                       "VALUES (1, 1, 2, 'both targets', 1, 0)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO Message (SenderID, RecipientID, Content, SenderSeq, Timestamp) "
                       "VALUES (999, 1, 'unknown sender', 1, 0)")


def test_failed_write_does_not_stop_the_server(make_server, connect):
    server, address = make_server()
    alice, bob = connect(address).sign_in("alice"), connect(address).sign_in("bob")
    server.repo.save_message(1, "neither room nor recipient", 1, 0.0)  # violates the CHECK constraint
    server.repo.flush()
    assert dm(alice, "bob", "still saving")["status"] == "DELIVERED"
    assert texts(history(alice, **{"with": "bob"})) == ["still saving"]
