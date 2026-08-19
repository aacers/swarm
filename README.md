# Swarm

A team of AI terminals on one Mac, steered from your iPhone.

Each bot is a real [Grok](https://grok.com), [Claude](https://claude.ai/code), or ChatGPT ([Codex](https://github.com/openai/codex)) session. Mix them on the same roster.

Not a cloud chatbot. Swarm is the control plane: hidden `tmux` sessions, one live chat per bot, one Chrome per bot, and a phone UI over Wi‑Fi or 5G.

The models still do the work. Swarm is how you run several at once, without leaving the sofa.

## What you get

- **Bots** — Grok, Claude, or ChatGPT (Codex); switch per bot
- **Phone UI** — chats, Stop that cancels the current turn, live pills, loops, files
- **Overleg** — bots pass work to each other as normal user turns
- **Browser** — each bot gets its own Chrome (`gbrowse`); the phone follows the open chat
- **Loops** — a 4-hour ASO sweep, a Gmail check — bounded ticks, then stop

## This is not

- A hosted product you log into
- A wrapper that hides the model. Swarm is the roster, the queue, and the phone
- Your private farm. Tokens, chats, and `roster.json` stay in `~/.grok/imac-phone/` and are **not** in this repo

## Requirements

- macOS
- `tmux`
- At least one CLI signed in: `grok`, `claude`, and/or `codex`
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
| `agents_tmux.py` | hidden Grok / Claude / Codex sessions |
| `roster.py` | names, loops, queues |
| `static/` | iPhone / desktop UI |
| `browse_daemon.py` | per-bot Chrome (`gbrowse`) |
| `browse_ids.py` | which bot owns a browse call |

## License

MIT. Issues and PRs welcome if you are actually running it.
