from __future__ import annotations

from pathlib import Path

from aaagent.core.paths import resolve_all_paths, resolve_project_path


def test_absolute_path_unchanged(tmp_path):
    p = tmp_path / "x"
    p.mkdir()
    assert resolve_project_path(str(p), tmp_path) == p.resolve()


def test_relative_path_anchored_to_project_root(tmp_path):
    out = resolve_project_path("data/sub", tmp_path)
    assert out == (tmp_path / "data" / "sub").resolve()


def test_dot_relative_resolves_against_project_root(tmp_path):
    out = resolve_project_path("./foo", tmp_path)
    assert out == (tmp_path / "foo").resolve()


def test_double_dot_normalised(tmp_path):
    out = resolve_project_path("../escape", tmp_path)
    # ../escape from tmp_path lands in tmp_path.parent — but the
    # resolved path is what `Path.resolve()` produces; we just assert
    # the result is absolute and ends in 'escape'.
    assert out.is_absolute()
    assert out.name == "escape"


def test_tilde_expansion(tmp_path, monkeypatch):
    # Path.expanduser() consults HOME on POSIX and USERPROFILE on
    # Windows. Set both so the test passes regardless of platform.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    out = resolve_project_path("~/x", tmp_path)
    assert out == (tmp_path / "x").resolve()


def test_resolve_all_paths_walks_known_scalar_keys(tmp_path):
    cfg = {
        "paths": {"dotenv": ".env"},
        "memory": {"data_dir": "data/mem"},
    }
    resolve_all_paths(cfg, tmp_path)
    assert cfg["paths"]["dotenv"] == str((tmp_path / ".env").resolve())
    assert cfg["memory"]["data_dir"] == str((tmp_path / "data" / "mem").resolve())


def test_resolve_all_paths_walks_known_list_keys(tmp_path):
    cfg = {
        "tools": {"allowed_dirs": [".", "data"]},
        "limits": {"protected_paths": ["config.yaml", ".env"]},
    }
    resolve_all_paths(cfg, tmp_path)
    assert cfg["tools"]["allowed_dirs"][0] == str(tmp_path.resolve())
    assert cfg["tools"]["allowed_dirs"][1] == str((tmp_path / "data").resolve())
    assert cfg["limits"]["protected_paths"][0] == str(
        (tmp_path / "config.yaml").resolve()
    )
    assert cfg["limits"]["protected_paths"][1] == str((tmp_path / ".env").resolve())


def test_resolve_all_paths_leaves_unknown_keys_alone(tmp_path):
    cfg = {
        "providers": {"minmax": {"api_key": "${MINMAX_API_KEY}"}},
        "log_level": "INFO",
    }
    resolve_all_paths(cfg, tmp_path)
    # api_key is an env var ref — must NOT be resolved as a path
    assert cfg["providers"]["minmax"]["api_key"] == "${MINMAX_API_KEY}"
    assert cfg["log_level"] == "INFO"


def test_resolve_all_paths_handles_missing_keys(tmp_path):
    cfg: dict = {}
    resolve_all_paths(cfg, tmp_path)
    assert cfg == {}


def test_resolve_all_paths_keeps_non_string_list_entries(tmp_path):
    cfg = {"tools": {"allowed_dirs": [123, "data"]}}
    resolve_all_paths(cfg, tmp_path)
    assert cfg["tools"]["allowed_dirs"][0] == 123
    assert cfg["tools"]["allowed_dirs"][1] == str((tmp_path / "data").resolve())


def test_application_default_allowed_dirs_is_project_root(tmp_path):
    """End-to-end: building an Application with no tools.allowed_dirs
    must default to [project_root], not [cwd]."""
    from aaagent.core.app import Application
    from aaagent.core.bus import EventBus

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "providers:\n"
        "  _meta:\n"
        "    default: x\n"
        "  x:\n"
        "    type: custom\n"
        "    class: aaagent.testing.FakeProvider\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    bus = EventBus()
    app = Application(config_path=str(cfg), bus=bus, providers={}, tool_registry=None)
    assert app._project_root == cfg.parent.resolve()
    assert app._tool_registry.allowed_dirs == [str(cfg.parent.resolve())]
    # paths.dotenv default lands next to config.yaml
    assert app._dotenv.path == (cfg.parent / ".env").resolve()
