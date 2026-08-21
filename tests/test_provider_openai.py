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