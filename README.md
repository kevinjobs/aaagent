# aaagent

A pluggable IM + LLM agent framework. The core stays minimal — only
mechanisms live here. Providers, tools, IM adapters, session stores,
memory stores, and slash-command bundles are all loaded as plugins
through Python entry points.

## Architecture

```
aaagent                 # core (CLI, EventBus, plugin manager, PromptBuilder, ...)
└── plugins/             # uv workspace of in-tree plugins
    ├── aaagent-plugin-openai/
    ├── aaagent-plugin-filetools/
    ├── aaagent-plugin-shelltools/
    ├── aaagent-plugin-memorytools/
    ├── aaagent-plugin-mcp/
    ├── aaagent-plugin-cliadapter/
    ├── aaagent-plugin-feishu/
    ├── aaagent-plugin-inmemorysession/
    ├── aaagent-plugin-markdownstore/
    ├── aaagent-plugin-shell/   # slash commands: /model /compact /session /sessions
    └── ...
```

The core defines six protocols (`Provider`, `ToolPlugin`, `IMAdapter`,
`SessionStoreFactory`, `MemoryStoreFactory`, plus the `aaagent.commands`
entry-point group for slash-command bundles) and a `PluginManager` that
discovers implementations via:

1. Python entry points (`importlib.metadata.entry_points(group=...)`)
2. Explicit `config.yaml` overrides (`plugins:` block)

The core has no built-in registry of plugin classes — `pip install
aaagent[default]` (or `uv sync --all-packages` for the in-tree
workspace) is what brings the plugins into reach. The core does not
import any of them by name.

See [docs/plugin-authoring.md](docs/plugin-authoring.md) for the full
plugin authoring guide.

## Installation

