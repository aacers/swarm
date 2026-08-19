# Swarm

A team of AI terminals on one Mac. Use it on the Mac, then take the same team over on your iPhone.

**Built first for [Grok Build](https://grok.com).** The work happens in those terminals. Swarm is the roster, the queue, and the remote — so you can run several Grok sessions at once without leaving the sofa.

It also runs [Claude](https://claude.ai/code), ChatGPT ([Codex](https://github.com/openai/codex)), Gemini, Aider, OpenCode, Cursor, or any other CLI on your Mac. If it lives in a terminal, it can be a Swarm bot. Each bot has its own Chrome.

## What you get

- **Bots** — Grok Build first; also Claude, ChatGPT, Gemini, or any other terminal CLI. Each one has its own browser.
- **Phone UI** — chats, Stop that cancels the current turn, live pills, loops, files
- **Overleg** — bots pass work to each other as normal user turns
- **Browser** — each bot gets its own Chrome (`gbrowse`); the phone follows the open chat
- **Device lab** — Swarm tests iOS (and later Android) apps via `glab` / `/api/lab` without grabbing the Mac mouse
- **Loops** — a 4-hour ASO sweep, a Gmail check — bounded ticks, then stop

## This is not

- A hosted product you log into
- A wrapper that hides the model. Swarm is the roster, the queue, and the phone
- Your private farm. Tokens, chats, and `roster.json` stay in `~/.grok/imac-phone/` and are **not** in this repo

## Requirements

- macOS
- `tmux`
- At least one AI CLI in PATH (`grok`, `claude`, `codex`, `gemini`, …)
- Python 3.11+ with PyObjC (`Quartz` + `AppKit`)

## Run (one script)

```bash
git clone https://github.com/aacers/swarm.git
cd swarm
./install.sh
```

That is the whole setup: `tmux` + Python if needed, a venv, then Swarm starts. No extra config file. Use it in the Mac window, or open the **Wi‑Fi** URL on your iPhone and take the same bots over. Tap **+ Bot** → pick Grok (or another CLI) → send. Sign-in for that CLI happens in Swarm. If no CLI is installed yet, Swarm still opens — add one and tap **+ Bot**.

Per-bot Chrome: `./install.sh --browser`

Stuck? **[Getting started](docs/GETTING_STARTED.md)**.

## Layout

| Path | Role |
|---|---|
| `server.py` | HTTP UI + APIs |
| `agents_tmux.py` | hidden Grok / Claude / Codex sessions |
| `roster.py` | names, loops, queues |
| `static/` | iPhone / desktop UI |
| `browse_daemon.py` | per-bot Chrome (`gbrowse`) |
| `device_lab.py` | iOS Simulator lab (`glab`, `:8793`) |
| `browse_ids.py` | which bot owns a browse call |

## License

MIT. Issues and PRs welcome if you are actually running it.
