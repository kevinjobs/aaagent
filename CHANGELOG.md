# Changelog

## 0.4.1 - Unreleased

### `config.yaml` is now machine-local

`config.yaml` (and its runtime `.bak` / `.tmp` backups) is
**gitignored** — it holds machine-specific absolute paths and is not
meant to be shared. The version-controlled template is
`config.yaml.example`.

- **`config.yaml.example`**: sanitised copy of the previous default
  config. Absolute `D:\...` paths replaced with relative defaults
  (`paths.dotenv: .env`, `memory.data_dir: data/memories`,
  `tools.allowed_dirs: [.]`, `protected_paths` as globs). `base_url`
  values are public provider endpoints and are kept as-is.
- **First-run bootstrap**: when `config.yaml` is missing, `Application`
  auto-copies `config.yaml.example` to `config.yaml` and logs a warning,
  so `aaagent run` works out of the box on a fresh clone.
- **History rewrite**: `config.yaml` and `config.yaml.bak` were removed
  from git history (filter scripts), not just `HEAD`.

## 0.4.0 - Unreleased

### Capability limits (security)

A configurable guard layer that keeps the LLM from running away or
touching the wrong files. Every cap below has a safe built-in
default; tune them under `limits:` in `config.yaml`.

- **`limits.max_tool_turns`** (default `10`, was a 20-module-constant)
  — number of agent turns before `_run_tool_loop` aborts with
  "已达到最大工具调用次数".
- **`limits.max_tool_chars`** (default `200000`) — cumulative size of
  the `messages` list passed to the LLM. Aborts before context-window
  overflow.
- **`limits.max_tool_wallclock_s`** (default `120`, NEW) — outer
  wall-clock fence around a single `_handle_message`. The previous
  code only counted turns; a stuck loop could still hang forever if
  each turn produced tool calls in a reasonable time. A timeout
  surfaces as "工具循环超时（Ns），已中止。请简化请求或调整
  limits.max_tool_wallclock_s。"
- **`limits.provider_rpm`** (default `30`, was `0` = unlimited) —
  per-provider token bucket. The previous default was
  "unbounded", which combined badly with OpenAI SDK's own retry
  loop on 429. The legacy `rate_limit.provider_rpm` key is still
  read for backward-compat.
- **`limits.provider_persistence`** (`disk` | `memory`, default
  `disk`) — when `memory`, providers added/updated via `/model`
  stay in-process only and are not written back to `config.yaml`.
  Useful for testing a temporary provider.

### Breaking change — `/model --key` now writes to `.env`, not `config.yaml`

The previous `/model -new --key K` flow embedded the raw API key
into `config.yaml` (where it could leak via VCS). New behaviour:

- `DotenvStore.set(<provider>_API_KEY, K)` writes the key to
  `.env` (atomic, comment-preserving, idempotent).
- `config.yaml` is rewritten to hold `api_key: "${<provider>_API_KEY}"`
  — a `${ENV_VAR}` reference, not the raw key.
- `os.environ[<provider>_API_KEY]` is updated immediately so the
  next provider instantiation picks up the new value without a
  restart.
- `audit log` records the env-var name and the set/overwrite/noop
  result; the raw key is never logged.
- **D2 warn before overwrite**: when `DotenvStore.set` returns
  `"overwrite"` AND other providers in `config.yaml` reference the
  same `${ENV_VAR}`, the reply appends a warning listing the
  affected provider names. (Audit log records the env-var name and
  the set of shared providers — the raw key is never logged.)

Legacy provider entries with a raw `api_key: sk-…` in
`config.yaml` are preserved untouched until the operator
explicitly re-runs `/model --provider X --key K`, which migrates
them in place.

### Protected paths

`limits.protected_paths` is a glob list (default: `config.yaml`,
`config.yaml.bak`, `.env`, `.env.*`, `*.pem`, `*.key`,
`**/id_rsa*`). Every agent filesystem write — through `write_file`
AND through `run_shell`'s redirection targets (`>`, `>>`, `<`,
`2>`, `&>`) — runs those paths through `is_protected_target()`
and is refused if a match is found. Closes the
`cat foo > config.yaml` bypass of the file plugin's path
containment. `grep` also skips protected files during its walk.

`aaagent.core.policy` exposes `is_protected_target()` and
`extract_shell_paths()` for anyone building new tool plugins that
want the same gate.

### Secret scrubbing

`aaagent.core.sanitize.scrub()` redacts `sk-…`, `sk-ant-…`,
`ghp_…`, `xox[abprs]-…`, JWT tokens, `key=value` assignments for
sensitive env vars, `Authorization: Bearer …` headers, and known
per-provider env-var assignments (`MINMAX_API_KEY=`,
`FEISHU_APP_SECRET=`, …) to `***`. It runs:

