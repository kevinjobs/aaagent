# Changelog

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

## 0.1.0 - 2026-08-21

- Initial release