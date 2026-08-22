# Changelog

## 0.3.1 - Unreleased

### Web tools
- New `aaagent-plugin-websearch`: provides `web_search` via a pluggable
  `Backend` ABC; ships with `TavilyBackend` (default), requires
  `TAVILY_API_KEY`. Returns a numbered `[i] title — url\nsnippet` list.
  Supports `top_k`, `recency_days`, `include_answer`.
- New `aaagent-plugin-webscrape`: provides `fetch_url` via httpx +
  trafilatura. Returns main content as `markdown` (default) / `text` /
  `html` with `timeout`, `max_bytes`, `max_chars` caps. JS-heavy pages
  fall back to title + first paragraph + link list.
- Both registered as workspace members; config snippets in `README.md`
  and `config.yaml`; `TAVILY_API_KEY` documented in `.env.example`.

### Memory & Retrieval
- `MemoryStore.recall` accepts an optional `tags` filter
- **markdownstore**: fact lines are parsed structurally and ranked by
  corpus-aware IDF-weighted token overlap + recency decay + tag bonus;
  `recall` honors `top_k` / `tags` (`memory.recall.*` weights configurable)
- **memorytools**: the `recall` tool forwards `top_k` (default 10) and
  `tags`; schema documents both
- Sessions idle longer than `memory.archive_after_hours` (default 24) are
  periodically archived to `archive.md` and dropped from the session store
  (`SessionStore.drop_session`), wiring the previously-dead
  `MemoryStore.archive_session` path
- Fix: `Application` never imported `time` (latent NameError in the tool loop)

### MCP
- New `aaagent-plugin-mcp`: bridges MCP servers (stdio / streamable HTTP)
  into the ToolRegistry with automatic `<server>_<tool>` expansion; lazy
  connections, failure isolation, `establish()`/`close()` lifecycle hooks
- `ToolPlugin` protocol gains optional async `establish(registry, config)`
  and `close()`; `Application.run()` establishes before adapters start and
  closes on shutdown
- Declared `[dependency-groups] dev` (pytest, pytest-asyncio, fastmcp) so
  `uv sync --all-packages` keeps the test harness installed

### Providers
- **Fallback providers**: `fallback_providers:` (ordered list of `providers:`
  keys) is consulted when the primary fails transiently (network, timeout,
  429 rate limit, 5xx, overloaded). Non-transient errors are raised
  immediately. Streaming swaps only before the first token is emitted.
- **Per-provider rate buckets**: `rate_limit.provider_rpm` now applies one
  token bucket per provider instead of a single shared bucket.
- Fix: `default_provider` selection block was unreachable dead code after
  `_instantiate_provider` refactor; `self._provider` was only ever assigned
  by `set_provider()`. Primary selection now happens in
  `_resolve_provider_chain()` and is always applied at startup.

## 0.3.0 - Unreleased

### Plugin Refactor (commits 1-9)
- **Core protocols**: `Provider`, `ToolPlugin`, `IMAdapter`,
  `SessionStoreFactory`, `MemoryStoreFactory` defined in
  `core/plugin.py`
- **PluginManager** discovers plugins via builtin registry, Python
  entry points (`aaagent.providers` / `aaagent.tools` /
  `aaagent.adapters` / `aaagent.sessions` / `aaagent.memories`), and
  config.yaml overrides; runtime validation raises
  `PluginNotFoundError` / `PluginValidationError`
- **envutil**: single `resolve_env()` for `${ENV_VAR}` placeholders
- All concrete implementations moved to plugin packages under
  `plugins/`: openai, filetools, shelltools, memorytools, cliadapter,
  feishu, inmemorysession, markdownstore (8 plugins, no wechat)
- uv workspace with `[tool.uv.sources]` for in-tree development
- `wechat` adapter removed (was a NotImplementedError stub)
- See `docs/plugin-authoring.md` for plugin author guide

## 0.2.0 - Unreleased

### Security & Data Integrity (Work Item A)
- **MemoryStore**: per-store `asyncio.Lock`; remember/archive use `aiofiles` for safe
  concurrent writes; `maybe_consolidate_profile` uses snapshot-release-LLM-reacquire-write
  to avoid blocking other requests while waiting for the LLM
