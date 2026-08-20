# aaagent

A pluggable IM + LLM agent framework.

## Installation

```bash
pip install -e .
```

## Configuration

Copy `.env.example` to `.env` and fill in your API keys:

```bash
cp .env.example .env
```

Edit `config.yaml` to configure providers and adapters.

### Providers

Each provider requires a `type` field. Currently supported types:

- `openai_compatible` — OpenAI-compatible API (covers OpenAI, DeepSeek, Qwen, etc.)
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

- `api_key`: Supports `${ENV_VAR}` syntax to read from environment variables (loaded from `.env`)
- `base_url`: Override API endpoint for OpenAI-compatible services. Leave empty for official OpenAI endpoint

### Adapters

```yaml
adapters:
  feishu:
    enabled: false
    app_id: ""
    app_secret: ""
  wechat:
    enabled: false
    token: ""
```

### Session

```yaml
session:
  max_history: 20
  compress_threshold: 0.8
```

- `max_history`: Max messages kept per session (sliding window)
- `compress_threshold`: Trigger summary compression when message count reaches `max_history * compress_threshold`

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

## Project Structure

```
src/aaagent/
├── cli.py              # CLI entry point (typer)
├── core/
│   ├── app.py          # Application main class
│   ├── bus.py          # Event bus
│   ├── message.py      # Unified message model
│   └── session.py      # Multi-session + sliding window + summary compression
├── adapters/
│   ├── base.py         # IMAdapter ABC
│   ├── cli_adapter.py  # CLI virtual adapter
│   ├── feishu.py       # Feishu adapter (skeleton)
│   └── wechat.py       # WeChat adapter (skeleton)
└── providers/
    ├── base.py         # LLMProvider ABC + type registry
    └── openai.py       # OpenAI-compatible provider
```
