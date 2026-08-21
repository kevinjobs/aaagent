from aaagent_plugin_feishu import (
    _build_control_frame,
    _parse_frame,
)


def _encode_varint(v):
    out = bytearray()
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def _encode_field_bytes(field, value):
    return _encode_varint((field << 3) | 2) + _encode_varint(len(value)) + value


def _encode_field_varint(field, value):
    return _encode_varint((field << 3) | 0) + _encode_varint(value)


def _encode_field_string(field, value):
    return _encode_field_bytes(field, value.encode("utf-8"))


def _encode_header(k, v):
    return _encode_field_string(1, k) + _encode_field_string(2, v)


def test_parse_user_log_head():
    """The exact 30-byte head from the user's bug report."""
    head = bytes.fromhex("08ceddf2b31710de94e6d180b0e2e61818f681801020012a1a0a0974696d")
    parsed = _parse_frame(head)
    assert parsed["seq_id"] == 6282849998
    assert parsed["log_id"] == 1787235810156317278
    assert parsed["service"] == 33554678
    assert parsed["method"] == 1
    assert parsed["payload"] is None


def test_parse_data_event_frame_roundtrip():
    import json

    envelope = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {"chat_id": "oc_1", "content": json.dumps({"text": "hi"})},
            "sender": {"sender_id": {"open_id": "ou_2"}},
        },
    }
    payload = json.dumps(envelope).encode("utf-8")

    frame = bytearray()
    frame += _encode_field_varint(1, 100)
    frame += _encode_field_varint(2, 200)
    frame += _encode_field_varint(3, 33554678)
    frame += _encode_field_varint(4, 1)
    frame += _encode_field_bytes(5, _encode_header("type", "event"))
    frame += _encode_field_bytes(5, _encode_header("message_id", "om_42"))
    frame += _encode_field_bytes(8, payload)
    frame = bytes(frame)

    parsed = _parse_frame(frame)
    assert parsed["method"] == 1
    assert parsed["service"] == 33554678
    assert parsed["headers"]["type"] == "event"
    assert parsed["headers"]["message_id"] == "om_42"
    assert parsed["payload"] == payload


def test_parse_control_frame_roundtrip():
    raw = _build_control_frame(service=33554678, msg_type="ping")
    parsed = _parse_frame(raw)
    assert parsed["method"] == 0
    assert parsed["headers"]["type"] == "ping"
    assert parsed["service"] == 33554678


def test_parse_pong_frame():
    raw = _build_control_frame(service=123, msg_type="pong")
    parsed = _parse_frame(raw)
    assert parsed["method"] == 0
    assert parsed["headers"]["type"] == "pong"
    assert parsed["service"] == 123


def test_parse_unknown_field_is_skipped():
    """Unknown fields should be skipped without breaking the parser."""
    frame = bytearray()
    frame += _encode_field_varint(99, 123)  # unknown field
    frame += _encode_field_varint(4, 1)  # method=1
    frame = bytes(frame)
    parsed = _parse_frame(frame)
    assert parsed["method"] == 1


def test_parse_truncated_varint_returns_none():
    # 10 bytes all with continuation bit set, no terminator
    bad = bytes.fromhex("ffffffffffffffffffff")
    assert _parse_frame(bad) is None


def test_parse_empty_returns_empty_dict():
    parsed = _parse_frame(b"")
    assert parsed == {
        "seq_id": 0,
        "log_id": 0,
        "service": 0,
        "method": 0,
        "headers": {},
        "payload_encoding": "",
        "payload_type": "",
        "payload": None,
    }


def test_repeated_header_last_wins():
    h1 = _encode_field_bytes(5, _encode_header("type", "event"))
    h2 = _encode_field_bytes(5, _encode_header("type", "card"))
    frame = _encode_field_varint(4, 1) + h1 + h2
    parsed = _parse_frame(frame)
    assert parsed["headers"]["type"] == "card"