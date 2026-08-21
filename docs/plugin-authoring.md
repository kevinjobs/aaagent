# Plugin Authoring Guide

This guide explains how to write a plugin for `aaagent`.

## Plugin types

There are five plugin types, each registered under a specific entry-point group:

| Type | Group | Purpose |
|---|---|---|
| `Provider` | `aaagent.providers` | LLM provider |
| `ToolPlugin` | `aaagent.tools` | One or more tools callable by the agent |
| `IMAdapter` | `aaagent.adapters` | IM channel |
| `SessionStoreFactory` | `aaagent.sessions` | Per-session history store |
| `MemoryStoreFactory` | `aaagent.memories` | Long-term memory store |

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
```

```toml
[project.entry-points."aaagent.providers"]
my_provider = "aaagent_plugin_myname:MyProvider"
```

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

1. **Builtin registry**: in-tree classes registered in `aaagent.core._builtin_wrappers`.
2. **Python entry points**: every installed package that declares an entry point under one of the five groups.
3. **Config overrides**: explicit declarations under `plugins:` in `config.yaml`:
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

Failures raise `PluginValidationError` at startup.

## Local development workflow

The `aaagent` repository is a uv workspace that bundles all 8 in-tree
plugins under `plugins/`. To make local plugin changes effective, run:

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