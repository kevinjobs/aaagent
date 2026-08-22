import pytest

from aaagent.core.app import _strip_think


def test_strip_paired_think():
    assert _strip_think("<think>reasoning</think>\n\nHello!") == "Hello!"


def test_strip_paired_thinking_tag():
    assert _strip_think("<thinking>deep thought</thinking>final") == "final"


def test_strip_multiline_think():
    s = "<think>\nstep1\nstep2\n</think>\n**bold answer**"
    assert _strip_think(s) == "**bold answer**"


def test_strip_unclosed_think():
    assert _strip_think("<think>reasoning left dangling") == ""


def test_strip_unclosed_thinking():
    assert _strip_think("Hello <think>unfinished") == "Hello"


def test_no_think_unchanged():
    assert _strip_think("Just plain text") == "Just plain text"


def test_collapses_excess_newlines():
    s = "<think>plan</think>\n\nline1\n\n\n\nline2"
    assert _strip_think(s) == "line1\n\nline2"


def test_strip_with_prefix_and_suffix():
    assert _strip_think("prefix<think>mid</think>suffix") == "prefixsuffix"


def test_empty_input():
    assert _strip_think("") == ""


def test_none_input():
    assert _strip_think(None) is None  # type: ignore[arg-type]


def test_only_whitespace_after_strip():
    assert _strip_think("<think>plan</think>\n\n  ") == ""


def test_case_insensitive():
    s = "<THINK>reasoning</THINK>\nanswer"
    assert _strip_think(s) == "answer"