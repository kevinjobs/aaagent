# Changelog

## 0.4.5 - Unreleased

### New plugin: `aaagent-plugin-web` (browser chat UI)

After `pip install aaagent-plugin-web`, a new top-level CLI command
becomes available:

    aaagent web [--port 8848] [--host 127.0.0.1] [--open/--no-open]

It boots the aaagent Application together with a `WebAdapter`
(based on `IMAdapter`) and a FastAPI server that:

* Serves the bundled React SPA (DSH-inspired palette, shadcn/ui
  components) from `web/dist/`.
* Exposes a WebSocket at `/api/ws` that fans the existing EventBus
  events (`message_to_send`, `stream_token`, `tool_start`,
  `tool_result`, `slash_*`) to every connected browser tab.
* Falls back to a clear "Frontend not built" page when the
  Vite-built assets are missing (so users without npm can still
  use the WebSocket directly via `wscat`).

How `aaagent web` hooks in:

* Core: new `aaagent.cli_commands` entry-point group + tiny CLI
  loader. Plugins that want to add a top-level Typer subcommand
  just expose a `(typer_app, config_path) -> None` function.
  No core logic changes; `Application` / `EventBus` /
  `IMAdapter` are untouched.
* Plugin: registers both `aaagent.adapters:web` and
  `aaagent.cli_commands:web` entry points; new `WebAdapter`
  subscribes to the same bus events the CLI adapter already
  knows about, plus a `/api/ws` handler in `server.py`.

Bundled frontend (under `plugins/aaagent-plugin-web/web/`):

* React 18 + TypeScript + Vite + Tailwind + shadcn/ui.
* Auto-reconnecting WebSocket with a 30 s keep-alive ping.
* Markdown + GFM + syntax highlighting via `react-markdown` /
  `rehype-highlight`.
* Light/dark theme that follows OS preference and can be
  toggled in the header (state persisted to localStorage).
* Tool-call trace as inline cards (collapse/expand), one per
  `tool_start`/`tool_result` pair.

Tests:

* 2 new core tests in `test_plugin.py` for CLI-command discovery.
* 8 new plugin tests in `plugins/aaagent-plugin-web/tests/`
  covering: message_to_send fan-out, platform filtering,
  inbound → bus translation, slash command translation, health
  endpoint, fallback-page render, WebSocket round-trip end-to-end,
  ping/pong heartbeat.

Test status: 362 passed / 1 skipped / 0 failed.

### Fix: pin the active LLM provider for tool turns in the same message

Cross-provider `tool_call_id`s are not portable — each provider mints
ids in its own format (`call_xxx`, `call_chatcmpl-xxx`,
`call_function_xxx`, `toolu_xxx`, …). When `_chat_with_fallback`
fell through from a fallback provider back to the primary mid-way
through a multi-turn agent loop, the primary received a tool
result whose `tool_call_id` it didn't recognise and rejected the
whole request with HTTP 400:

> invalid params, tool result's tool id(call_function_xxx) not found (2013)

In production this surfaced as noisy per-turn logs like

> Provider minmax failed (retryable), trying next: … tool result's tool id …

…appearing on every turn after the first one, even though a
fallback kept picking up the slack. The bot worked but every
inbound message produced N warning lines.

Fix: `Application` now remembers the provider that successfully
handled the previous LLM call as `_active_provider` and pins it at
the front of the fallback order. Subsequent turns in the same
`_handle_message` start with the active provider, so tool `id`s
stay consistent. If the active provider fails (retryable error),
the rest of the chain is still consulted — they just stop being
the always-first choice. `_active_provider` is reset at the start
of every `_handle_message`, so each new inbound message is free
to pick the best starting provider again.

Regression tests:
`test_app.py::test_chat_with_fallback_pins_active_provider` and
`test_app.py::test_chat_with_fallback_resets_active_provider_on_new_handle`.

### Fix: per-session lock to prevent "mixed-context" replies