- on every exception string returned by `tool_registry.execute`
  (closes the "provider auth error echoes api_key back into the
  next chat turn" leak path);
- on every exception string returned by `run_shell`;
- on every log record via `ScrubbingFormatter` installed by
  `Application.__init__`.

The old `_REDACT_PATTERNS` / `_redact_yaml` in `core.app` is
removed in favour of a single `scrub()` source.

### Project-root path resolution

`config.yaml` is now the single anchor for every relative path the
framework reads. Previously, `tools.allowed_dirs`,
`memory.data_dir`, `paths.dotenv`, and `limits.protected_paths`
were resolved against `os.getcwd()` at first use — meaning a
remote-launched aaagent (Feishu, systemd, CI) could see a
different filesystem than a locally-launched one. Now every
path-typed key is rewritten to an absolute path anchored at
`config.yaml.parent` at load time. `~` is expanded by
`Path.expanduser()` before that step. `tools.allowed_dirs`
defaults to `[<project_root>]` (was `[Path.cwd()]`); operators
who actually want CWD-relative behaviour can set
`tools.allowed_dirs: ["."]` explicitly.

New `aaagent.core.paths.resolve_project_path()` /
`resolve_all_paths()`. New `_PATH_KEYS` registry; add new
path-typed config keys there to keep the resolution semantics
uniform.

## 0.3.1 - Unreleased

### Breaking changes
- **Provider routing moved into `providers._meta`**. The top-level
  `default_provider:` and `fallback_providers:` keys are no longer
  read; both now live under `providers._meta.default` and
  `providers._meta.fallback` so every provider-related setting is in
  one place. `_meta` is a reserved key inside the `providers:` block
  and is skipped during provider instantiation. Migration: edit
  `config.yaml` manually (no runtime fallback). `Application` now
  reads `providers._meta.default` / `.fallback`; `/model -default`
  writes to `providers._meta.default`; `/models` reads the same.

### Adapters
- **feishu**: new `adapters.feishu.message_format` config option
  (`text` | `markdown` | `auto`, default `auto`). `text` keeps the
  previous plain-text behaviour (no markdown rendering);
  `markdown` always sends a Feishu Card v2 with a `markdown` element
  so the server renders bold/italic/code/links/lists/headings;
  `auto` picks `markdown` whenever the content contains a recognised
  markdown marker (`**`, `__`, `` ` ``, `#`, `>`, list bullets/numbers,
  `[…](…)`, fenced code blocks) and `text` otherwise. Slash-command
  replies and LLM output both flow through the same path. Invalid
  values log a warning and fall back to `auto`.

### Slash commands
- New `aaagent.core.commands.SlashCommandRegistry` centralises chat-time
  meta commands. CLI and Feishu adapters both emit a `slash_command`
  bus event for lines starting with `/`; core routes through the
  registry and emits `slash_reply`, `slash_quit`,
  `slash_session_switch`, `slash_unknown` for adapters to act on.
- Handler protocol switched to `async` so commands can do real I/O
  (provider calls, disk writes).
- New built-ins:
  - `/compact` — force-compress the current session, summarising older
    messages via the active provider (`Session.force_compress`).
  - `/model --provider X --model Y [-new --key K --base-url U]
    [-default]` — switch the active provider+model, optionally create
    a brand-new provider entry and/or make it the default. `-new`
    requires both `--key` and `--base-url`.
  - `/models` — list all providers with their current model and type,
    marking the active one with `*` and the default with `(default)`.
- `/model` writes changes back to `config.yaml` via
  `aaagent.core.config_io.ConfigStore` (ruamel.yaml round-trip —
  comments and ordering are preserved; previous file is copied to
  `config.yaml.bak` before each save). Persistence failure is surfaced
  in the reply but does not roll back the in-memory change.
- New dependency: `ruamel.yaml>=0.18` (round-trip YAML).
- Built-ins: `/help`, `/quit`, `/session`, `/sessions`. The redundant
  `/exit` alias was removed (use `/quit`).
- `/session` with no args now starts a brand-new session (auto-id like
  `cli-new-143012`) instead of just reporting the current one; the old
  behaviour of switching by name is preserved.
- New `/sessions` lists all known sessions from `SessionStore`, marking
  the current one with `*`. The current session is always shown, even
  if the user typed `/sessions` before sending any user message (in
  which case the store is empty).
- CLI slash replies now render `[bold green]` (was `[dim]`) so command
  feedback is visible in every terminal theme.
- Per-platform blacklists (`config.yaml: slash_command_blacklist.<platform>`)
  suppress side effects on platforms where they don't make sense (e.g.
  `/quit` on Feishu) while still replying a friendly "this platform does
  not support that command" notice.

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
- `websearch` plugin resolves `${ENV_VAR}` placeholders via
  `aaagent.core.envutil.resolve_env` and raises on missing key; previously
  the literal placeholder string was forwarded to the backend and caused
  spurious 401s.

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
- Moderation / content-policy blocks now also trigger fallback (markers:
  `sensitive`, `unprocessable_entity`, `content_filter`,
  `content_policy_violation`, `policy_violation`). MiniMax's stricter
  filter previously masked useful responses with a generic 422; the chain
  now falls through to a more permissive provider.
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