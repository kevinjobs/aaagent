# Plugin Authoring Guide

This guide explains how to write a plugin for `aaagent`.

## Plugin types

There are six plugin types, each registered under a specific entry-point group:

| Type | Group | Purpose |
|---|---|---|
| `Provider` | `aaagent.providers` | LLM provider |
| `ToolPlugin` | `aaagent.tools` | One or more tools callable by the agent |
| `IMAdapter` | `aaagent.adapters` | IM channel |
| `SessionStoreFactory` | `aaagent.sessions` | Per-session history store |
| `MemoryStoreFactory` | `aaagent.memories` | Long-term memory store |
| Command registrar (function) | `aaagent.commands` | Slash-command bundle |

The core has **no built-in registry of plugin classes** — it discovers
everything via `importlib.metadata.entry_points` and (optionally) the
explicit `plugins:` block in `config.yaml`. To ship a feature by
default, install a plugin package; to remove a feature, uninstall it.

## Naming convention

- Folder name (kebab-case): `aaagent-plugin-<name>/`
- Python package (snake_case): `aaagent_plugin_<name>/`
- Distribution name (kebab-case): `aaagent-plugin-<name>`

## Minimal layout

```
aaagent-plugin-filetools/
├── pyproject.toml
└── src/
    └── aaagent_plugin_filetools/
        └── __init__.py
```

## pyproject.toml template

```toml
[project]
name = "aaagent-plugin-<name>"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "aaagent",
    # your plugin-specific deps
]

[project.entry-points."aaagent.<type>s"]
<entry_name> = "aaagent_plugin_<name>:<ClassName>"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/aaagent_plugin_<name>"]
```

`aaagent.tools` is the group for `ToolPlugin` plugins,
`aaagent.adapters` for `IMAdapter`, etc.

## Protocol quick reference

```python
from abc import ABC, abstractmethod

class Provider(ABC):
    type: str
    def __init__(self, config: dict) -> None: ...
    @abstractmethod
    async def chat(self, messages, tools=None, **kwargs) -> ChatResponse: ...
    async def stream_chat(self, messages, **kwargs) -> AsyncIterator[str]: ...
    def is_retryable_error(self, exc: BaseException) -> bool: ...

class ToolPlugin(ABC):
    name: str
    @abstractmethod
    def register(self, registry, config: dict) -> None: ...

class IMAdapter(ABC):
    name: str
    def __init__(self, config: dict, bus: EventBus) -> None: ...
    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...
    @abstractmethod
    async def send(self, msg: Message) -> None: ...
    async def health_check(self) -> bool: return True

class SessionStoreFactory(ABC):
    name: str
    @abstractmethod
    def create(self, config: dict) -> "SessionStore": ...

class MemoryStoreFactory(ABC):
    name: str
    @abstractmethod
    def create(self, config: dict) -> "MemoryStore": ...
```

## Example: a Tool plugin

```python
# src/aaagent_plugin_filetools/__init__.py
from aaagent.core.plugin import ToolPlugin

class FileToolsPlugin(ToolPlugin):
    name = "file"

    def register(self, registry, config):
        async def read_file(args):
            ...
        registry.register(
            name="read_file",
            description="Read a file.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            handler=read_file,
        )
```

```toml
# pyproject.toml
[project.entry-points."aaagent.tools"]
file = "aaagent_plugin_filetools:FileToolsPlugin"
```

Tool plugins that need async setup (e.g. open an MCP or database connection)
or cleanup (close a connection pool) can override the optional hooks on the
`ToolPlugin` protocol. `Application.run()` calls `establish(registry, config)`
once after synchronous `register()` and before adapters start; `close()` is
called at `Application.stop()`. Both have no-op defaults so simple plugins
don't need them.

```python
class McpToolsPlugin(ToolPlugin):
    name = "mcp"

    async def establish(self, registry, config):
        # open transport, list_tools, register expanded tools
        ...

    async def close(self):
        # release transport / child process
        ...
```

### Tool plugins that need framework state (memory / bus / project_root)

Override the `set_context(ctx)` hook. The framework hands each tool
plugin a single, immutable `PluginContext` handle once, before
`register()`. Plugins read what they declared they need from it and
must not reach back into the `Application` object.

```python
from aaagent.core.plugin import PluginContext, ToolPlugin

class MemoryToolsPlugin(ToolPlugin):
    name = "memory"

    def __init__(self) -> None:
        self._memory_store = None

    def set_context(self, ctx: PluginContext) -> None:
        # Replace the legacy `set_memory` / `set_application` probes.
        # Plugins should only read fields they care about.
        self._memory_store = ctx.memory_store

    def register(self, registry, config):
        # ... use self._memory_store in your handlers
```

`PluginContext` fields:

| Field | Description |
|---|---|
| `event_bus` | the framework's `EventBus` (slash_reply, tool_result, ...) |
| `session_store` | the configured `SessionStore` |
| `memory_store` | the configured `MemoryStore` (may be `None` if no plugin installed) |
| `project_root` | absolute `Path` to the directory containing `config.yaml` |
| `config` | the parsed `config.yaml` dict (read-only contract) |

Adding a new plugin-visible capability means adding a field to
`PluginContext`. Plugins that need more (e.g. an LLM router, a config
writer) should propose the field rather than poking private attributes.

## Example: a Provider plugin

