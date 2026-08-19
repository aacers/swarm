# Swarm

A team of [Grok](https://grok.com) terminals on one Mac, steered from your iPhone.

Not a cloud chatbot. Swarm is the control plane: hidden `tmux` sessions, one live chat per bot, a shared browser, and a phone UI over Wi‑Fi or 5G.

Built because the work happens in Grok Build — Swarm is how you run several of those at once, without leaving the sofa.

## What you get

- **Bots** — each specialist is a real Grok (or Claude / Codex) session
- **Phone UI** — chats, live “busy” pills, loops, drag-and-drop files
- **Overleg** — bots pass work to each other as normal user turns
- **Browser** — one shared Chrome window the team and you both see
- **Loops** — a 4-hour ASO sweep, a Gmail check — bounded ticks, then stop

## This is not

- A hosted product you log into
- A wrapper that hides Grok. The model still does the work; Swarm is the roster, the queue, and the phone
- Your private farm. Tokens, chats, and `roster.json` stay in `~/.grok/imac-phone/` and are **not** in this repo

## Requirements

- macOS
- [Grok CLI](https://grok.com) (`grok`) signed in
- `tmux`
- Python 3.11+ with PyObjC (`Quartz`) — the desktop-harness venv works

## Run

```bash
git clone https://github.com/aacers/swarm.git
cd swarm
./bin/imac-phone
```

Open the URL it prints (same Wi‑Fi, or your own HTTPS tunnel). A key is written to `~/.grok/imac-phone/token`. Do not commit that file.

## Layout

| Path | Role |
|---|---|
| `server.py` | HTTP UI + APIs |
| `agents_tmux.py` | hidden Grok sessions |
| `roster.py` | names, loops, queues |
| `static/` | iPhone / desktop UI |
| `browse_daemon.py` | shared browser |

## License

MIT. Issues and PRs welcome if you are actually running it.