When two `message_received` events for the same `session_id` arrived
in quick succession (e.g. a user message plus a scheduler-fired
reminder), the two `_handle_message` coroutines used to run
concurrently. Both would `add_message()` then `get_session()` then
call the LLM. The second call saw the first call's inbound message
but NOT its reply (which hadn't been written yet), and both LLM calls
ended up reasoning over the same half-baked context. The user-visible
symptom was a single reply that mixed two unrelated topics, e.g.

> 你好呀，强哥！👋 灰机随时待命～...
> 支持的，强哥。目前有两种定时任务：...

…after typing `你好`.

Fix: `Application` now holds a per-`session_id` lock. `_on_message_received`
acquires the lock before calling `_handle_message`, so the second call
waits for the first to fully finish (including persisting its reply).
Different sessions still run in parallel; only same-session
`_handle_message` calls are serialised. This is the same scope a
single agent loop naturally has, so the lock matches the
expectation rather than introducing new ordering.

`aaagent-plugin-scheduler` and the IM adapters both emit
`message_received`; if their events overlap with a user message in the
same session, the lock now prevents the LLM from interleaving them.

Regression test: `test_app.py::test_concurrent_messages_same_session_are_serialised`.

### Feishu: "thinking…" indicator for slow LLM replies

`aaagent-plugin-feishu` now posts a temporary "thinking…" message
after a configurable delay if the LLM hasn't replied yet, then deletes
it as soon as the real reply lands. This gives the user immediate
feedback that the bot is alive during slow LLM/tool runs (long MCP
calls, big model warmup, etc.) without cluttering the chat history.

```yaml
adapters:
  feishu:
    app_id: ${FEISH_APP_ID}
    app_secret: ${FEISH_APP_SECRET}
    pending_indicator:
      enabled: true          # default
      delay_seconds: 3.0     # default; 0 disables effectively
      text: "🤔 思考中..."     # default
```

- Default config ships enabled. To opt out, set
  `pending_indicator.enabled: false`.
- Per-chat state: each `(chat_id)` runs its own timer; concurrent
  chats don't interfere.
- Cancel-on-replace: a new inbound message on the same chat cancels
  the in-flight timer.
- Cancel-on-reply: a `message_to_send` for the same chat cancels the
  timer and deletes the indicator before the real reply goes out.
- Failure-tolerant: a delete failure logs a warning but never blocks
  the real reply (the actual send is fired by the existing
  `_on_message_to_send` handler, in parallel with the delete).

### Internals

- `FeishuAdapter.send()` now returns the platform-side `message_id` on
  success (or `None` on failure). Existing callers ignore the return
  value; back-compat preserved.
- New `FeishuAdapter.delete_message(message_id)` wraps
  `DELETE /open-apis/im/v1/messages/{message_id}`.

### Test layout: core tests in `src/aaagent/tests/`, plugin tests in each plugin

Before: a single flat `tests/` at repo root held all 27 tests for the
core and every plugin. After: tests live next to the code they test,
each with its own `tests/` subdirectory.

```
/
├── conftest.py                      # shared fixtures (tmp_memory_dir, fake_provider, fake_profile_provider)
├── pyproject.toml                   # root pytest: testpaths = ["src/aaagent/tests", "plugins"]
├── src/aaagent/
│   ├── testing.py                   # new: public FakeProvider helper
│   └── tests/                       # 12 core tests
│       ├── test_app.py
│       └── ...
└── plugins/aaagent-plugin-X/
    ├── pyproject.toml               # per-plugin pytest: testpaths = ["tests"]
    └── tests/                       # plugin tests
```

- Two entry points for testing:
  - **Repo-root `pytest`** — one command runs every test (340 tests)
    across the core and all 15 plugins. The root `conftest.py` is
    auto-discovered and wires every plugin's `src/` into `sys.path`
    so the core's test suite can resolve plugin imports.
  - **Per-plugin `cd plugins/aaagent-plugin-X && pytest`** — tests
    just that plugin. The plugin's own `pyproject.toml` sets
    `testpaths = ["tests"]` and `pythonpath = ["src"]`; the root
    `conftest.py` is still auto-discovered (pytest walks up to the
    repo root), so the shared `FakeProvider`, `tmp_memory_dir`, and
    `fake_profile_provider` fixtures remain available to plugins that
    need them.
- `aaagent.testing.FakeProvider` replaces the old
  `tests.conftest.FakeProvider` — the four test files that referenced
  it by YAML string now use `aaagent.testing.FakeProvider`. The class
  itself is unchanged; it's just moved to a public location so plugins
  can import it without depending on the root tests package.

### `PluginContext` — single, explicit handle for plugin framework access

Replaces the ad-hoc `hasattr(plugin, "set_memory")` / `set_application`
probes that leaked the `Application` object into plugins. The core
defines a single `PluginContext` dataclass:

```python
@dataclass
class PluginContext:
    event_bus: EventBus
    session_store: SessionStore
    memory_store: MemoryStore | None
    project_root: Path
    config: dict[str, Any]
```

`Application._setup_tool_registry` builds one `PluginContext` per
startup and hands it to every tool plugin via the new
`ToolPlugin.set_context(ctx)` hook (default no-op). Adding a new
plugin-visible capability now means adding a field to `PluginContext`
instead of inventing a new attribute probe.

**Migrated plugins:**
- `aaagent-plugin-memorytools` now reads `ctx.memory_store` instead of
  using the legacy `set_memory` probe.
- `aaagent-plugin-scheduler` now reads `ctx.event_bus` and
  `ctx.project_root` instead of holding a back-reference to the
  `Application` object.

### `AgentLoop` — per-request agent loop is now pluggable

The historical `_handle_message` / `_run_tool_loop` / `_stream_or_chat`
/ `_run_tool_loop_with_limits` body moved out of `Application` into a
new `aaagent.core.agent_loop` module:

- `AgentLoop` — protocol with a single
  `async handle_message(message, context) -> str` method.
- `AgentContext` — the inputs a loop needs per request
  (session_id, platform, chat_id, messages, tools, profile, system_prompt).
- `DefaultAgentLoop` — the bundled implementation, taking a
  back-reference to `Application` and reading `_chat_with_fallback`,
  `_stream_or_chat`, `_tool_registry`, etc. from it.

`Application.__init__(agent_loop=...)` accepts a custom loop. Default
is `DefaultAgentLoop(self)` so existing behaviour is unchanged. Plugins
can ship alternative loops (plan-and-execute, tree-of-thought,
agent-as-tool, ...) without forking the framework.

`Application._handle_message` is now ~20 lines: persist inbound
message → build `AgentContext` → delegate to `self._agent_loop` →
persist reply → emit `message_to_send`. The loop owns everything else.

The `_THINK_RE` / `_UNCLOSED_THINK_RE` / `_strip_think` /
`_PUBLIC_ERROR` constants moved into `agent_loop.py` (with re-exports
in `app.py` so existing imports continue to work).

### Core slim-down: builtin registry removed, default plugins via `pip extras`

The core no longer carries a built-in registry of plugin classes. Every
provider, tool, adapter, session store, memory store, and slash-command
bundle is now discovered purely through `importlib.metadata.entry_points`,
plus an optional `config.yaml` `plugins:` block. To get the example
config working out of the box:

```bash
pip install 'aaagent[default]'
```

The `default` extra pulls in seven plugins (openai, inmemorysession,
markdownstore, cliadapter, filetools, shelltools, memorytools, plus
the new shell command bundle). The core has no knowledge of which
packages fill those slots — only the entry-point groups.

**Removed:**
- `aaagent.core._builtin_wrappers` and the `BUILTIN_*` dicts on
  `PluginManager`. Plugin discovery no longer has a "fallback to
  in-tree classes" layer.
- Direct-construction fallbacks in `Application.__init__` for session
  and memory stores: if no plugin is installed, `Application` now
  raises `PluginNotFoundError` rather than silently constructing an
  abstract-class instance.
- The bespoke CLI-adapter discovery path in `cli.py`; the chat
  command now uses the same `PluginManager` the rest of the app
  uses, so there is one and only one plugin-discovery surface.
- The legacy `LLMProvider` shim in `_instantiate_provider`. The core
  now accepts `Provider` instances directly; `LLMProvider` is retained
  only as a back-compat alias so existing tests / `FakeProvider`
  subclasses keep working.

**Moved out of core into plugins:**
- `/model`, `/compact`, `/session`, `/sessions` slash commands →
  new `aaagent-plugin-shell` package (workspace member; pulled in via
  `aaagent[default]`).
- OpenAI-SDK-specific named exception classes (used for retry
  classification) → `aaagent-plugin-openai.is_retryable_error` override.
  The core's default classifier only owns universal signals (network /
  OS / HTTP status substring sweep / moderation-policy substrings).
