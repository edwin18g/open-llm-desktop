# EK Light LLM

Offline desktop chat app for **Gemma 4 E2B** running on **LiteRT-LM** (`litert-lm-api`), shown in a Chrome/Chromium app window served from a local web server (`server.py`, 127.0.0.1 only). Set `EK_BROWSER=chromium` to pick a browser; without Chrome/Chromium it opens in the default browser.

## Run

```bash
cd /var/www/html/ek-light-llm
./run.sh                # first run: creates .venv and installs deps (~150 MB)
./install-desktop.sh    # optional: adds "EK Light LLM" to your app menu
```

On first launch click **Download Gemma 4 E2B** (~2.6 GB from `litert-community/gemma-4-E2B-it-litert-lm`, resumable). It's saved to `models/` and loads automatically. You can also point it at any existing `.litertlm` file via **Choose file…**.

## Features
- Streaming replies, Stop button (`cancel_process`), New chat
- CPU / GPU backend (GPU falls back to CPU automatically), thread count, context size
- Temperature / top-p / top-k, system prompt
- Markdown + code blocks with copy, light/dark theme
- Settings saved to `~/.config/ek-light-llm/config.json`

## Features
- **Chat history**: every chat is saved (`~/.local/share/ek-light-llm/chats.db`). The sidebar groups chats by date and has Starred; each chat can be renamed, starred, moved to a project, exported to Markdown or deleted. **Ctrl+K** searches titles and message text.
- **Projects**: shared instructions plus knowledge files (PDF, DOCX, XLSX, text, code) used in every chat in the project.
- **Attachments**: the paperclip, drag-and-drop or paste. Text, code, PDF, DOCX, XLSX and CSV are read as text; images (and audio) go to Gemma's vision/audio encoders, which load only when first needed.
- **Light on memory**: after 10 idle minutes the model is unloaded (about 75 MB left in use) and reloads in under a second on the next message. Change with `EK_IDLE_UNLOAD=<seconds>` (0 turns it off).
- **Artifacts**: HTML or SVG answers open in a side panel with Preview and Code tabs, plus copy and download.
- **Messages**: edit and resend (or press ↑ in an empty input), regenerate, copy, stop (Esc). Markdown, tables and syntax-highlighted code with copy buttons.
- **Extended thinking** toggle (shows the thought process), **response styles** (Normal, Concise, Explanatory, Formal), a context-window meter, and light/dark theme.
- **Shortcuts**: Ctrl+N new chat · Ctrl+K search · Ctrl+B sidebar · Ctrl+, settings · Esc stop.

## Inline browser
The globe button at the top of a chat opens a browser panel beside it, and links in answers open there too. It is a hidden Chrome (its own profile, so sign-ins stay separate from your normal Chrome) shown live: click, scroll and type in it as usual; the arrow button opens the page in Chrome. When the **Browser** plugin is attached, Gemma drives this same Chrome, so the panel shows what it does and each step's tool card keeps a screenshot. Like any Chrome debugging port, it listens on 127.0.0.1, where other programs running as you could connect to it.

## Plugin notes
Each plugin's details page has **Notes for Gemma**: facts Gemma reads with every question while that plugin is attached (for example, which list "leads" means).

## Video from JSON
Install **Video Maker** from the Plugins page and attach it to ask Gemma for a video, or run it directly: `.venv/bin/python servers/video_server.py input.json -o out.mp4`. Plugin videos are saved in `~/Videos/EK Light`. The JSON format is at the top of [servers/video_server.py](servers/video_server.py). It needs `ffmpeg`.

## Tamil voice
Settings > Voice > Voice language > தமிழ். The next time you start voice mode it offers a one-time download of about 1 GB: Whisper "small" hears the Tamil (Gemma's own hearing garbled most of it), Gemma answers the text, and a community Piper voice (`ta_IN-roja-medium`) speaks. It all runs offline after the download and uses about 2.6 GB of RAM during a voice chat.

## Plugins (MCP)
Plugins are MCP servers. They work like attachments: Gemma only sees the tools of the plugins you attach to a chat.

- **Plugins** page: search, see what's installed (green = connected), and click **+** to install from the catalog (Files, Web Fetch, Web Search, Brave Search, GitHub, Time, MySQL, SQLite, Git, Browser, Notion, Memory, Step-by-step). **+ Add custom** takes any MCP server URL (with headers) or local command (with env vars).
- **Web Search** is built in and needs no key: it searches Bing, Wikipedia and Google News and reads the best pages. Your searches go to those sites. For a paid-grade source, use **Brave Search** with an API key.
- **In chat**: click **+** or type **@** to attach plugins. They show as chips above the input and stay attached until you remove them or start a new chat. Attaching or removing one keeps the conversation.
- **Files** (built in, no Node.js): list, tree, read, search names/text, file info, write, append, edit (exact find-and-replace), create folder, move/rename, copy, delete file, delete folder. It only works inside the folders you choose. Deletes go to the Ubuntu Trash (restorable) and always ask Allow/Deny unless you turn that off in Settings. Access can be read & write, read only, or permanent delete.
- Click an installed plugin to turn single tools on or off, reconnect, or uninstall it.
- **Settings → Tool calls**: run automatically, or ask before each call (Allow/Deny in the chat).
- Everything is stored in `~/.config/ek-light-llm/mcp_servers.json` (Claude Desktop format). You can edit it under Settings → Edit MCP config.

Tips for a 2B model: attach only what the task needs, and keep context at 8192+ tokens.

## Troubleshooting
| Problem | Fix |
|---|---|
| `python3 -m venv` fails | `sudo apt install python3-venv` |
| Blank window on Wayland | already forced to X11 via `QT_QPA_PLATFORM=xcb` in `run.sh` |
| Debug the UI | `EK_DEBUG=1 ./run.sh` (opens dev tools) |
| MCP server shows error `No such file … 'npx'`/`'uvx'` | install Node.js (`npx`) or uv (`pip install uv`) |
| Download needs auth | `HF_TOKEN=hf_xxx ./run.sh` |

## Files
- `app.py` — window, LiteRT-LM engine worker thread, model downloader
- `mcp_client.py` — MCP connections (stdio / streamable HTTP / SSE) → LiteRT-LM tools
- `plugins.py` — plugin catalog (one-click MCP servers)
- `servers/fs_server.py` — built-in Files MCP server
- `store.py` — chats, projects, attachments (SQLite)
- `attachments.py` — file text extraction & image prep
- `ui/index.html`, `ui/app.css`, `ui/app.js` — UI (`ui/vendor/` has marked, DOMPurify and highlight.js, bundled for offline use)