The project uses [uv](https://github.com/astral-sh/uv) workspaces.

```bash
uv sync --all-packages
```

This installs `aaagent` plus every in-tree plugin (default + extras).
After that:

```bash
aaagent chat    # CLI chat mode (requires cliadapter + openai + at least one tool)
aaagent run     # start all enabled adapters from config.yaml
```

If you only need the core (for instance to write a new plugin):

```bash
uv pip install aaagent
uv pip install aaagent-plugin-openai     # + any other plugins
```

### Default install (recommended)

`pip install aaagent[default]` pulls in the seven plugins that make
`aaagent chat` work out of the box:

```bash
uv pip install 'aaagent[default]'
```

Equivalent to installing:

- `aaagent-plugin-openai` (LLM provider)
- `aaagent-plugin-inmemorysession` (session store)
- `aaagent-plugin-markdownstore` (memory store)
- `aaagent-plugin-cliadapter` (CLI REPL adapter)
- `aaagent-plugin-filetools`, `aaagent-plugin-shelltools`,
  `aaagent-plugin-memorytools` (tool plugins)
- `aaagent-plugin-shell` (slash commands: `/model` / `/compact` /
  `/session` / `/sessions`)

### Adding Feishu

```bash
uv pip install aaagent-plugin-feishu
```

### Adding MCP tool servers

```bash
uv pip install aaagent-plugin-mcp
```

Configure `mcp.servers` in `config.yaml` (see [Configuration](#mcp)).

### Adding web search / fetch

```bash
uv pip install aaagent-plugin-websearch aaagent-plugin-webscrape
```

- **websearch** (Tavily backend by default): provides `web_search`; needs
  `TAVILY_API_KEY` from <https://tavily.com> (free tier 1000 req/month).
- **webscrape**: provides `fetch_url` — fetches a URL and returns its main
  content as clean Markdown / text / HTML using httpx + trafilatura. JS-heavy
  pages fall back to title + first paragraph + link list. No API key required.

## Capability limits

Tune `limits:` in `config.yaml` to keep the agent from running
away or touching the wrong files. Every cap below has a safe
built-in default.

```yaml
limits:
  # Outer fence on a single _handle_message call.
  max_tool_wallclock_s: 120
  # Maximum number of agent turns (LLM -> tools -> repeat) before
  # the loop is aborted.
  max_tool_turns: 10
  # Cumulative size of the messages list passed to the LLM.
  max_tool_chars: 200000
  # Per-provider token bucket (default 30 RPM; legacy
  # `rate_limit.provider_rpm` still honoured).
  provider_rpm: 30
  # Provider persistence: "disk" (default) or "memory" (transient).
  provider_persistence: disk
  # Globs the agent is never allowed to read or write, even through
  # `run_shell` redirection. Keep the defaults.
  protected_paths:
    - config.yaml
    - config.yaml.bak
    - .env
    - .env.*
    - "*.pem"
    - "*.key"
    - "**/id_rsa*"
```

What the layers do:

- **`max_tool_wallclock_s`** — outer wall-clock fence. A stuck
  tool loop is killed after this many seconds; the user sees a
  `工具循环超时（N s），已中止` reply.
- **`max_tool_turns`** / **`max_tool_chars`** — turn count and
  cumulative `messages` size limits inside the loop.
- **`provider_rpm`** — per-provider token bucket. Default is now
  `30` (was `0` = unlimited), so a runaway retry storm is bounded.
- **`provider_persistence`** — `memory` mode means `/model -new`
  keeps the provider in-process only and never touches
  `config.yaml`. Useful for trying out a new provider without
  leaving a permanent entry.
- **`protected_paths`** — globs that the LLM is forbidden from
  reading or writing. Enforced for `read_file` / `write_file` /
  `list_dir` / `grep`, AND for `run_shell` redirection targets
  (`>`, `>>`, `<`, `2>`, `&>`). Built on
  `aaagent.core.policy.is_protected_target()`.

### Secrets stay out of `config.yaml`

`/model --key K` writes the key to `.env` (atomic,
comment-preserving, idempotent) via
`aaagent.core.dotenv_io.DotenvStore`. `config.yaml` only ever
holds a `${ENV_VAR}` reference like `api_key:
"${MINMAX_API_KEY}"`. If the new key overwrites an existing
env var that's also used by other providers in `config.yaml`,
the reply includes a `WARN overwrote <ENV>; also used by: ...`
line.

Legacy `api_key: sk-...` entries in `config.yaml` are preserved
untouched until you explicitly re-run
`/model --provider X --key K`, which migrates them in place.

### Secret scrubbing

`aaagent.core.sanitize.scrub()` redacts `sk-...`,
`sk-ant-...`, `ghp_...`, `xox[abprs]-...`, JWT tokens, sensitive
env-var assignments, and `Authorization: Bearer ...` headers to
`***`. It runs on every exception string returned by a tool
(so a provider auth error that includes `sk-...` cannot be
echoed back to the LLM on the next turn) AND on every log
record via a `ScrubbingFormatter` installed by
`Application.__init__`.

### Project-root paths

`config.yaml` is the single anchor for every relative path the
framework reads. `tools.allowed_dirs`, `memory.data_dir`,
`paths.dotenv`, and `limits.protected_paths` are all rewritten
to absolute paths anchored at the directory containing
`config.yaml` at load time. This means aaagent always sees the
same filesystem no matter where it was launched from
(local terminal, Feishu bot, systemd, CI). `~` is expanded by
`Path.expanduser()` before the resolution step.

Add new path-typed config keys to `_PATH_KEYS` in
`aaagent.core.paths` to keep the resolution semantics uniform.

## Configuration

`config.yaml` is a **local, machine-specific file** — it is
**gitignored** and should never be committed. The version-controlled
template is `config.yaml.example`.

```bash
cp .env.example .env
cp config.yaml.example config.yaml
```

Fill in API keys in `.env`, then edit `config.yaml` to wire providers,
adapters, tools, memory, and rate limits.

On first run, if `config.yaml` is missing, aaagent auto-copies
`config.yaml.example` into `config.yaml` and logs a warning — you still
need to fill in the keys before `/model` works.

### Providers

Each provider requires a `type` field that matches the `Provider.type`
class attribute / entry-point name of an installed plugin. Built-in
plugins ship with `openai_compatible` (covers OpenAI, DeepSeek, Qwen,
MiniMax, cntoken, etc.) and `custom` (load any class via
`cfg.class`).

```yaml
providers:
  # Routing metadata lives under _meta so all provider config stays
  # in one block. _meta is a reserved key; _setup_providers skips it.
  _meta:
    default: deepseek
    # Ordered list of providers to fall back to when the primary
    # fails with a transient error (network / timeout / 429 / 5xx /
    # overloaded). Moderation blocks also trigger fallback.
    # Non-transient errors (auth, bad request) are raised immediately.
    fallback:
      - openai

  openai:
    type: openai_compatible
    enabled: true
    api_key: "${OPENAI_API_KEY}"
    base_url: ""
    model: gpt-4o

  deepseek:
    type: openai_compatible
    enabled: true
    api_key: "${DEEPSEEK_API_KEY}"
    base_url: "https://api.deepseek.com/v1"
    model: deepseek-chat

  custom_provider:
    type: custom
    enabled: true
    class: my_package.MyProvider
```

- `api_key`: supports `${ENV_VAR}` syntax (loaded from `.env`)
- `base_url`: override the API endpoint for OpenAI-compatible services
- `class`: dotted path for `type: custom` providers

### Adding a custom provider (in-process)

If your provider is not yet a published plugin, add an explicit
declaration:

```yaml
plugins:
  - kind: provider
    type: my_provider
    class: my_pkg.sub:MyProvider
```

`kind` is one of `provider`, `tool`, `adapter`, `session`, `memory`.

### Adapters

```yaml
adapters:
  feishu:
    enabled: false
    app_id: "${FEISHU_APP_ID}"
    app_secret: "${FEISHU_APP_SECRET}"
    # text | markdown | auto (default auto)
    # When auto, messages containing markdown syntax (`# `, `**bold**`,
    # `` `code` ``, `[link](url)`, lists, blockquotes, code fences) are
    # sent as Feishu Card v2 with a markdown element so the server
    # renders them. Plain prose stays as `msg_type: text`.
    message_format: auto
  cli:
    enabled: true
```

- **feishu**: WebSocket-based, requires app credentials from
  <https://open.feishu.cn/app>
- **cli**: REPL chat; only meaningful with the `chat` command

### Session

```yaml
session:
  type: inmemory               # matches plugin entry-point name
  max_history: 20
  compress_threshold: 0.8
```

- `type`: `inmemory` plugin (default), or any installed session plugin
- `max_history`: max messages kept per session (sliding window)
- `compress_threshold`: when the message count exceeds
  `max_history * compress_threshold`, the oldest messages are summarized
  and replaced with a single summary system message

### Tools

Tools come from `aaagent.tools` plugins. Enabled / disabled via the
plugin name:

```yaml
tools:
  file:
    enabled: true
    allowed_dirs:
      - "D:/Projects/aaagent"
  shell:
    enabled: true
    timeout: 30
    max_output: 4096
  memory:
    enabled: true
  websearch:
    enabled: true
    backend: tavily
    api_key: "${TAVILY_API_KEY}"
    max_results: 5
    timeout: 15
  webscrape:
    enabled: true
    timeout: 20
    max_bytes: 5000000
```

- `file` (aaagent-plugin-filetools): `read_file`, `write_file`,
  `list_dir`, `grep`; paths must live under one of `allowed_dirs`
- `shell` (aaagent-plugin-shelltools): `run_shell` with a safety policy
- `memory` (aaagent-plugin-memorytools): `remember`, `recall` operating
  on the configured `MemoryStore`
- `websearch` (aaagent-plugin-websearch): `web_search` using a pluggable
  backend (Tavily by default); needs `TAVILY_API_KEY`
- `webscrape` (aaagent-plugin-webscrape): `fetch_url` returning Markdown /
  text / HTML via httpx + trafilatura, with `timeout` and `max_bytes` caps

#### Shell safety policy

The shell tool denies commands targeting root (`rm -rf /`, `dd if=<abs>`,
`mkfs.*`, redirect to `/dev/sd*`, `chmod 777 /`, `chown root:root /`,
fork bomb) regardless of any bypass attempt (backslash, pipeline, etc.).

### Memory

```yaml
memory:
  type: markdown               # matches plugin entry-point name
  enabled: true
  data_dir: "data/memories"
  # 空闲超过此时长（小时）的会话会被归档到 archive.md 并从内存移除
  archive_after_hours: 24
  recall:
    relevance_weight: 0.7
    recency_weight: 0.3
```

- `type`: `markdown` plugin (default), or any installed memory plugin
- Three Markdown files under `data_dir`: `profile.md`,
  `facts/YYYY-MM-DD.md`, `archive.md`
- `profile.md` is consolidated when entries >= 15
- `recall` ranks memories by IDF-weighted token overlap + recency decay,
  and accepts a `tags` filter / `top_k` (the `recall` tool exposes both)

Relative `data_dir` paths resolve against the directory containing
`config.yaml`, not cwd.

### MCP

Connects your agent to any [Model Context Protocol](https://modelcontextprotocol.io)
server. Each configured server's tools are auto-expanded into the registry
under the `<server_name>_<tool_name>` namespace.

```yaml
mcp:
  servers:
    - name: fs
      transport: stdio  # stdio | http
      command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."]
    - name: remote
      transport: http
      url: "http://localhost:8080/mcp"
```

Servers with `enabled: false` are skipped. A failing server is logged and
isolated — other servers and native tools still work.

### Rate limiting

```yaml
rate_limit:
  provider_rpm: 60
```

If set, calls to the provider (chat and stream_chat) are throttled to
the given requests-per-minute.

## CLI Usage

### Chat mode (interactive)

```bash
python -m aaagent chat
```

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/session` | Start a new session (auto-generated id like `cli-new-143012`) |
| `/session <name>` | Switch to session `<name>` (prefix auto-applied if missing) |
| `/sessions` | List all known sessions (current marked with `*`) |
| `/compact` | Force-compress the current session (summarises older messages) |
| `/model` | Switch provider+model, optionally add new ones (see below) |
| `/models` | List all configured providers and their models |
| `/quit` | Exit chat |

Slash commands are handled centrally by `aaagent.core.commands.SlashCommandRegistry`
so future IM adapters (web, Slack, ...) get the same `/help` / `/quit`
support out of the box. The core only ships the two protocol-level
commands every adapter needs (`/help`, `/quit`); the chat-time
`/model`, `/compact`, `/session`, `/sessions` come from
`aaagent-plugin-shell` (pulled in via `aaagent[default]`). Plugins can
register additional commands through the `aaagent.commands` entry-point
group — each entry point is a callable `(app: Application) -> None`
that registers one or more handlers.

The agent-loop itself is pluggable. `aaagent.core.agent_loop.AgentLoop`
is the strategy that turns one inbound message into an assistant
reply; the bundled `DefaultAgentLoop` does the historical
tool-iteration + provider-fallback dance. Alternative loops (plan-and-
execute, agent-as-tool, tree-of-thought, ...) install themselves via
`Application(agent_loop=...)`.

Per-platform blacklists in `config.yaml`
(`slash_command_blacklist.<platform>`) suppress side effects (e.g.
`/quit` on Feishu) while still replying a friendly "this platform does
not support that command" notice.

### `/model` syntax

```
/model                                                  # show current model
/model --provider X --model Y                           # switch on existing provider X
/model --provider X --model Y -default                  # also make X the primary
/model --provider X --model Y -new \
        --key sk-xxx --base-url https://api.x.com/v1    # add new provider then switch
/model --provider X --model Y -new --key K \
        --base-url U -default                           # add + switch + primary
```

`-new` requires both `--key` and `--base-url`; `--type` defaults to
`openai_compatible`. All changes are **persisted to `config.yaml`** via
ruamel.yaml round-trip (comments and ordering preserved). The previous
file is saved as `config.yaml.bak` before each write.

### Run mode (start all enabled IM adapters)

```bash
python -m aaagent run
```

### Specify config file

```bash
python -m aaagent chat --config path/to/config.yaml
python -m aaagent run --config path/to/config.yaml
```

## Tool Calling Behavior

When the LLM decides to call tools, it goes through a loop:

- Up to **20 turns** per request (`_MAX_TOOL_TURNS`); exceeding returns
  `"已达到最大工具调用次数。"`
- Accumulated `messages` payload must stay under **200,000 characters**
  (`_MAX_TOOL_CHARS`); exceeding returns `"上下文过长，已中止。请开启新对话。"`
- After the loop, the session is compressed if it exceeds
  `max_history * compress_threshold`, triggered inside the same lock
  as `add_message` so concurrent requests cannot interleave compress
  and append.
- Tool execution time is reported via the `tool_result` event payload
  (`duration_ms`).

## Streaming

The OpenAI provider supports streaming. When no tools are configured,
the agent yields tokens to the CLI as they arrive (via the `stream_token`
event). When tools are present, the agent still uses non-streaming
`chat()` because it needs to parse `tool_calls` deterministically.

## Logs

Default format:

```
2026-08-21 12:34:56 [INFO] [s1/feishu] aaagent.tools: ...
```

The `[session_id/platform]` segment is filled from contextvars set by
`Application._on_message_received` so log lines are correlatable per
request.

`run` mode prints to stderr; `chat` mode writes to `logs/aaagent.log`
and keeps the console quiet.

For the Feishu adapter, set `FEISHU_DEBUG=1` to enable verbose frame
logging.

## Troubleshooting

- **`No provider plugin for type 'xxx'`** — install the matching plugin,
  e.g. `pip install aaagent-plugin-openai`.
- **`command denied by safety policy`** — your command matched a shell
  deny rule; review the rule list above.
- **Provider 4xx/5xx storm** — set `rate_limit.provider_rpm` to throttle.
- **Memory not persisting** — check that `data_dir` resolves to a
  writable directory. With relative paths, it resolves against the
  `config.yaml` directory.
- **Feishu adapter not starting** — `app_id` / `app_secret` must be
  set; the adapter logs an error and exits if missing.
- **`logs/aaagent.log` not created in `chat` mode** — check that
  `logs/` is writable relative to cwd.

## Plugins

`aaagent` is shipped with these in-tree plugins (all included via
`uv sync --all-packages`):

| Folder | Provides | Plugin class |
|---|---|---|
| `aaagent-plugin-openai` | OpenAI-compatible LLM provider | `OpenAICompatibleProvider` |
| `aaagent-plugin-filetools` | File tools (read/write/list/grep) | `FileToolsPlugin` |
| `aaagent-plugin-shelltools` | Shell execution | `ShellToolsPlugin` |
| `aaagent-plugin-memorytools` | remember / recall tools | `MemoryToolsPlugin` |
| `aaagent-plugin-mcp` | Model Context Protocol server bridge | `McpToolsPlugin` |
| `aaagent-plugin-shell` | Slash commands: `/model` / `/compact` / `/session` / `/sessions` | `register(app)` |
| `aaagent-plugin-cliadapter` | CLI REPL adapter | `CliAdapter` |
| `aaagent-plugin-feishu` | Feishu WebSocket adapter | `FeishuAdapter` |
| `aaagent-plugin-inmemorysession` | In-memory session store | `InMemorySessionFactory` |
| `aaagent-plugin-sqlitesession` | SQLite-backed session store + history tools | `SqliteSessionStore`, `_DualWriteSessionStore`, `SqliteSessionToolsPlugin` |
| `aaagent-plugin-markdownstore` | Markdown memory store | `MarkdownMemoryStoreFactory` |
| `aaagent-plugin-websearch` | Web search (Tavily backend) | `WebSearchToolsPlugin` |
| `aaagent-plugin-webscrape` | URL fetch + extract (httpx + trafilatura) | `WebScrapeToolsPlugin` |
| `aaagent-plugin-skills` | Skills plugin (LLM-authorable file-backed instructions) | `SkillsPlugin` |
| `aaagent-plugin-scheduler` | Cron-style scheduler tools | `SchedulerToolsPlugin` |

To author a new plugin, see
[docs/plugin-authoring.md](docs/plugin-authoring.md).

## Project Structure

```
aaagent/                                  # workspace root
├── pyproject.toml                         # aaagent-core + workspace members
├── src/aaagent/                           # core (CLI, bus, plugin manager, ...)
│   ├── cli.py
│   └── core/
│       ├── app.py                          # Application (plugin-driven)
│       ├── bus.py                          # EventBus
│       ├── commands.py                     # SlashCommandRegistry + core /help /quit
│       ├── config_io.py                    # ruamel.yaml round-trip ConfigStore
│       ├── dotenv_io.py                    # comment-preserving DotenvStore
│       ├── envutil.py                      # ${ENV_VAR} resolver
│       ├── logctx.py                       # contextvars logging context
│       ├── memory.py                       # MemoryStore ABC
│       ├── message.py
│       ├── paths.py                        # project-root path resolution
│       ├── plugin.py                       # Provider / ToolPlugin / IMAdapter / factories + PluginManager
│       ├── policy.py                       # protected-paths + shell path extraction
│       ├── prompt.py                       # PromptBuilder
│       ├── ratelimit.py                    # TokenBucket
│       ├── sanitize.py                     # secret scrubbing
│       ├── session.py                      # SessionStore ABC
│       ├── tool_registry.py                # ToolRegistry
│       └── types.py                        # ChatResponse / ToolCall / legacy LLMProvider alias
├── plugins/                                # uv workspace of in-tree plugins
└── docs/plugin-authoring.md
```

## Development

```bash
uv sync --all-packages        # install all workspace packages
pytest                        # run tests
```

Lint and type-check configurations live in `pyproject.toml`.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).