- `DotenvStore` / `ConfigStore` round-trip semantics are still in
  core, but `/model` (the only caller) moved into the shell plugin.

**New protocol-level API:**
- `Provider.is_retryable_error(self, exc)` — base implementation
  lives in the core; plugin authors override to plug in their SDK's
  named exceptions without forcing the core to know those class names.
- `Application.commands` (read-only) and `register_slash_command(...)`
  helpers — the public surface plugins use to contribute slash
  commands without poking at private attributes.
- New entry-point group `aaagent.commands` — `aaagent-plugin-shell`
  is the canonical registrar; third-party plugins can ship their own.

**Semantics:**
- `Application(providers={})` is now treated as "skip `_setup_providers`,
  use exactly these" (matching the way `providers={"fake": ...}` was
  already interpreted). `providers=None` keeps the previous behaviour
  of falling back to `config.yaml`.

**Test updates:**
- `tests/test_commands.py` now registers the plugin-owned commands via
  `aaagent_plugin_shell.register(app)` to mirror runtime wiring.
- `tests/test_plugin.py` no longer exercises the deleted `BUILTIN_*`
  class attribute; it tests the same config-driven override path
  through the modern `plugins:` block.

## 0.4.4 - Unreleased

### SQLite session mirror + cross-session history tools

New plugin `aaagent-plugin-sqlitesession` registers two session-store
factories and two LLM tools:

