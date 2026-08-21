# aaagent

A pluggable IM + LLM agent framework.

## Installation

```bash
uv sync
# or
pip install -e .
```

## Configuration

Copy `.env.example` to `.env` and fill in your API keys:

```bash
cp .env.example .env
```

Edit `config.yaml` to configure providers, adapters, tools, memory, and rate limits.

### Providers

Each provider requires a `type` field. Supported types:

- `openai_compatible` — OpenAI-compatible API (OpenAI, DeepSeek, Qwen, MiniMax, cntoken, etc.)
- `custom` — Dynamically load a custom provider class via `class` field

Set `default_provider` to specify which provider to use. If omitted, the first enabled provider is used.

```yaml
default_provider: deepseek

providers:
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

#### Adding a custom provider (in-process)

```python
from aaagent.providers.base import PROVIDER_TYPE_REGISTRY, LLMProvider, ChatResponse

class MyProvider(LLMProvider):
    async def chat(self, messages, tools=None, **kwargs):
        return ChatResponse(content="hi")

PROVIDER_TYPE_REGISTRY["my_provider"] = MyProvider
```

Then in `config.yaml`:

```yaml
providers:
  mine:
    type: my_provider
    enabled: true
```

### Adapters

```yaml
adapters:
  feishu:
    enabled: false
    app_id: "${FEISHU_APP_ID}"
    app_secret: "${FEISHU_APP_SECRET}"
  wechat:
    enabled: false
    token: ""
```

- **feishu**: WebSocket-based, requires app credentials from <https://open.feishu.cn/app>
- **wechat**: skeleton only, not yet implemented

### Session

```yaml
session:
  max_history: 20
  compress_threshold: 0.8
```

- `max_history`: max messages kept per session (sliding window)
- `compress_threshold`: when the message count exceeds `max_history * compress_threshold`,
  the oldest messages are summarized and replaced with a single summary system message

### Tools

```yaml
tools:
  allowed_dirs:
    - "D:/Projects/aaagent"
  shell:
    enabled: true
    timeout: 30
    max_output: 4096
```

- `allowed_dirs`: file tool paths must live under one of these; non-existent entries
  are warned and skipped at startup
- `shell.enabled`: toggles `run_shell`; default `true`
- `shell.timeout`: per-command timeout in seconds (default 30)
- `shell.max_output`: stdout/stderr truncation length (default 4096)

The shell tool enforces a safety policy: commands targeting root (`rm -rf /`, `dd if=<abs>`,
`mkfs.*`, redirect to `/dev/sd*`, `chmod 777 /`, `chown root:root /`, fork bomb) are
denied regardless of any bypass attempt (backslash, pipeline, etc.).

### Memory

```yaml
memory:
  enabled: true
  data_dir: "data/memories"
```

The memory store keeps three Markdown files in `data_dir`:
- `profile.md` — user preferences / identity (consolidated when entries >= 15)
- `facts/YYYY-MM-DD.md` — chronological fact log
- `archive.md` — session archives

The agent can call the `remember` and `recall` tools to manage its own memory.

Relative `data_dir` paths resolve against the directory containing `config.yaml`,
not the current working directory, so the memory location is stable.

### Rate limiting

```yaml
rate_limit:
  provider_rpm: 60
```

If set, calls to the provider (chat and stream_chat) are throttled to the given
requests-per-minute. Useful for upstream APIs with QPS limits.

## CLI Usage

### Chat mode (interactive)

```bash
python -m aaagent chat
```

Enter interactive REPL. Available commands in chat:

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/session <name>` | Switch to a different session |
| `/quit` or `/exit` | Exit chat |

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
- The accumulated `messages` payload must stay under **200,000 characters**
  (`_MAX_TOOL_CHARS`); exceeding returns `"上下文过长，已中止。请开启新对话。"`
- After the loop, the session is compressed if it exceeds `max_history * compress_threshold`,
  triggered inside the same lock as `add_message` so concurrent requests cannot
  interleave compress and append.
- Tool execution time is reported via the `tool_result` event payload (`duration_ms`).

If you have many tools, expect the first reply to take a few seconds while the
LLM reasons about which to call.

## Streaming

The OpenAI provider supports streaming. When no tools are configured, the agent
yields tokens to the CLI as they arrive (via the `stream_token` event). When
tools are present, the agent still uses non-streaming `chat()` because it needs
to parse `tool_calls` deterministically.

## Logs

Default format:

```
2026-08-21 12:34:56 [INFO] [s1/feishu] aaagent.tools.shell: ...
```

The `[session_id/platform]` segment is filled from contextvars set by
`Application._on_message_received` so log lines are correlatable per request.

`run` mode prints to stderr; `chat` mode writes to `logs/aaagent.log` and keeps
the console quiet.

For the Feishu adapter, set `FEISHU_DEBUG=1` to enable verbose frame logging.

## Troubleshooting

- **`denied by safety policy`** — your command matched a shell deny rule. Review
  the rule list above.
- **Provider 4xx/5xx storm** — set `rate_limit.provider_rpm` to throttle.
- **Memory not persisting** — check that `data_dir` resolves to a writable
  directory. With relative paths, it resolves against `config.yaml`'s directory.
- **Feishu adapter not starting** — `app_id` / `app_secret` must be set; the
  adapter logs an error and exits if missing.
- **`logs/aaagent.log` not created in `chat` mode** — check that `logs/` is
  writable relative to the current working directory.

## Project Structure

```
src/aaagent/
├── cli.py                       # Typer entry point (run / chat)
├── core/
│   ├── app.py                   # Application orchestrator (DI-aware)
│   ├── bus.py                   # Async event bus with concurrent handlers
│   ├── logctx.py                # contextvars-based logging context
│   ├── memory.py                # MemoryStore (async, locked)
│   ├── message.py               # Unified Message dataclass
│   ├── prompt.py                # PromptBuilder (central context assembly)
│   ├── ratelimit.py             # TokenBucket
│   └── session.py               # SessionStore (per-session lock)
├── adapters/
│   ├── base.py                  # IMAdapter ABC + health_check hook
│   ├── cli_adapter.py           # CLI REPL (rich output + streaming)
│   ├── feishu.py                # Feishu WebSocket adapter
│   └── wechat.py                # placeholder
├── providers/
│   ├── base.py                  # LLMProvider ABC + chat/stream_chat + registry
│   └── openai.py                # OpenAI-compatible (chat + streaming)
└── tools/
    ├── registry.py              # ToolRegistry
    ├── file_tools.py            # read_file / write_file / list_dir / grep
    ├── shell_tools.py           # run_shell (rule-table deny list)
    └── memory_tools.py          # remember / recall
```

## Development

```bash
uv pip install -e ".[dev]"
pytest                  # run tests
```

Lint and type-check configurations live in `pyproject.toml`.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).