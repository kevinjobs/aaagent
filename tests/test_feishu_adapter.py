from aaagent.adapters.feishu import FeishuAdapter, _resolve_env
from aaagent.core.bus import EventBus


def test_resolve_env_passthrough():
    assert _resolve_env("plain") == "plain"


def test_resolve_env_empty():
    assert _resolve_env("") == ""


def test_resolve_env_substitution(monkeypatch):
    monkeypatch.setenv("MY_FEISHU_KEY", "secret123")
    assert _resolve_env("${MY_FEISHU_KEY}") == "secret123"


def test_resolve_env_missing(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    assert _resolve_env("${NOPE}") == ""


def test_feishu_config_warning_when_missing(caplog):
    import logging

    bus = EventBus()
    with caplog.at_level(logging.WARNING):
        FeishuAdapter({"app_id": "", "app_secret": ""}, bus)
    assert any("Feishu adapter misconfigured" in r.getMessage() for r in caplog.records)


def test_feishu_remember_message_dedup():
    bus = EventBus()
    adapter = FeishuAdapter(
        {"app_id": "x", "app_secret": "y"}, bus
    )  # missing config but we won't call start
    assert adapter._remember_message("om_1") is True
    assert adapter._remember_message("om_1") is False
    assert adapter._remember_message("om_2") is True


def test_feishu_remember_message_empty_id():
    bus = EventBus()
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, bus)
    assert adapter._remember_message("") is True
    assert adapter._remember_message("") is True  # empty always allowed


def test_feishu_truncate_long_message():
    bus = EventBus()
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, bus)
    long = "x" * 5000
    truncated = adapter._truncate_for_feishu(long)
    assert len(truncated) == 4000


def test_feishu_truncate_short_message_unchanged():
    bus = EventBus()
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, bus)
    short = "hello"
    assert adapter._truncate_for_feishu(short) == short