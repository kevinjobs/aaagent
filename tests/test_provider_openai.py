import pytest

from aaagent_plugin_openai import OpenAICompatibleProvider


def test_api_key_from_env_var(monkeypatch):
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-from-env")
    p = OpenAICompatibleProvider(
        {
            "type": "openai_compatible",
            "api_key": "${TEST_OPENAI_KEY}",
            "base_url": "",
            "model": "x",
        }
    )
    assert p._api_key == "sk-from-env"


def test_api_key_literal():
    p = OpenAICompatibleProvider(
        {"api_key": "sk-literal", "model": "x"},
    )
    assert p._api_key == "sk-literal"


def test_missing_api_key_logs_error(monkeypatch, caplog):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    caplog.set_level("ERROR", logger="aaagent.provider.openai")
    OpenAICompatibleProvider(
        {"api_key": "${MISSING_VAR}", "model": "x"},
    )
    assert "api_key missing" in caplog.text


def test_model_default():
    p = OpenAICompatibleProvider({"api_key": "k"})
    assert p._model == "gpt-4o"


def test_base_url_none_when_empty():
    p = OpenAICompatibleProvider({"api_key": "k", "base_url": ""})
    assert p._base_url is None


def test_name_is_pulled_from_config_underscore_name():
    """Regression: the framework passes `config["_name"]` so the provider
    exposes a stable `name` attribute for the agent loop / rate limit /
    log records. Without this, `_acquire_provider_bucket` and
    `_chat_with_fallback` blow up with AttributeError at runtime.
    """
    p = OpenAICompatibleProvider({"api_key": "k", "_name": "primary"})
    assert p.name == "primary"

    # Plain callers (tests, ad-hoc scripts) that don't supply `_name`
    # get an empty string rather than AttributeError.
    p2 = OpenAICompatibleProvider({"api_key": "k"})
    assert p2.name == ""