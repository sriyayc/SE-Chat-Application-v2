
"""Issue #5: real-time text messaging (CHAT-F-007, CHAT-F-008, CHAT-F-010, CHAT-NF-003; TC-MSG-01/02/04, TC-REL-01)."""
import queue
import threading
import time

from client.net.connection import ChatConnection


def send(client, to, text):
    return client.request("MSG", {"to": to, "text": text})


def test_message_reaches_recipient_and_sender_gets_ack(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    seq = send(alice, "bob", "hi bob")

    event = bob.recv()
    assert event["type"] == "EVENT" and event["kind"] == "msg"
    assert event["data"]["from"] == "alice"
    assert event["data"]["to"] == "bob"
    assert event["data"]["text"] == "hi bob"
    assert event["data"]["seq"] == seq

    ack = alice.reply_to(seq)
    assert ack["type"] == "ACK" and ack["status"] == "DELIVERED"
    assert ack["data"]["to"] == "bob"


def test_reply_in_both_directions(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    send(alice, "bob", "ping?")
    assert bob.recv()["data"]["text"] == "ping?"
    send(bob, "alice", "pong!")
    received = [alice.recv() for _ in range(2)]
    # ACK for Alice's message and Bob's reply can arrive in either order.
    assert any(
        m.get("kind") == "msg" and m["data"]["text"] == "pong!"
        for m in received
    )


def test_recipient_name_is_case_insensitive(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("Bob")
    seq = send(alice, "BOB", "hello")
    assert bob.recv()["data"]["to"] == "Bob"
    assert alice.reply_to(seq)["status"] == "DELIVERED"


def test_messages_arrive_in_send_order(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    for i in range(200):
        send(alice, "bob", f"message {i}")

    received = [bob.recv() for _ in range(200)]
    assert [m["data"]["text"] for m in received] == [
        f"message {i}" for i in range(200)
    ]
    seqs = [m["data"]["seq"] for m in received]
    assert seqs == sorted(seqs)


def test_order_is_kept_per_sender_with_concurrent_senders(connect):
    bob = connect().sign_in("bob")
    senders = [connect().sign_in(f"user{i}") for i in range(4)]

    def blast(client):
        for n in range(50):
            send(client, "bob", str(n))

    threads = [
        threading.Thread(target=blast, args=(client,))
        for client in senders
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    by_sender = {}
    for _ in range(200):
        data = bob.recv()["data"]
        by_sender.setdefault(data["from"], []).append(int(data["text"]))

    assert all(numbers == list(range(50)) for numbers in by_sender.values())
    assert len(by_sender) == 4


def test_offline_recipient_gets_not_delivered_status(connect):
    alice = connect().sign_in("alice")
    connect().sign_in("bob").close()
    time.sleep(0.2)

    ack = alice.reply_to(send(alice, "bob", "are you there?"))
    assert ack["type"] == "ACK" and ack["status"] == "NOT_DELIVERED"
    assert ack["data"]["reason"] == "bob is offline"


def test_unknown_recipient_is_not_found(connect):
    alice = connect().sign_in("alice")
    reply = alice.reply_to(send(alice, "ghost", "boo"))
    assert reply["type"] == "ERROR" and reply["code"] == "NOT_FOUND"


def test_cannot_message_yourself(connect):
    alice = connect().sign_in("alice")
    assert alice.reply_to(send(alice, "Alice", "me"))["code"] == "INVALID_MESSAGE"


def test_room_message_needs_membership(connect):
    alice = connect().sign_in("alice")
    reply = alice.reply_to(
        alice.request("MSG", {"room": "general", "text": "hi"})
    )
    assert reply["code"] == "NOT_FOUND"


def test_must_be_logged_in_to_send(connect):
    connect().sign_in("bob")
    anonymous = connect()
    assert anonymous.reply_to(send(anonymous, "bob", "hi"))["code"] == "SESSION_EXPIRED"


def test_invalid_message_is_rejected_and_others_unaffected(connect):
    alice, bob, carol = (
        connect().sign_in("alice"),
        connect().sign_in("bob"),
        connect().sign_in("carol"),
    )
    bad = alice.reply_to(send(alice, "bob", "x" * 2001))
    assert bad["code"] == "INVALID_MESSAGE"

    carol.sock.sendall(b"\x00\x00\x00\x05{bad}")
    carol.close()

    seq = send(alice, "bob", "still working")
    assert bob.recv()["data"]["text"] == "still working"
    assert alice.reply_to(seq)["status"] == "DELIVERED"


def next_non_notification(inbox):
    """Next item from a client inbox, skipping presence and room notifications."""
    while True:
        item = inbox.get(timeout=5)
        if item[0] != "message" or item[1].get("kind") not in ("presence", "room", "join", "leave"):
            return item


def test_two_client_network_handlers_chat(server_address):
    inbox_a, inbox_b = queue.Queue(), queue.Queue()
    a, b = ChatConnection(inbox_a), ChatConnection(inbox_b)

    try:
        for conn, inbox, name in (
            (a, inbox_a, "alice"),
            (b, inbox_b, "bob"),
        ):
            conn.connect(*server_address)
            conn.request(
                "REGISTER",
                {"username": name, "password": "password-123"},
            )

            register_event = inbox.get(timeout=5)
            assert register_event[0] == "message"
            assert register_event[1]["status"] == "REGISTERED"

            conn.request(
                "LOGIN",
                {"username": name, "password": "password-123"},
            )

            login_event = inbox.get(timeout=15)
            assert login_event[0] == "message"
            assert login_event[1]["status"] == "LOGGED_IN"

        a.request(
            "MSG",
            {"to": "bob", "text": "hello from a real client"},
        )

        _, ack, request_type = next_non_notification(inbox_a)
        assert (request_type, ack["status"]) == ("MSG", "DELIVERED")

        _, event, _ = next_non_notification(inbox_b)
        assert event["data"]["text"] == "hello from a real client"

    finally:
        a.close()
        b.close()