```python
class MyProvider(Provider):
    type = "my_provider"

    def __init__(self, config):
        super().__init__(config)
        # ... your setup

    async def chat(self, messages, tools=None, **kwargs):
        # call your backend
        return ChatResponse(content="...")

    def is_retryable_error(self, exc):
        # Return True to let Application._chat_with_fallback try the next
        # provider in the chain; False aborts the chain immediately. The
        # base implementation in aaagent.core.plugin.Provider covers
        # network / OS-level errors plus a conservative HTTP substring
        # sweep; override here for SDK-specific named exceptions.
        if isinstance(exc, MySdkThrottle):
            return True
        return super().is_retryable_error(exc)
```

```toml
# pyproject.toml
[project.entry-points."aaagent.providers"]
my_provider = "aaagent_plugin_myname:MyProvider"
```

## Example: an AgentLoop plugin

The per-request "agent thinks" loop is itself pluggable. The core
ships `DefaultAgentLoop` (tool iteration + provider fallback). To
ship an alternative loop (plan-and-execute, tree-of-thought, agent-as-
tool, ...) subclass `AgentLoop`:

```python
from aaagent.core.agent_loop import AgentContext, AgentLoop

class TreeOfThoughtLoop(AgentLoop):
    async def handle_message(self, message, context: AgentContext) -> str:
        # `context` carries everything you need:
        #   context.messages, context.tools, context.profile,
        #   context.session_id, context.platform, context.chat_id
        # `self._app` (set by Application.__init__) gives you access to
        # the live provider chain via _provider_order /
        # _chat_with_fallback / _stream_or_chat if you want to delegate.
        ...
```

Install it on the application:

```python
from aaagent import Application
from my_plugin import TreeOfThoughtLoop

app = Application(agent_loop=TreeOfThoughtLoop(app_or_factory=...))
```

The loop never replaces the bus events the rest of the application
relies on (`message_to_send`, `tool_start`, `tool_result`, ...). It
just decides how the assistant reply is produced.

## Example: a Slash-command plugin

Slash commands live in plugins too. The `aaagent.commands` entry-point
group expects a callable `(app: Application) -> None` that registers
one or more commands on `app.commands`:

```python
from aaagent.core.commands import SlashContext, SlashResult

async def _session_handler(arg: str, ctx: SlashContext) -> SlashResult:
    return SlashResult(reply=f"new session for {ctx.platform}")

def register(app):
    app.commands.register(
        "/newsession",
        description="Start a new session",
        handler=_session_handler,
        source="my-plugin-name",
    )
```

```toml
[project.entry-points."aaagent.commands"]
my_commands = "aaagent_plugin_mycommands:register"
```

`Application.__init__` calls each registered registrar after the core's
own `register_builtins()`. The `/help` output attributes each command to
its `source`.

## config.yaml integration

After installing the plugin, reference it in `config.yaml`:

```yaml
providers:
  my_named_instance:
    type: my_provider      # matches Provider.type / entry-point name
    enabled: true
    # arbitrary plugin-specific fields
    api_key: "${MY_API_KEY}"
```

## Discovery layers

When `Application.__init__` runs, `PluginManager.load()` performs:

1. **Python entry points**: every installed package that declares an
   entry point under one of the six groups.
2. **Config overrides**: explicit declarations under `plugins:` in
   `config.yaml`:
   ```yaml
   plugins:
     - kind: provider
       type: my_provider
       class: my_pkg.sub:MyProvider
   ```

If a plugin is requested by config but not registered anywhere, the manager raises `PluginNotFoundError` with an install hint:

```
No provider plugin for type 'my_provider'. Install one with
`pip install aaagent-plugin-<provider-name>`.
```

## Runtime validation

`PluginManager._validate_all()` checks each registered class for:

- `Provider`: has `type` (non-empty string) and `chat` (callable).
- `ToolPlugin`: has `register` (callable).
- `IMAdapter`: has `start`, `stop`, `send` (callable).
- Session/Memory factories: has `create` (callable).
- Command registrars: are callable (any callable is accepted).

Failures raise `PluginValidationError` at startup.

## Local development workflow

The `aaagent` repository is a uv workspace that bundles every in-tree
plugin under `plugins/`. To make local plugin changes effective, run:

```bash
uv sync --all-packages
```

This installs every workspace member into the project's `.venv` and
re-registers their entry points.

To publish a single plugin as a standalone PyPI package, copy its
directory to a new repo, add a `pyproject.toml` (use the template
above), and `uv publish`.

## Tool plugins with dependencies (e.g. memory store)

If your tool plugin needs access to the configured `MemoryStore`,
implement the optional `set_memory()` hook:

```python
class MemoryToolsPlugin(ToolPlugin):
    name = "memory"

    def __init__(self):
        self._memory_store = None

    def set_memory(self, store):
        self._memory_store = store

    def register(self, registry, config):
        # ... use self._memory_store in your handlers
```

`Application._setup_tool_registry` calls `plugin.set_memory(self._memory)`
before `plugin.register(...)` when the attribute exists.

## See also

- [`aaagent.core.plugin`](../../src/aaagent/core/plugin.py) — protocol definitions and `PluginManager`
- [`aaagent.core.envutil`](../../src/aaagent/core/envutil.py) — `${ENV_VAR}` resolver
- Existing plugins in `plugins/` for end-to-end examples