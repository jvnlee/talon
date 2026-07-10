from datetime import timedelta

import httpx

from talon.notify.telegram import Alerter, TelegramNotifier


def make_notifier(handler, token="tok", chat_id="123"):
    return TelegramNotifier(token, chat_id, transport=httpx.MockTransport(handler))


def test_send_success():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["json"] = request.read().decode()
        return httpx.Response(200, json={"ok": True, "result": {}})

    notifier = make_notifier(handler)
    assert notifier.send("안녕")
    assert seen["path"] == "/bottok/sendMessage"
    assert "123" in seen["json"]


def test_send_unconfigured_returns_false():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    notifier = make_notifier(handler, token="", chat_id="")
    assert not notifier.send("x")


def test_send_api_error_returns_false():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "chat not found"})

    notifier = make_notifier(handler)
    assert not notifier.send("x")


def test_send_transport_error_swallowed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    notifier = make_notifier(handler)
    assert not notifier.send("x")


def test_alerter_cooldown(state):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.read().decode())
        return httpx.Response(200, json={"ok": True, "result": {}})

    notifier = make_notifier(handler)
    alerter = Alerter(notifier, state, timedelta(hours=1))
    assert alerter.alert("key", "첫 알림")
    assert not alerter.alert("key", "중복 알림")
    assert alerter.alert("other", "다른 키")
    assert len(sent) == 2
    assert "[talon]" in sent[0]


def test_alerter_failed_send_not_marked(state):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False})

    notifier = make_notifier(handler)
    alerter = Alerter(notifier, state, timedelta(hours=1))
    assert not alerter.alert("key", "실패")
    assert state.should_alert("key", timedelta(hours=1))


def test_list_chats():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"message": {"chat": {"id": 42, "username": "hyunjun"}}},
                    {"message": {"chat": {"id": 42, "username": "hyunjun"}}},
                ],
            },
        )

    notifier = make_notifier(handler, chat_id="")
    assert notifier.list_chats() == [(42, "hyunjun")]
