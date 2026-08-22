# aaagent-plugin-web

Browser-based chat surface for aaagent, styled after DeepSeek
Harness.

After `pip install aaagent-plugin-web` you get a new top-level CLI
subcommand, `aaagent web`, that boots aaagent with a browser
chat UI. The UI is a React SPA served by FastAPI in the same
process; everything else (LLM providers, tools, scheduler, memory,
…) comes from your existing config.

## Install

```bash
pip install aaagent-plugin-web
```

Or, if you use `aaagent[default]` extras, add `aaagent-plugin-web`
to the list in your project's `pyproject.toml`.

## Use

```bash
aaagent web            # serves on http://127.0.0.1:8848
aaagent web --port 9000
aaagent web --no-open  # don't try to pop a browser
```

`config.yaml` settings (CLI flags win):

```yaml
adapters:
  web:
    host: 127.0.0.1
    port: 8848
    open_browser: true
    default_session_id: web-default
    default_chat_id: web-default
    default_user_id: web-user
```

The `enabled` flag under `adapters.web` is intentionally ignored —
the `aaagent web` command is itself the opt-in.

## What's bundled

| Layer | What |
| --- | --- |
| Backend | FastAPI app, WebSocket endpoint at `/api/ws`, health probe at `/api/health`. |
| Adapter | `WebAdapter(IMAdapter)` bridges the EventBus to every connected client. |
| Frontend | React 18 + TypeScript + Vite + Tailwind + shadcn/ui. DSH palette, no DSH code. |
| Build artefact | `web/dist/` — vendored into the wheel. End users do not need npm. |

## Wire protocol (WebSocket frames)

Server → client (one JSON frame per event):

* `{type: "message", role, content, session_id, chat_id, message_id}`
* `{type: "stream_token", content}`
* `{type: "tool_start", turn, tool_calls: [{name, arguments}]}`
* `{type: "tool_result", tool_call_id, tool_name, arguments, result, duration_ms, turn}`
* `{type: "slash_reply", reply}`
* `{type: "slash_unknown", text}`
* `{type: "slash_session_switch", new_session}`
* `{type: "slash_quit"}`

Client → server:

* `{type: "user_message", content, session_id?, chat_id?, user_id?}`
* `{type: "slash", text}`
* `{type: "ping"}` → server replies with `{type: "pong"}`

## Development

The plugin ships a built `web/dist/`. To iterate on the SPA:

```bash
cd plugins/aaagent-plugin-web/web
npm install
npm run dev          # Vite dev server on :5173, proxies /api → :8848
```

Then start the backend in another terminal:

```bash
aaagent web --port 8848 --no-open
```

… and edit files in `web/src/`. Vite HMR reflects changes in the
browser instantly.

To ship a fresh build:

```bash
npm run build        # outputs to web/dist/
```

Re-install the plugin (`uv pip install -e plugins/aaagent-plugin-web`)
to pick up the new dist, or commit `web/dist/` to your fork if
that's how you publish.

## Limitations (MVP)

* Single session per browser tab. Multi-session sidebar is on the
  roadmap.
* No file/image attachments, no settings panel, no plan mode.
* The fallback HTML page is served if `web/dist/index.html` is
  missing (e.g. a wheel built without running `npm run build`); it
  tells the developer how to build the frontend.

## Layout

```
plugins/aaagent-plugin-web/
├── pyproject.toml
├── src/aaagent_plugin_web/
│   ├── __init__.py         # register_cli_command, _run_web
│   ├── adapter.py          # WebAdapter(IMAdapter)
│   └── server.py           # FastAPI app factory + uvicorn bootstrap
├── tests/test_web_adapter.py
└── web/
    ├── package.json
    ├── tsconfig.json
    ├── vite.config.ts
    ├── tailwind.config.js
    ├── index.html
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── components/
        │   ├── ChatView.tsx
        │   ├── Composer.tsx
        │   ├── MessageBubble.tsx
        │   ├── ToolCallCard.tsx
        │   └── ui/button.tsx
        ├── hooks/useWebSocket.ts
        ├── lib/utils.ts
        └── styles/tokens.css
```
