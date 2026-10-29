from bot import alerts as A


def test_disabled_without_token_still_records(monkeypatch):
    a = A.Alerts(token="", chat_id="")
    called = []
    monkeypatch.setattr(A.requests, "post", lambda *x, **k: called.append(1))
    a.send("hello")
    assert a.sent == ["hello"] and not called and a.poll_commands() == []


def test_send_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(A.requests, "post", boom)
    A.Alerts(token="t", chat_id="1").send("x", blocking=True)


def test_commands_only_from_configured_chat(monkeypatch):
    updates = {"result": [
        {"update_id": 1, "message": {"chat": {"id": 1}, "text": "/stop"}},
        {"update_id": 2, "message": {"chat": {"id": 999}, "text": "/stop"}},
        {"update_id": 3, "message": {"chat": {"id": 1}, "text": "/Status@mybot now"}},
        {"update_id": 4, "message": {"chat": {"id": 1}, "text": "hello"}}]}

    class R:
        def json(self):
            return updates
    monkeypatch.setattr(A.requests, "get", lambda *a, **k: R())
    a = A.Alerts(token="t", chat_id="1")
    assert a.poll_commands() == ["/stop", "/status"] and a.offset == 5
