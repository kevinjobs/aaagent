import pytest

from aaagent.core.message import Message


def test_default_values():
    m = Message()
    assert m.session_id == ""
    assert m.platform == ""
    assert m.role == "user"
    assert m.content == ""
    assert m.id  # auto-generated
    assert m.timestamp > 0


def test_to_llm_dict():
    m = Message(role="assistant", content="hi")
    assert m.to_llm_dict() == {"role": "assistant", "content": "hi"}


def test_unique_ids():
    a = Message()
    b = Message()
    assert a.id != b.id


def test_explicit_fields():
    m = Message(
        session_id="s1",
        platform="feishu",
        chat_id="oc_x",
        user_id="ou_y",
        content="hello",
        role="user",
    )
    assert m.session_id == "s1"
    assert m.platform == "feishu"
    assert m.to_llm_dict() == {"role": "user", "content": "hello"}