**Factories (`session.type` in config):**
- **`dual_write`** (default for new configs) — wraps an in-memory
  primary store as the hot path and asynchronously mirrors every
  write to a SQLite database. Writes are serialised with an
  `asyncio.Lock` and scheduled via `create_task`; SQLite failures
  only log a warning and never block the conversation.
- **`sqlite`** — a `SessionStore` that talks directly to SQLite with
  no in-memory cache. Useful for debugging or single-process workloads.

**Restore-on-startup:** the most recently active `restore_n` sessions
(default 1) are rehydrated from SQLite into the primary at app start;
other sessions are lazy-loaded on first `get_session` /
`get_context` access.

**Schema (SQLite):**
- `sessions(id PK, platform, chat_id, user_id, summary,
  system_prompt, created_at, last_activity)` with index on
  `(user_id, platform, last_activity DESC)`.
- `messages(id PK, session_id FK, role, content, raw JSON, timestamp,
  tool_call_id, name, tool_calls JSON)` with index on
  `(session_id, timestamp ASC)`.
- `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `synchronous=NORMAL`.

**LLM tools:**
- **`session_search`** — keyword search across past messages,
  scoped to `current_user_id()` + current `platform`.
  Returns matching snippets (200 chars), `session_id`, `role`,
  timestamp. Cross-user reads return empty, never an error.
- **`session_get_messages`** — fetch the message list of a single
  session with the same scope check (`session_id` required, `limit`
  capped at 200, optional `since_timestamp`).

**Tool plumbing:**
- `Application._last_message` is set on every `_on_message_received`
  so plugins can derive the current platform without re-plumbing.
- New entry-point groups: `aaagent.sessions` (`dual_write`, `sqlite`)
  and `aaagent.tools` (`sqlite_session`).

**Dependencies:** `aiosqlite>=0.19` (workspace-only, no runtime
impact on the core).

## 0.4.3 - Unreleased

### Scheduler plugin — cron-style scheduled tasks

New plugin `aaagent-plugin-scheduler` registers four tools that let
the LLM create, list, remove, and toggle cron-style scheduled tasks:

- **`schedule_create`** — create a `recurring` (5-field or 6-field
  with seconds cron expression) or `once` (ISO datetime) schedule.
  Requires `platform` / `chat_id` / `user_id` for delivery routing.
- **`schedule_list`** — list the current user's schedules (filtered
  by `creator_user_id` from the inbound message's `user_id`).
- **`schedule_remove`** / **`schedule_update`** — owner-only
  mutations.

Triggers fire on a 5-second tick (`tick_seconds` config). At fire
time the plugin emits `message_received` on the bus; the existing
Application pipeline handles the full agent loop (tools, memory,
session persistence, adapter reply) — no LLM code duplicated.

Permission model: each record carries `creator_user_id`; only the
creator can list / mutate their own schedules. Cross-user access
is refused.

Persistence: JSON file at `data/scheduler/schedules.json` (atomic
write via temp + rename, with best-effort cross-platform file lock).

### Plumbing

- New `set_application(self)` hook on `Application._setup_tool_registry`,
  mirroring `set_memory` so plugins can reach the bus + project root
  without new infrastructure in `PluginManager`.
- `aaagent.core.logctx` extended with a `user_id` contextvar plus
  `current_user_id()` accessor. The inbound message's `user_id` is
  now available to tool handlers via this contextvar, which the
  scheduler uses for owner-scoped operations.

## 0.4.2 - Unreleased

### Skills plugin — LLM-authorable, file-backed instructions

New plugin `aaagent-plugin-skills` registers four tools that let the
LLM manage a personal skill library:

- **`create_skill`** — writes a Markdown file with YAML frontmatter
  to `data/skills/<name>.md`. The LLM is instructed to ask the user
  before generating a new skill ("这个流程可以保存为 skill 方便下次
  复用，要生成吗？") rather than generating silently.
- **`list_skills`** — scans the skills directory, returns name /
  description / tags for each. Optional `tags` filter.
- **`load_skill`** — reads a skill file and returns it as a tool
  response, injecting its full content into the conversation for the
  LLM to reference. Session-scoped (not cross-session).
- **`delete_skill`** — removes a skill file. Only called on explicit
  user request.

Skills are **session-scoped**: the LLM must actively `load_skill` each
session. No automatic injection — this keeps the system simple and
self-governing. The LLM also decides when to proactively suggest
generating a skill based on context, per the tool description.

Config under `tools.skills.skills_dir` (default `data/skills`),
resolves against project root via `resolve_all_paths`.

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