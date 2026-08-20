from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("aaagent.tools.file")


def _ensure_allowed(path: str, allowed_dirs: list[str] | None) -> str:
    resolved = Path(path).resolve()
    if not allowed_dirs:
        return str(resolved)
    for d in allowed_dirs:
        try:
            Path(d).resolve().relative_to(resolved)
        except ValueError:
            try:
                resolved.relative_to(Path(d).resolve())
            except ValueError:
                continue
            return str(resolved)
        return str(resolved)
    raise PermissionError(f"Path '{path}' is not in allowed directories: {allowed_dirs}")


async def read_file(args: dict[str, Any], allowed_dirs: list[str] | None) -> str:
    path = _ensure_allowed(args["path"], allowed_dirs)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        return content
    except FileNotFoundError:
        return f"Error: file not found: {path}"
    except IsADirectoryError:
        return f"Error: path is a directory: {path}"
    except PermissionError:
        return f"Error: permission denied: {path}"
    except Exception as e:
        return f"Error reading file: {e}"


async def write_file(args: dict[str, Any], allowed_dirs: list[str] | None) -> str:
    path = _ensure_allowed(args["path"], allowed_dirs)
    content = args["content"]
    try:
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} bytes to {path}"
    except PermissionError:
        return f"Error: permission denied: {path}"
    except Exception as e:
        return f"Error writing file: {e}"


async def list_dir(args: dict[str, Any], allowed_dirs: list[str] | None) -> str:
    path = _ensure_allowed(args["path"], allowed_dirs)
    try:
        entries = os.listdir(path)
        lines: list[str] = []
        for entry in sorted(entries):
            full = Path(path) / entry
            suffix = "/" if full.is_dir() else ""
            lines.append(f"{entry}{suffix}")
        return "\n".join(lines) if lines else "(empty directory)"
    except FileNotFoundError:
        return f"Error: directory not found: {path}"
    except NotADirectoryError:
        return f"Error: not a directory: {path}"
    except PermissionError:
        return f"Error: permission denied: {path}"
    except Exception as e:
        return f"Error listing directory: {e}"


async def grep_files(args: dict[str, Any], allowed_dirs: list[str] | None) -> str:
    root = _ensure_allowed(args.get("path", "."), allowed_dirs)
    pattern = args["pattern"]
    include = args.get("include", "*")
    try:
        import fnmatch
        import re

        matches: list[str] = []
        regex = re.compile(pattern, re.IGNORECASE)
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if not fnmatch.fnmatch(fn, include):
                    continue
                full = Path(dirpath) / fn
                try:
                    with open(full, encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            if regex.search(line):
                                rel = full.relative_to(Path(root).resolve())
                                matches.append(f"{rel}:{lineno}:{line.rstrip()}")
                                if len(matches) >= 100:
                                    break
                    if len(matches) >= 100:
                        break
                except (OSError, UnicodeDecodeError):
                    continue
            if len(matches) >= 100:
                break

        if not matches:
            return f"No matches found for '{pattern}' in {root}"
        return "\n".join(matches[:100])
    except Exception as e:
        return f"Error during search: {e}"


def register_file_tools(registry: Any) -> None:
    allowed_dirs = registry.allowed_dirs

    async def _read_file(args: dict[str, Any]) -> str:
        return await read_file(args, allowed_dirs)

    async def _write_file(args: dict[str, Any]) -> str:
        return await write_file(args, allowed_dirs)

    async def _list_dir(args: dict[str, Any]) -> str:
        return await list_dir(args, allowed_dirs)

    async def _grep_files(args: dict[str, Any]) -> str:
        return await grep_files(args, allowed_dirs)

    registry.register(
        name="read_file",
        description="Read the contents of a file. Use this when you need to view the content of a specific file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read",
                },
            },
            "required": ["path"],
        },
        handler=_read_file,
    )

    registry.register(
        name="write_file",
        description="Write content to a file. Creates parent directories if they don't exist.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path where the file should be written",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        },
        handler=_write_file,
    )

    registry.register(
        name="list_dir",
        description="List files and directories in a given directory path.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list",
                },
            },
            "required": ["path"],
        },
        handler=_list_dir,
    )

    registry.register(
        name="grep",
        description="Search for a regex pattern in files under a directory.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression pattern to search for",
                },
                "include": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.py', '*.{ts,tsx}')",
                },
                "path": {
                    "type": "string",
                    "description": "Root directory to search in (default: current dir)",
                },
            },
            "required": ["pattern"],
        },
        handler=_grep_files,
    )
