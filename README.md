# Open LLM Desktop: offline AI chat app with Gemma 4, MCP plugins and voice (Linux)

**Run a private ChatGPT / Claude Desktop–style assistant fully offline on your own PC.** Open LLM Desktop (EK Light LLM) runs Google's **Gemma 4 E2B** locally with **LiteRT-LM**, with no cloud, no API key and no GPU required. It connects to any **MCP (Model Context Protocol) server** as a plugin, reads your files, browses the web, talks in **English and Tamil voice**, and even makes videos.

![Platform](https://img.shields.io/badge/platform-Linux-blue) ![Model](https://img.shields.io/badge/model-Gemma%204%20E2B-orange) ![Runtime](https://img.shields.io/badge/runtime-LiteRT--LM-green) ![MCP](https://img.shields.io/badge/MCP-client-purple) ![Offline](https://img.shields.io/badge/works-offline-success)

<!-- Add a screenshot or GIF here: it is the biggest single boost for stars. Example:
![Open LLM Desktop chatting with Gemma 4 offline, with MCP plugins attached](docs/screenshot.png) -->

## Why Open LLM Desktop?

- **100% local and private.** Chats, files and the model stay on your machine. Works without internet after the one-time model download.
- **Light.** Gemma 4 E2B (~2.6 GB) runs on an ordinary laptop CPU. After 10 idle minutes the model unloads to about 75 MB and reloads in under a second.
- **MCP plugins like Claude Desktop.** One-click Files, Web Search, Browser, GitHub, MySQL, SQLite, Git, Notion, Memory and more, or any MCP server URL or command (stdio, streamable HTTP, SSE, OAuth).
- **Voice chat in English and Tamil**, offline (Whisper + Piper TTS).
- **Free and open source.** No subscription, no account.

## Quick start

```bash
git clone https://github.com/edwin18g/open-llm-desktop.git
cd open-llm-desktop
./run.sh                # first run: creates .venv and installs deps (~150 MB)
./install-desktop.sh    # optional: adds the app to your applications menu
```

On first launch click **Download Gemma 4 E2B** (~2.6 GB from `litert-community/gemma-4-E2B-it-litert-lm`, resumable). It loads automatically. You can also point it at any existing `.litertlm` model file via **Choose file…**.

Or build a Debian/Ubuntu package: `packaging/build-deb.sh 1.5.0`, then install `dist/ek-light-llm_1.5.0_all.deb`.

**Requirements:** Linux (tested on Ubuntu), Python 3.10+, Chrome or Chromium (any browser works). Optional: Node.js for npm-based MCP servers, `ffmpeg` for video.

## Features

### Chat
- Streaming replies with stop (Esc), edit and resend, regenerate, copy
- Markdown, tables and syntax-highlighted code, light/dark theme
- **Extended thinking** toggle, response styles (Normal, Concise, Explanatory, Formal), context-window meter
- CPU / GPU backend (GPU falls back to CPU), threads, context size, temperature / top-p / top-k, system prompt
- **Artifacts:** HTML or SVG answers open in a side panel with Preview and Code tabs
- Shortcuts: Ctrl+N new chat · Ctrl+K search · Ctrl+B sidebar · Ctrl+, settings · Esc stop

### Chat history and projects
- Every chat is saved locally (SQLite), grouped by date, with starring, renaming, search and Markdown export
- **Projects:** shared instructions plus knowledge files (PDF, DOCX, XLSX, text, code) used in every chat in the project

### Attachments and vision
- Paperclip, drag-and-drop or paste. Text, code, PDF, DOCX, XLSX and CSV are read as text
- Images and audio go to Gemma's vision/audio encoders, loaded only when first needed

### MCP plugins (Model Context Protocol)
Plugins are MCP servers and work like attachments: Gemma only sees the tools of the plugins you attach to a chat (click **+** or type **@**).

- **Catalog:** Files, Web Fetch, Web Search, Brave Search, GitHub, Time, MySQL, SQLite, Git, Browser (Playwright), Notion, Memory, Step-by-step, Video Maker
- **+ Add custom:** any MCP server URL (with headers or OAuth sign-in) or local command (with env vars)
- **Files** (built in, no Node.js): list, read, search, write, edit, move, copy, delete, only inside the folders you choose. Deletes go to the Trash and ask Allow/Deny first
- **Web Search** (built in, no API key): Bing, Wikipedia and Google News, reading the best pages
- **Tool approval:** run automatically or ask before each call; destructive tools always ask
- **Plugin notes:** facts Gemma reads with every question while that plugin is attached
- Config is `~/.config/ek-light-llm/mcp_servers.json` in **Claude Desktop format**, so existing configs work

Tip for a 2B model: attach only what the task needs, and keep context at 8192+ tokens.

### Inline browser
The globe button opens a live browser panel beside the chat (a separate headless Chrome profile). When the **Browser** plugin is attached, Gemma drives that same Chrome, so you watch what it does, with a screenshot saved on each step's tool card.

### Voice (English and Tamil)
Settings > Voice > Voice language. Tamil uses a one-time ~1 GB download: Whisper "small" for speech recognition and a Piper voice (`ta_IN-roja-medium`) for replies, all offline afterwards.

### Video from JSON
Install **Video Maker** and ask Gemma for a video, or run `.venv/bin/python servers/video_server.py input.json -o out.mp4`. Scenes with text, images, clips, music and spoken narration. Needs `ffmpeg`.

## How it compares

| | Open LLM Desktop | Cloud chat apps | Ollama + web UI |
|---|---|---|---|
| Runs offline | ✅ | ❌ | ✅ |
| No API key or account | ✅ | ❌ | ✅ |
| MCP plugins built in | ✅ | Some | Add-ons |
| Tamil voice | ✅ | Partly | ❌ |
| One-command setup | ✅ | n/a | Two installs |

## FAQ

**Does it need a GPU?** No. It runs on the CPU, and uses the GPU if you choose it and it's available.

**Is my data sent anywhere?** No. Only plugins you attach (for example Web Search) reach the internet.

**Can I use another model?** Any `.litertlm` file can be loaded with **Choose file…**.

**Does it work with my Claude Desktop MCP config?** Yes, the config uses the same `mcpServers` format.

## Troubleshooting

| Problem | Fix |
|---|---|
| `python3 -m venv` fails | `sudo apt install python3-venv` |
| Debug the UI | `EK_DEBUG=1 ./run.sh` (opens dev tools) |
| MCP server error `No such file … 'npx'`/`'uvx'` | install Node.js (`npx`) or uv (`pip install uv`) |
| Model download needs auth | `HF_TOKEN=hf_xxx ./run.sh` |
| Something went wrong | see `~/.local/share/ek-light-llm/app.log` |

## Project layout
- `app.py`: LiteRT-LM engine worker, prompts, tool routing, API
- `server.py`: local web server (127.0.0.1 only, per-launch token) and app window
- `mcp_client.py`: MCP connections (stdio / streamable HTTP / SSE, OAuth) → LiteRT-LM tools
- `plugins.py`: plugin catalog
- `servers/`: built-in MCP servers (files, web search, video)
- `store.py`: chats, projects, attachments (SQLite)
- `attachments.py`: text extraction for PDF, DOCX, XLSX and more
- `voice.py`: Piper TTS and Whisper speech recognition
- `ui/`: the interface (marked, DOMPurify and highlight.js bundled for offline use)

## Contributing
Issues and pull requests are welcome. If this project is useful to you, a ⭐ helps others find it.

<!-- keywords: offline AI chat, local LLM desktop app, Gemma 4, LiteRT-LM, MCP client, Model Context Protocol, private ChatGPT alternative, Claude Desktop alternative, Linux AI assistant, Tamil voice assistant -->
