# Getting started

Usual path — one command after clone:

```bash
./install.sh
```

Add `--browser` for a Chrome per bot. Below is the manual version if that script cannot run.

Swarm was built for **Grok Build** first. You work on the Mac; the phone takes the same team over. Other terminal CLIs (Claude, ChatGPT, Gemini, …) can be bots too.

## 1. macOS tools

```bash
brew install tmux python@3.11
```

## 2. Clone and Python

```bash
git clone https://github.com/aacers/swarm.git
cd swarm
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`Quartz` / `AppKit` are required (window list + desktop). Without them the server will not start.

## 3. At least one model CLI

Install and sign in **one** of:

| Bot type | CLI | Install |
|---|---|---|
| Grok | `grok` | [grok.com](https://grok.com) → Grok CLI |
| Claude | `claude` | [Claude Code](https://claude.ai/code) |
| ChatGPT | `codex` | [Codex CLI](https://github.com/openai/codex) |

Check:

```bash
which grok claude codex
```

## 4. Optional: per-bot browser

Each bot can have its own Chrome. That needs Playwright:

```bash
pip install playwright
playwright install chromium
```

Then in another terminal:

```bash
python browse_daemon.py
```

Chats still work if you skip this. Bots just cannot drive a browser.

## 5. Start Swarm

```bash
./bin/imac-phone
```

It prints two URLs:

- **Same Wi‑Fi** — phone on the same network
- **5G / anywhere** — only if you have a public tunnel; otherwise ignore it and use the Wi‑Fi link

A key is stored in `~/.grok/imac-phone/token`. That folder is **yours**, not the git repo. Do not commit it.

Open the Wi‑Fi URL on your iPhone (Safari). Add to Home Screen if you want it as an app.

## 6. First bot

1. Tap **+ Bot**
2. Pick Grok, Claude, or ChatGPT
3. Wait until the row is idle
4. Open the chat, type, send
5. Tap the name to rename (CEO, Docs, …)

Each bot is a hidden `tmux` session. Stop in the UI cancels the current turn; it does not kill the bot.

## 7. Phone basics

- **Bots** — roster. Green / busy pill = that bot is working
- **Chat** — one thread per bot. Extra agents and queues live here
- **Browser** — follows the bot whose chat you have open (own Chrome per bot)
- **Memory** — short shared notes (`SHARED.md` in each workspace)
- **Settings** — theme, which features are on

Bots can pass work to each other (**Overleg**) if that bot is on the roster by name.

## Loops

On a bot, the loop button schedules a repeating prompt (for example every 4 hours). Keep the prompt short and make it **stop** after one bounded tick, or it will burn quota.

## If it does not start

| Symptom | Check |
|---|---|
| `Quartz` / `AppKit` import error | venv + `pip install -r requirements.txt` |
| `geen sessie` / no live chat | `tmux ls`; CLI in PATH (`which grok`) |
| Phone cannot open the page | same Wi‑Fi; Mac firewall; use the `http://192.168…:8790/?k=…` line |
| Bot row appears then dies | that AI CLI is not installed or not logged in |
| No browser | `browse_daemon.py` not running, or Playwright missing |

## What is not included

Your chats, bot names, API keys, and `roster.json` live under `~/.grok/` on each machine. Cloning Swarm does not copy someone else’s team.
