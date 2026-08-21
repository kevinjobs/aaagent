from __future__ import annotations

import json
import logging
from typing import Any, Callable, Coroutine

Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, str]]

logger = logging.getLogger("aaagent.tools")


class ToolRegistration:
    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Handler,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler

    @property
    def openai_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, allowed_dirs: list[str] | None = None) -> None:
        self._tools: dict[str, ToolRegistration] = {}
        self.allowed_dirs = allowed_dirs

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Handler,
    ) -> None:
        if name in self._tools:
            logger.warning("Tool '%s' already registered, overwriting", name)
        self._tools[name] = ToolRegistration(name, description, parameters, handler)

    def get(self, name: str) -> ToolRegistration | None:
        return self._tools.get(name)

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [t.openai_definition for t in self._tools.values()]

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    async def execute(self, name: str, arguments: str) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'"

        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as e:
            return f"Error: invalid arguments JSON for '{name}': {e}"

        try:
            result = await tool.handler(args)
            return result
        except Exception as e:
            logger.exception("Tool '%s' execution failed", name)
            return f"Error executing tool '{name}': {e}"