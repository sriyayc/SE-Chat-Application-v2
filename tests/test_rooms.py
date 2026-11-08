"""Issue #8: chat rooms and online presence (CHAT-F-004, F-005, F-006, F-013, CHAT-SR-003;
TC-USER-01, TC-ROOM-01, TC-ROOM-02, TC-NOTIF-01)."""


def create(client, room):
    return client.reply_to(client.request("CREATE_ROOM", {"room": room}))


def join(client, room):
    return client.reply_to(client.request("JOIN", {"room": room}))


def leave(client, room):
    return client.reply_to(client.request("LEAVE", {"room": room}))


def say(client, room, text):
    return client.request("MSG", {"room": room, "text": text})


# --- rooms ---

def test_create_room_makes_creator_a_member(connect):
    alice = connect().sign_in("alice")
    reply = create(alice, "general")
    assert reply["type"] == "ACK" and reply["status"] == "CREATED"
    assert reply["data"] == {"room": "general", "members": ["alice"]}


def test_duplicate_room_names_rejected_case_insensitively(connect):
    alice = connect().sign_in("alice")
    create(alice, "general")
    for name in ("general", "GENERAL"):
        reply = create(alice, name)
        assert reply["type"] == "ERROR" and reply["code"] == "DUPLICATE"


def test_room_limit_is_enforced(make_server, connect):
    _, address = make_server(max_rooms=2)
    alice = connect(address).sign_in("alice")
    assert create(alice, "one")["status"] == "CREATED"
    assert create(alice, "two")["status"] == "CREATED"
    assert create(alice, "three")["code"] == "FORBIDDEN"


def test_join_and_leave(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    create(alice, "general")
    reply = join(bob, "General")
    assert reply["status"] == "JOINED"
    assert reply["data"] == {"room": "general", "members": ["alice", "bob"]}
    assert join(bob, "general")["status"] == "JOINED"  # joining twice is harmless
    assert leave(bob, "general")["status"] == "LEFT"
    assert leave(bob, "general")["code"] == "NOT_FOUND"


def test_unknown_room_is_not_found(connect):
    alice = connect().sign_in("alice")
    assert join(alice, "nowhere")["code"] == "NOT_FOUND"
    assert leave(alice, "nowhere")["code"] == "NOT_FOUND"


def test_room_names_are_validated(connect):
    alice = connect().sign_in("alice")
    for bad in ("", " padded", "x" * 33, "semi;colon"):
        assert create(alice, bad)["code"] == "INVALID_MESSAGE"


def test_rooms_need_login(connect):
    anonymous = connect()
    assert create(anonymous, "general")["code"] == "SESSION_EXPIRED"


def test_membership_survives_logging_in_again(make_server, connect):
    _, address = make_server()
    alice = connect(address).sign_in("alice")
    create(alice, "general")
    alice.reply_to(alice.request("LOGOUT"))
    again = connect(address)
    reply = again.reply_to(again.request("LOGIN", {"username": "alice", "password": "password-123"}))
    assert reply["data"]["rooms"] == [{"name": "general", "status": "OPEN", "members": 1, "joined": True}]


# --- room messages ---

def test_room_message_reaches_online_members_only(connect):
    alice, bob, carol = (connect().sign_in(n) for n in ("alice", "bob", "carol"))
    create(alice, "general")
    join(bob, "general")
    seq = say(alice, "general", "hello room")

    event = bob.recv()
    assert event["kind"] == "msg"
    assert event["data"]["from"] == "alice"
    assert event["data"]["room"] == "general"
    assert event["data"]["text"] == "hello room"
    ack = alice.reply_to(seq)
    assert ack["status"] == "DELIVERED" and ack["data"]["delivered_to"] == 1

    # Carol is not a member: she gets nothing, and the next thing she reads is her own PONG.
    ping = carol.request("PING")
    assert carol.recv() == {"type": "PONG", "ref_seq": ping}


def test_non_member_cannot_post(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    create(alice, "general")
    reply = bob.reply_to(say(bob, "general", "let me in"))
    assert reply["type"] == "ERROR" and reply["code"] == "FORBIDDEN"


def test_room_message_with_no_one_else_online(connect):
    alice = connect().sign_in("alice")
    create(alice, "general")
    ack = alice.reply_to(say(alice, "general", "anyone?"))
    assert ack["status"] == "NOT_DELIVERED"
    assert ack["data"]["reason"] == "no one else in general is online"


def test_room_messages_keep_send_order(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    create(alice, "general")
    join(bob, "general")
    for i in range(50):
        say(alice, "general", str(i))
    assert [int(bob.recv()["data"]["text"]) for _ in range(50)] == list(range(50))


def test_restricted_room_is_read_only_and_closed(make_server, connect):
    server, address = make_server()
    alice, bob = connect(address).sign_in("alice"), connect(address).sign_in("bob")
    create(alice, "general")
    with server.repo._lock:
        server.repo._db.execute("UPDATE ChatRoom SET Status = 'RESTRICTED' WHERE RoomName = 'general'")
    assert alice.reply_to(say(alice, "general", "hi"))["code"] == "FORBIDDEN"
    assert join(bob, "general")["code"] == "FORBIDDEN"


# --- notifications and presence ---

def test_join_and_leave_notifications(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    create(alice, "general")
    join(bob, "general")
    assert alice.next_event("join")["data"] == {"room": "general", "user": "bob"}
    leave(bob, "general")
    assert alice.next_event("leave")["data"] == {"room": "general", "user": "bob"}


def test_new_room_is_announced(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    create(alice, "general")
    assert bob.next_event("room")["data"] == {"action": "created", "room": "general"}


def test_login_reply_lists_online_users_and_rooms(connect):
    alice = connect().sign_in("alice")
    create(alice, "general")
    bob = connect()
    bob.reply_to(bob.request("REGISTER", {"username": "bob", "password": "password-123"}))
    reply = bob.reply_to(bob.request("LOGIN", {"username": "bob", "password": "password-123"}))
    assert reply["data"]["online"] == ["alice", "bob"]
    assert reply["data"]["rooms"] == [{"name": "general", "status": "OPEN", "members": 1, "joined": False}]


def test_presence_updates_when_users_come_and_go(connect):
    alice = connect().sign_in("alice")
    bob = connect().sign_in("bob")
    assert alice.next_event("presence")["data"] == {"user": "bob", "status": "online"}
    bob.close()
    assert alice.next_event("presence")["data"] == {"user": "bob", "status": "offline"}


def test_logout_announces_offline(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    alice.next_event("presence")
    bob.reply_to(bob.request("LOGOUT"))
    assert alice.next_event("presence")["data"] == {"user": "bob", "status": "offline"}


def test_disconnected_member_is_skipped(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    create(alice, "general")
    join(bob, "general")
    bob.close()
    assert alice.next_event("presence")["data"]["status"] == "offline"
    assert alice.reply_to(say(alice, "general", "still there?"))["status"] == "NOT_DELIVERED"
