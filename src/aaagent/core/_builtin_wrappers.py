"""Builtin plugin class lookup for in-tree fallback.

When plugins are installed via `pip install -e plugins/<x>`, Python's
entry_points discovery in `PluginManager._load_entry_points()` already
picks them up. The BUILTIN_* dicts below provide a fallback for
development scenarios where the plugin source is in-tree but not yet
installed (e.g., working in `plugins/aaagent-plugin-foo/` before
running `uv pip install -e .`).
"""

from __future__ import annotations


BUILTIN_PROVIDERS: dict[str, str] = {
    "openai_compatible": "aaagent_plugin_openai:OpenAICompatibleProvider",
}

BUILTIN_ADAPTERS: dict[str, str] = {
    "cli": "aaagent_plugin_cliadapter:CliAdapter",
    "feishu": "aaagent_plugin_feishu:FeishuAdapter",
}

BUILTIN_TOOLS: dict[str, str] = {
    "file": "aaagent_plugin_filetools:FileToolsPlugin",
    "shell": "aaagent_plugin_shelltools:ShellToolsPlugin",
    "memory": "aaagent_plugin_memorytools:MemoryToolsPlugin",
    "skills": "aaagent_plugin_skills:SkillsPlugin",
}

BUILTIN_SESSIONS: dict[str, str] = {
    "inmemory": "aaagent_plugin_inmemorysession:InMemorySessionFactory",
}

BUILTIN_MEMORIES: dict[str, str] = {
    "markdown": "aaagent_plugin_markdownstore:MarkdownMemoryStoreFactory",
}