- **shell_tools**: deny-list rewritten as `shlex`-tokenized rule table with explicit
  patterns (`rm -rf /`, `dd if=<abs>`, `mkfs.*`, redirect to `/dev/sd*`, `chmod 777 /`,
  `chown root:root /`, fork bomb); commands are normalized (Unicode NFKC + backslash
  strip + whitespace fold) before checking
- **file_tools**: `_ensure_allowed` now raises `PermissionError` when `allowed_dirs` is
  empty or `None`; logic simplified to use `relative_to` correctly
- **allowed_dirs**: entries pointing to non-existent paths are warned and skipped at
  startup; falls back to `cwd` if all entries are invalid
- **Sensitive fields**: `api_key` / `app_secret` / `token` / `Authorization: Bearer` are
  redacted in debug-level config-load logs

### Reliability (Work Item B)
- **EventBus**: handlers now run concurrently via `asyncio.gather`; per-handler
  exceptions are caught and logged so one bad handler can't break the bus
- **Message.to_llm_dict**: tool messages now include the `name` field (required by
  some non-OpenAI providers)
- **SessionStore**: `maybe_compress` merged into `add_message` so the lock covers
  both operations; the standalone `maybe_compress` is removed
- **Tool loop guard**: if accumulated message payload exceeds 200,000 chars the
  loop aborts with a user-facing fallback instead of letting context grow unbounded

### Testability & Architecture (Work Item C)
- **Application DI**: constructor accepts optional `bus`, `session_store`, `memory`,
  `tool_registry`, `providers`; provided dependencies skip default construction
- **Provider registry**: each Application uses an instance-level copy of
  `PROVIDER_TYPE_REGISTRY` for test isolation; the module-level registry is still
  the public API for third-party providers
- **FakeProvider**: reusable async LLM stub in `tests/conftest.py`
- **FeishuAdapter**: `_create_http` and `_connect_ws` extracted to overridable
  helpers; `health_check()` returns True if tenant token is valid and not expired
- **Application health loop**: background task calls each adapter's `health_check`
  every 60s and logs failures; `stop()` cancels it cleanly

### Observability & UX (Work Item D)
- **core/logctx**: `ContextFilter` injects `session_id` / `platform` / `chat_id` from
  `contextvars` into every log record; `set_context` / `reset_context` use tokens
- **cli.py**: attaches the ContextFilter to all log handlers and includes the new
  fields in the format string; catches non-KeyboardInterrupt exceptions and exits
  with code 1
- **Tool timing**: `tool_result` event payload includes `duration_ms`
- **LLM streaming**: `LLMProvider.stream_chat` async generator with OpenAI
  implementation; Application prefers streaming when no tools are configured;
  CliAdapter prints tokens inline
- **core/prompt.PromptBuilder**: central context assembly so future context
  fragments can be added in one place
- **core/ratelimit.TokenBucket**: throttles provider calls when `rate_limit.provider_rpm`
  is configured
- **MemoryStore.data_dir**: relative paths resolve against the directory containing
  `config.yaml` for predictable persistence

### Documentation & Polish (Work Item E)
- **Tests**: new `tests/test_app.py` (Application main flow + length guard),
  `tests/test_work_item_d.py` (ContextFilter / TokenBucket / concurrent bus);
  total 99 passing tests
- **README**: expanded config reference, custom provider example, troubleshooting
  section, project structure with new modules
- **.env.example**: comments for each API key and `FEISHU_DEBUG` toggle
- **pyproject.toml**: `[tool.ruff]` and `[tool.mypy]` configuration
- **Feishu debug toggle**: `FEISHU_DEBUG=1` enables verbose frame logging
- **CliAdapter.send**: now delegates to the `message_to_send` bus event so all
  outgoing assistant messages flow through one handler
- **cli.py**: top-level exceptions in `_run_application` are logged and
  `SystemExit(1)` is raised; `KeyboardInterrupt` still exits silently

## 0.1.0 - 2026-08-21

- Initial release