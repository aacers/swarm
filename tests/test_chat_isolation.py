#!/usr/bin/env python3
"""Chats must never leak between bots. Run: python3 tests/test_chat_isolation.py"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import roster as rosterlib  # noqa: E402
import server  # noqa: E402


def _roster(agents: dict, ceo: str = "", home: str = "") -> dict:
    return {"ceo": ceo, "home": home, "agents": agents, "order": []}


class IsolationUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.roster_file = root / "roster.json"
        self.nicks_file = root / "nicknames.json"
        self.agents_dir = root / "agents"
        self.agents_dir.mkdir()
        rosterlib.ROOT = root
        rosterlib.ROSTER_FILE = self.roster_file
        rosterlib.NICK_FILE = self.nicks_file
        rosterlib.AGENTS_DIR = self.agents_dir
        server._last_typed.clear()
        server._stopped_until.clear()
        server._busy_since.clear()
        server._busy_idle_since.clear()

    def test_persist_refuses_taken_session(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {"label": "A", "session_id": "sid-a", "window_id": 1},
                    "bot-b": {"label": "B", "window_id": 2},
                }
            )
        )
        ok = server._persist_session("bot-b", "sid-a")
        self.assertFalse(ok)
        rost = rosterlib.load_roster()
        self.assertEqual(rost["agents"]["bot-a"]["session_id"], "sid-a")
        self.assertFalse(rost["agents"]["bot-b"].get("session_id"))

    def test_persist_keeps_own_session(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A", "session_id": "sid-a"}}))
        self.assertTrue(server._persist_session("bot-a", "sid-a"))

    def test_nicks_do_not_assign_by_label(self):
        rosterlib.save_nicks(
            {
                "by_window": {"1": {"label": "CEO"}},
                "by_session": {"sid-ceo": {"label": "CEO"}},
            }
        )
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {"label": "CEO", "session_id": "sid-ceo", "window_id": 1},
                    "bot-b": {"label": "CEO", "window_id": 2, "auto": False},
                }
            )
        )
        rost = rosterlib.apply_nicks(rosterlib.load_roster())
        self.assertEqual(rost["agents"]["bot-a"]["session_id"], "sid-ceo")
        self.assertNotEqual(rost["agents"]["bot-b"].get("session_id"), "sid-ceo")

    def test_busy_clock_survives_idle_flicker_and_reopen(self):
        server._busy_since.clear()
        server._busy_idle_since.clear()
        t0 = server.track_busy("bot-a", True, hint=time.time() - 90)
        self.assertIsNotNone(t0)
        self.assertAlmostEqual(time.time() - t0, 90, delta=0.5)
        # Closing the chat is just another poll: still busy → same start.
        t1 = server.track_busy("bot-a", True)
        self.assertEqual(t0, t1)
        # Title flicker must not restart the clock.
        t2 = server.track_busy("bot-a", False)
        self.assertEqual(t0, t2)
        t3 = server.track_busy("bot-a", True)
        self.assertEqual(t0, t3)
        payload = server.busy_payload("bot-a", True)
        self.assertGreaterEqual(payload["busy_for"], 89)
        self.assertEqual(payload["busy_since"], t0)

    def test_last_submit_hold_marks_window_busy(self):
        server._busy_since.clear()
        server._busy_idle_since.clear()
        server._stopped_until.clear()
        now = time.time()
        self.assertTrue(server.last_submit_hold({"last_submit_at": now}, now))
        self.assertTrue(server.last_submit_hold({"last_submit_at": now - 3}, now))
        self.assertFalse(server.last_submit_hold({"last_submit_at": now - 7}, now))
        self.assertFalse(server.last_submit_hold({}, now))
        rosterlib.save_roster(
            _roster({"bot-x": {"label": "X", "window_id": 974103, "last_submit_at": now}})
        )
        wins = server.attach_busy_times(
            [{"id": 974103, "title": "X — grok", "busy": False, "activity": "Klaar"}]
        )
        self.assertTrue(wins[0]["busy"])
        self.assertEqual(wins[0]["activity"], "Busy")
        self.assertEqual(wins[0]["slug"], "bot-x")

    def test_live_progress_title_spinner_is_waiting(self):
        out = server.live_progress(
            "", "", "timgrootes — ⠹ - Preparing search_replace (3)… - grok", ""
        )
        self.assertTrue(out["waiting"])
        self.assertEqual(out["activity"], "Writing")
        self.assertIn("search_replace", (out.get("detail") or "").lower() + (out.get("pane") or "").lower())

    def test_live_progress_idle_title_is_klaar(self):
        out = server.live_progress("", "", "timgrootes — SEO hacks degero.nl - grok", "")
        self.assertFalse(out["waiting"])
        self.assertEqual(out["activity"], "Ready")

    def test_terminal_tabs_timeout_keeps_cache(self):
        import subprocess as sp

        server._tty_cache = (0.0, [{"id": 1, "tty": "/dev/ttys001", "title": "x"}])
        with mock.patch.object(server.subprocess, "run", side_effect=sp.TimeoutExpired("osascript", 2)):
            tabs = server.terminal_tabs()
        self.assertEqual(tabs[0]["tty"], "/dev/ttys001")

    def test_live_session_id_for_tty_reads_ps(self):
        server._tty_sid_cache.clear()
        fake = mock.Mock(returncode=0, stdout="ttys001 /usr/bin/grok --session-id abc-123 --always-approve\n")
        with mock.patch.object(server, "tty_of_pid", return_value=""):
            with mock.patch.object(server.subprocess, "run", return_value=fake):
                sid = server.live_session_id_for_tty("/dev/ttys001")
        self.assertEqual(sid, "abc-123")

    def test_interrupt_chat_sends_escape_to_tmux(self):
        rosterlib.save_roster(
            _roster({"bot-a": {"label": "A", "tmux": "heavy-bot-a", "session_id": "sid-a"}})
        )
        hit = []
        with mock.patch.object(
            server.agents_tmux, "list_sessions", return_value=[{"tmux": "heavy-bot-a"}]
        ):
            with mock.patch.object(
                server.agents_tmux,
                "interrupt",
                side_effect=lambda n, force=False: hit.append((n, force)),
            ):
                out = server.interrupt_chat({"slug": "bot-a", "tmux": "heavy-bot-a", "id": 1})
        self.assertTrue(out["stopped"])
        self.assertFalse(out["busy"])
        self.assertEqual(out["activity"], "Ready")
        self.assertGreaterEqual(len(hit), 1)
        self.assertEqual(hit[0], ("heavy-bot-a", True))
        self.assertTrue(server.is_just_stopped("bot-a"))

    def test_interrupt_chat_missing_session(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A"}}))
        with mock.patch.object(server.agents_tmux, "list_sessions", return_value=[]):
            with self.assertRaises(RuntimeError):
                server.interrupt_chat({"slug": "bot-a"})

    def test_tmux_interrupt_sends_escape(self):
        import agents_tmux

        keys = []
        sent = {"esc": False}

        def fake_run(args, input_bytes=None):
            keys.append(list(args))
            if args and args[0] == "send-keys" and "Escape" in args:
                sent["esc"] = True
            if args and args[0] == "capture-pane":
                body = (
                    "│ ❯ │\nalways-approve".encode()
                    if sent["esc"]
                    else b"Waiting for response\nEsc:cancel"
                )
                return mock.Mock(returncode=0, stderr=b"", stdout=body)
            return mock.Mock(returncode=0, stderr=b"", stdout=b"")

        with mock.patch.object(agents_tmux, "_run", side_effect=fake_run):
            with mock.patch.object(agents_tmux.time, "sleep", return_value=None):
                agents_tmux.interrupt("heavy-bot-a")
        sent_keys = [k for k in keys if k and k[0] == "send-keys"]
        self.assertEqual(sent_keys[0], ["send-keys", "-t", "heavy-bot-a", "Escape"])
        self.assertIn(["send-keys", "-t", "heavy-bot-a", "C-["], sent_keys)
        self.assertNotEqual(sent_keys[0], ["send-keys", "-t", "heavy-bot-a", "C-c"])

    def test_tmux_interrupt_idle_does_not_open_rewind(self):
        import agents_tmux

        keys = []

        def fake_run(args, input_bytes=None):
            keys.append(list(args))
            return mock.Mock(returncode=0, stderr=b"", stdout=b"> \nGrok 4.6")

        with mock.patch.object(agents_tmux, "_run", side_effect=fake_run):
            agents_tmux.interrupt("heavy-bot-a")
        self.assertEqual([k for k in keys if k and k[0] == "send-keys"], [])

    def test_tmux_interrupt_esc_before_ctrl_c(self):
        import agents_tmux

        keys = []
        n = {"cap": 0}

        def fake_run(args, input_bytes=None):
            keys.append(list(args))
            if args and args[0] == "capture-pane":
                n["cap"] += 1
                body = (
                    b"command still running\nEsc:cancel"
                    if n["cap"] < 4
                    else "│ ❯ │\nalways-approve".encode()
                )
                return mock.Mock(returncode=0, stderr=b"", stdout=body)
            return mock.Mock(returncode=0, stderr=b"", stdout=b"")

        with mock.patch.object(agents_tmux, "_run", side_effect=fake_run):
            with mock.patch.object(agents_tmux.time, "sleep", return_value=None):
                agents_tmux.interrupt("heavy-bot-a", force=True)
        sent = [k for k in keys if k and k[0] == "send-keys"]
        self.assertEqual(sent[0], ["send-keys", "-t", "heavy-bot-a", "Escape"])
        self.assertIn(["send-keys", "-t", "heavy-bot-a", "C-c"], sent)
        self.assertLess(sent.index(["send-keys", "-t", "heavy-bot-a", "Escape"]), sent.index(["send-keys", "-t", "heavy-bot-a", "C-c"]))

    def test_stop_command_does_not_start_a_new_turn(self):
        self.assertTrue(server.is_stop_command("stop"))
        self.assertTrue(server.is_stop_command("STOP!!!"))
        self.assertTrue(server.is_stop_command("hou op"))
        self.assertFalse(server.is_stop_command("stop de ads"))
        rosterlib.save_roster(
            _roster({"bot-a": {"label": "X", "tmux": "heavy-bot-a", "session_id": "sid-a"}})
        )
        hit = []
        with mock.patch.object(
            server.agents_tmux, "list_sessions", return_value=[{"tmux": "heavy-bot-a"}]
        ):
            with mock.patch.object(
                server.agents_tmux, "interrupt", side_effect=lambda n, force=False: hit.append(n)
            ):
                out = server.dispatch_text({"slug": "bot-a", "tmux": "heavy-bot-a"}, "stop!!!", True)
        self.assertTrue(out.get("stopped"))
        self.assertEqual(out.get("via"), "stop-cmd")
        self.assertGreaterEqual(len(hit), 1)
        self.assertEqual(hit[0], "heavy-bot-a")
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function isStopCmd(t)", html)
        self.assertIn("if(isStopCmd(text))", html)
        follow = html.split("function looksFollow(t){", 1)[1].split("}", 1)[0]
        self.assertNotIn("|stop|", follow)

    def test_stop_clears_submit_hold_immediately(self):
        now = time.time()
        server._stopped_until.clear()
        self.assertTrue(server.last_submit_hold({"last_submit_at": now}, now))
        server.mark_stopped("bot-a")
        self.assertTrue(server.is_just_stopped("bot-a"))
        rosterlib.save_roster(
            _roster({"bot-a": {"label": "A", "last_submit_at": now, "tmux": "heavy-bot-a"}})
        )
        server.clear_last_submit("bot-a")
        meta = rosterlib.load_roster()["agents"]["bot-a"]
        self.assertIsNone(meta.get("last_submit_at"))

    def test_deliver_text_uses_tmux_not_focus(self):
        rosterlib.save_roster(
            _roster({"bot-a": {"label": "A", "tmux": "heavy-bot-a", "session_id": "sid-a"}})
        )
        sent = []
        with mock.patch.object(
            server.agents_tmux, "list_sessions", return_value=[{"tmux": "heavy-bot-a"}]
        ):
            with mock.patch.object(
                server.agents_tmux, "send_text", side_effect=lambda *a, **k: sent.append((a, k))
            ):
                with mock.patch.object(server, "focus_window_id") as foc:
                    server.deliver_text(
                        {"slug": "bot-a", "tmux": "heavy-bot-a", "id": 1}, "hoi", True
                    )
        self.assertEqual(sent[0][0][0], "heavy-bot-a")
        self.assertEqual(sent[0][0][1], "hoi")
        foc.assert_not_called()

    def test_ensure_hidden_returns_live_tmux(self):
        with mock.patch.object(
            server.agents_tmux, "list_sessions", return_value=[{"tmux": "heavy-bot-a"}]
        ):
            with mock.patch.object(server, "stop_native_grok") as stop:
                name = server.ensure_hidden_tmux("bot-a", {"tmux": "heavy-bot-a"})
        self.assertEqual(name, "heavy-bot-a")
        stop.assert_not_called()

    def test_launch_cmd_resumes_existing_session(self):
        import agents_tmux

        work = Path(self.tmp.name)
        fresh = agents_tmux.launch_cmd("grok", work, "sid-new", resume=False)
        old = agents_tmux.launch_cmd("grok", work, "sid-old", resume=True)
        self.assertIn("--session-id", fresh)
        self.assertNotIn("--resume", fresh)
        self.assertIn("--resume", old)
        self.assertNotIn("--session-id", old)

    def test_launch_cmd_codex_starts_codex_cli(self):
        import agents_tmux

        work = Path(self.tmp.name) / "bot-codex"
        work.mkdir()
        cmd = agents_tmux.launch_cmd("codex", work, "sid-ignored", model="gpt-5.4")
        self.assertIn("codex", cmd)
        self.assertIn("--ask-for-approval never", cmd)
        self.assertIn("--sandbox danger-full-access", cmd)
        self.assertIn("-C", cmd)
        self.assertIn("gpt-5.4", cmd)
        self.assertNotIn("--session-id", cmd)

    def test_launch_cmd_generic_cli_plain_exec(self):
        import agents_tmux

        work = Path(self.tmp.name) / "bot-g"
        work.mkdir()
        with mock.patch.object(agents_tmux, "bin_for", return_value="/opt/homebrew/bin/gemini"):
            cmd = agents_tmux.launch_cmd("gemini", work, "sid-x")
        self.assertIn("gemini", cmd)
        self.assertIn("exec", cmd)
        self.assertNotIn("--always-approve", cmd)
        self.assertNotIn("--session-id", cmd)

    def test_ensure_codex_trust_appends_once(self):
        import agents_tmux

        cfg = Path(self.tmp.name) / "config.toml"
        cfg.write_text("# start\n", encoding="utf-8")
        work = Path(self.tmp.name) / "bot-trust"
        work.mkdir()
        old = agents_tmux.CODEX_CONFIG
        agents_tmux.CODEX_CONFIG = cfg
        self.addCleanup(lambda: setattr(agents_tmux, "CODEX_CONFIG", old))
        agents_tmux.ensure_codex_trust(work)
        agents_tmux.ensure_codex_trust(work)
        text = cfg.read_text(encoding="utf-8")
        self.assertEqual(text.count(f'[projects."{work.resolve()}"]'), 1)
        self.assertIn('trust_level = "trusted"', text)

    def test_cmd_ai_ignores_codex_word_in_grok_prompt(self):
        import agents_tmux

        grok = "/Users/timgrootes/.local/bin/grok --always-approve --verbatim mention Codex here"
        self.assertEqual(agents_tmux.cmd_ai(grok), "grok")
        self.assertEqual(agents_tmux.cmd_ai("/Users/timgrootes/.local/bin/codex -m gpt-5.4"), "codex")
        self.assertEqual(agents_tmux.cmd_ai(""), "")

    def test_switch_bot_ai_respawns_when_roster_already_codex(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {
                        "label": "ChatGPT",
                        "tmux": "heavy-bot-a",
                        "ai": "codex",
                        "model": "",
                        "cwd": str(Path(self.tmp.name)),
                    }
                }
            )
        )
        spawned = []

        def fake_respawn(name, ai="grok", cwd=None, sid=None, model=""):
            spawned.append((name, ai, model))
            return {"session_id": sid or "new-sid", "cwd": cwd, "ai": ai, "model": model}

        with mock.patch.object(server.agents_tmux, "running_ai", return_value="grok"):
            with mock.patch.object(server.agents_tmux, "bin_for", return_value="/usr/bin/codex"):
                with mock.patch.object(server.agents_tmux, "respawn", side_effect=fake_respawn):
                    with mock.patch.object(server, "maybe_update_ai"):
                        out = server.switch_bot_ai("bot-a", "codex")
        self.assertTrue(out.get("respawned"))
        self.assertEqual(spawned[0][0], "heavy-bot-a")
        self.assertEqual(spawned[0][1], "codex")
        self.assertEqual(rosterlib.load_roster()["agents"]["bot-a"]["ai"], "codex")

    def test_switch_bot_ai_skips_when_already_running(self):
        rosterlib.save_roster(
            _roster({"bot-a": {"label": "ChatGPT", "tmux": "heavy-bot-a", "ai": "codex"}})
        )
        with mock.patch.object(server.agents_tmux, "running_ai", return_value="codex"):
            with mock.patch.object(server.agents_tmux, "bin_for", return_value="/usr/bin/codex"):
                with mock.patch.object(server.agents_tmux, "respawn") as respawn:
                    out = server.switch_bot_ai("bot-a", "codex")
        self.assertFalse(out.get("changed"))
        respawn.assert_not_called()

    def test_release_session_locks_removes_files(self):
        sid = "sid-lock-test"
        loc = Path.home() / ".grok" / "relocations" / f"{sid}.lock"
        loc.parent.mkdir(parents=True, exist_ok=True)
        loc.write_text("", encoding="utf-8")
        self.addCleanup(lambda: loc.unlink(missing_ok=True))
        server.release_session_locks(sid)
        self.assertFalse(loc.exists())

    def test_worker_cannot_broadcast_overleg(self):
        import overleg_watch as ow

        rosterlib.save_roster(
            _roster(
                {
                    "bot-c": {"label": "CEO", "role": "ceo"},
                    "bot-d": {"label": "Degero"},
                    "bot-x": {"label": "X"},
                },
                ceo="bot-c",
            )
        )
        rost = rosterlib.load_roster()
        targets, err = ow.plan_targets({"to": "all", "from": "Degero", "text": "hoi"}, rost)
        self.assertEqual(targets, [])
        self.assertIn("boss", err)
        ok, err2 = ow.plan_targets({"to": "all", "from": "CEO", "text": "doe dit"}, rost)
        slugs = [s for s, _ in ok]
        self.assertEqual(err2, "")
        self.assertIn("bot-d", slugs)
        self.assertIn("bot-x", slugs)
        self.assertNotIn("bot-c", slugs)

    def test_overleg_matches_roster_label(self):
        import overleg_watch as ow

        rosterlib.save_roster(
            _roster({"bot-c": {"label": "CEO"}, "bot-d": {"label": "Degero"}}, ceo="bot-c")
        )
        rost = rosterlib.load_roster()
        hit, err = ow.plan_targets({"to": "Degero", "from": "CEO", "text": "gacs"}, rost)
        self.assertEqual(err, "")
        self.assertEqual(hit[0][0], "bot-d")

    def test_pick_routes_sends_degero_and_keeps_ok(self):
        rost = _roster(
            {
                "bot-c": {"label": "CEO", "role": "ceo"},
                "bot-d": {"label": "Degero"},
                "bot-a": {"label": "ADS bot"},
                "bot-x": {"label": "X"},
            },
            ceo="bot-c",
        )
        self.assertEqual(rosterlib.pick_routes("check gacs op degero.nl", rost, "bot-c"), ["bot-d"])
        self.assertEqual(rosterlib.pick_routes("apple ads is te duur", rost, "bot-c"), ["bot-a"])
        self.assertEqual(rosterlib.pick_routes("ok", rost, "bot-c"), [])
        self.assertEqual(
            rosterlib.pick_routes(
                "zet je deze vraag meteen bij de app bot? heeft hij een 2de agent?",
                rost,
                "bot-c",
            ),
            [],
        )
        self.assertIn("bot-d", rosterlib.pick_routes("iedereen even stand", rost, "bot-c"))
        self.assertNotIn("bot-c", rosterlib.pick_routes("iedereen even stand", rost, "bot-c"))

    def test_parse_leak_command(self):
        self.assertEqual(rosterlib.parse_leak_command("lek naar ads"), "ads")
        self.assertEqual(rosterlib.parse_leak_command("LEK NAAR ADS bot"), "ADS bot")
        self.assertEqual(rosterlib.parse_leak_command("leak to degero"), "degero")
        self.assertEqual(rosterlib.parse_leak_command("stuur naar X"), "X")
        self.assertIsNone(rosterlib.parse_leak_command("check apple ads"))
        self.assertIsNone(rosterlib.parse_leak_command("lekker naar huis"))

    def test_dispatch_leak_switches_to_ads_without_typing_here(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-17944": {"label": "Old", "window_id": 17944},
                    "bot-a": {"label": "ADS bot", "window_id": 918136, "tmux": "heavy-bot-71128"},
                }
            )
        )
        win = {"slug": "bot-17944", "id": 17944, "tmux": "heavy-bot-17944"}
        with mock.patch.object(server, "deliver_text") as deliver:
            with mock.patch.object(server, "slug_for_window", return_value="bot-17944"):
                out = server.dispatch_text(win, "lek naar ads", True)
        self.assertTrue(out.get("ok"))
        self.assertTrue(out.get("silent"))
        self.assertEqual(out.get("routed"), ["ADS bot"])
        self.assertEqual(out.get("switch", {}).get("slug"), "bot-a")
        self.assertEqual(out.get("switch", {}).get("id"), 918136)
        deliver.assert_not_called()

    def test_set_home_marks_one_bot(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-c": {"label": "CEO", "role": "ceo"},
                    "bot-ask": {"label": "ASK"},
                    "bot-d": {"label": "Degero"},
                },
                ceo="bot-c",
            )
        )
        rosterlib.set_home("bot-ask")
        rost = rosterlib.load_roster()
        self.assertEqual(rost["home"], "bot-ask")
        self.assertTrue(rosterlib.is_home("bot-ask", rost))
        self.assertFalse(rosterlib.is_home("bot-d", rost))
        rosterlib.update_bot("bot-d", home=True)
        rost = rosterlib.load_roster()
        self.assertEqual(rost["home"], "bot-d")
        self.assertFalse(rosterlib.is_home("bot-ask", rost))
        pub = rosterlib.public_roster([], lambda w: "")
        self.assertEqual(pub["home"], "bot-d")
        by = {a["slug"]: a for a in pub["agents"]}
        self.assertTrue(by["bot-d"]["home"])
        self.assertFalse(by["bot-ask"]["home"])

    def test_home_bot_starts_helper_for_new_task_when_busy(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-c": {"label": "CEO", "role": "ceo"},
                    "bot-ask": {"label": "ASK", "tmux": "heavy-bot-ask", "home": True},
                },
                ceo="bot-c",
            )
        )
        rosterlib.set_home("bot-ask")
        win = {"slug": "bot-ask", "tmux": "heavy-bot-ask", "id": 9}
        with mock.patch.object(server, "live_busy", return_value=True):
            with mock.patch.object(server, "recently_submitted", return_value=True):
                with mock.patch.object(server, "is_new_question", return_value=True):
                    with mock.patch.object(server, "helper_slots_full", return_value=False):
                        with mock.patch.object(
                            server,
                            "start_helper",
                            return_value={"slug": "h--ask-1", "tmux": "heavy-h--ask-1"},
                        ) as helper:
                            with mock.patch.object(server, "deliver_text") as deliver:
                                out = server.dispatch_text(win, "tweede vraag over spacex", True)
        self.assertEqual(out.get("choice"), "helper")
        self.assertTrue(out.get("helper"))
        self.assertFalse(out.get("queued"))
        self.assertEqual(out.get("chosen_by"), "swarm")
        helper.assert_called_once()
        deliver.assert_not_called()
        self.assertEqual(rosterlib.load_queue("bot-ask"), [])

    def test_first_message_not_parked_when_idle(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A", "tmux": "heavy-bot-a"}}))
        win = {"slug": "bot-a", "tmux": "heavy-bot-a", "id": 1}
        with mock.patch.object(server, "live_busy", return_value=False):
            with mock.patch.object(server, "recently_submitted", return_value=True):
                with mock.patch.object(server, "is_new_question", return_value=True):
                    with mock.patch.object(server, "deliver_text") as deliver:
                        out = server.dispatch_text(win, "eerste vraag over de site", True)
        self.assertFalse(out.get("inbox"))
        deliver.assert_called_once()

    def test_file_notice_is_not_parked_as_queue(self):
        self.assertTrue(server.is_file_notice("Bestand ontvangen: /tmp/a.png\nGebruik dit bestand in je werk."))
        self.assertFalse(server.is_new_question({}, "Bestand: foto.png", "bot-a"))
        rosterlib.save_roster(_roster({"bot-a": {"label": "A", "tmux": "heavy-bot-a"}}))
        win = {"slug": "bot-a", "tmux": "heavy-bot-a", "id": 1}
        with mock.patch.object(server, "live_busy", return_value=True):
            with mock.patch.object(server, "steer_into_chat", return_value={"via": "steer"}) as steer:
                with mock.patch.object(server, "deliver_text") as deliver:
                    out = server.dispatch_text(win, "Bestand ontvangen: /x/a.png\nGebruik dit bestand in je werk.", True)
        self.assertFalse(out.get("inbox"))
        deliver.assert_not_called()
        steer.assert_called()
        self.assertEqual(rosterlib.public_queue("bot-a"), [])

    def test_merge_file_cards_keeps_upload(self):
        msgs = [{"role": "user", "text": "hallo", "at": 1}]
        swarm = [{"role": "user", "text": "shot.png", "meta": "file", "name": "shot.png", "path": "/tmp/shot.png", "at": 2}]
        out = server.merge_file_cards(msgs, swarm)
        self.assertEqual(out[-1]["meta"], "file")
        self.assertEqual(out[-1]["name"], "shot.png")

    def test_merge_file_cards_dedupes_notice_and_swarm(self):
        notice = {
            "role": "user",
            "text": "Bestand ontvangen: /tmp/inbox/shot.png\nGebruik dit bestand in je werk.",
            "at": 1,
        }
        swarm = [
            {
                "role": "user",
                "text": "shot.png",
                "meta": "file",
                "name": "shot.png",
                "path": "/tmp/inbox/shot.png",
                "at": 2,
            }
        ]
        out = server.merge_file_cards([notice], swarm)
        files = [m for m in out if m.get("meta") == "file" or server.is_file_notice(m.get("text") or "")]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["meta"], "file")
        self.assertEqual(files[0]["name"], "shot.png")
        self.assertFalse(any(server.is_file_notice(m.get("text") or "") for m in out))
        self.assertEqual(server.file_basename(notice), "shot.png")

    def test_weekly_usage_parses_billing_json(self):
        payload = json.dumps(
            {
                "config": {
                    "creditUsagePercent": 14.0,
                    "currentPeriod": {"end": "2026-08-24T18:26:40+00:00"},
                    "productUsage": [{"product": "GrokBuild", "usagePercent": 14.0}],
                }
            }
        ).encode()
        cm = mock.MagicMock()
        cm.read.return_value = payload
        cm.__enter__.return_value = cm
        with mock.patch.object(server, "_grok_bearer", return_value="tok"):
            with mock.patch.object(urllib.request, "urlopen", return_value=cm):
                out = server._pull_weekly_usage()
        self.assertEqual(out["percent"], 14.0)
        self.assertEqual(out["build"], 14.0)
        self.assertIn("2026-08-24", out["reset"])

    def test_reply_prefix_is_not_a_new_question(self):
        win = {"slug": "bot-a", "title": "A — grok"}
        self.assertFalse(
            server.is_new_question(
                win,
                "↩ Reply to this message:\n> oude bubbel\n\nmaak dit korter",
                "bot-a",
            )
        )

    def test_tick_loops_skips_busy_bot_without_marking_fired(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A", "tmux": "heavy-bot-a"}}))
        item = rosterlib.upsert_loop("bot-a", "Check", "5m", "korte ronde")
        loops = rosterlib.load_loops("bot-a")
        loops[0]["next"] = time.time() - 10
        rosterlib.save_loops("bot-a", loops)
        nxt = loops[0]["next"]
        server._last_loops = 0
        with mock.patch.object(server, "resolve_delivery", return_value={"slug": "bot-a", "tmux": "heavy-bot-a"}):
            with mock.patch.object(server, "live_busy", return_value=True):
                with mock.patch.object(server, "dispatch_text") as dispatch:
                    server.tick_loops()
        dispatch.assert_not_called()
        again = rosterlib.load_loops("bot-a")
        self.assertAlmostEqual(float(again[0]["next"]), nxt, places=1)

    def test_role_upsert_keeps_facts(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "Degero", "role": "worker"}}))
        rosterlib.agent_dir("bot-a")
        p = rosterlib.memory_path("bot-a")
        p.write_text("# Geheugen\n\n- blijf staan\n", encoding="utf-8")
        rosterlib.ensure_role_memory("bot-a")
        text = p.read_text(encoding="utf-8")
        self.assertIn("blijf staan", text)
        self.assertIn("ROLE", text)
        self.assertIn("Degero", text)
        rosterlib.ensure_role_memory("bot-a")
        self.assertEqual(text.count("<!-- ROLE -->"), 1)

    def test_forgotten_bot_is_not_resyncd(self):
        rosterlib.save_roster(
            _roster({"bot-a": {"label": "A", "window_id": 1, "tmux": "heavy-bot-a", "session_id": "sid-a"}})
        )
        rosterlib.remember_forgotten(
            "bot-a", {"window_id": 1, "tmux": "heavy-bot-a", "session_id": "sid-a"}
        )
        rosterlib.drop_agent(slug="bot-a")
        rost = rosterlib.sync_from_windows(
            [{"id": 1, "title": "A — grok", "tmux": "heavy-bot-a", "session_id": "sid-a", "slug": "bot-a"}],
            lambda w: "A",
        )
        self.assertNotIn("bot-a", rost["agents"])

    def test_delete_bot_drops_roster(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {"label": "A", "tmux": "heavy-bot-a"},
                    "bot-b": {"label": "B"},
                },
                ceo="bot-a",
            )
        )
        with mock.patch.object(server.agents_tmux, "kill") as killed:
            with mock.patch.object(server, "kill_helpers"):
                server.delete_bot("bot-a")
        rost = rosterlib.load_roster()
        self.assertNotIn("bot-a", rost["agents"])
        self.assertEqual(rost["ceo"], "bot-b")
        killed.assert_called()

    def test_busy_clock_resets_after_real_idle(self):
        server._busy_since.clear()
        server._busy_idle_since.clear()
        start = server.track_busy("bot-a", True)
        server._busy_idle_since["bot-a"] = time.time() - (server._BUSY_IDLE_GRACE + 1)
        self.assertIsNone(server.track_busy("bot-a", False))
        later = server.track_busy("bot-a", True)
        self.assertIsNotNone(later)
        self.assertGreater(later, start)

    def test_session_from_nicks_is_window_only(self):
        rosterlib.save_nicks(
            {
                "by_window": {
                    "1": {"label": "CEO", "session_id": "sid-ceo"},
                    "2": {"label": "Degero"},
                },
                "by_session": {"sid-ceo": {"label": "CEO"}},
            }
        )
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {"label": "CEO", "session_id": "sid-ceo", "slug": "bot-a", "window_id": 1},
                    "bot-b": {"label": "Degero", "slug": "bot-b", "window_id": 2},
                }
            )
        )
        sid = server.session_id_from_nicks({"slug": "bot-b", "label": "CEO", "window_id": 2})
        self.assertEqual(sid, "")

    def test_shared_home_cwd_is_not_private(self):
        self.assertFalse(server.cwd_is_private("bot-17944", str(Path.home())))
        self.assertFalse(server.cwd_is_private("bot-17944", "/Users/timgrootes"))

    def test_messages_without_session_do_not_use_home_cwd(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A", "slug": "bot-a"}}))
        msgs = server.messages_for_meta(
            {"slug": "bot-a", "cwd": str(Path.home()), "ai": "grok"},
            allow_cwd_fallback=True,
        )
        self.assertEqual(msgs, [])

    def test_session_owner_includes_helpers(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {
                        "label": "A",
                        "session_id": "sid-a",
                        "helpers": [{"slug": "h--bot-a-1", "session_id": "sid-h"}],
                    }
                }
            )
        )
        self.assertEqual(server.session_owner("sid-a"), "bot-a")
        self.assertEqual(server.session_owner("sid-h"), "h--bot-a-1")
        self.assertIn("sid-h", server.taken_session_ids())

    def test_persist_refuses_helper_session(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {
                        "label": "A",
                        "session_id": "sid-a",
                        "helpers": [{"slug": "h--bot-a-1", "session_id": "sid-h"}],
                    }
                }
            )
        )
        self.assertFalse(server._persist_session("bot-a", "sid-h"))
        self.assertEqual(rosterlib.load_roster()["agents"]["bot-a"]["session_id"], "sid-a")

    def test_bound_session_does_not_fallback_to_helper_cwd(self):
        work = self.agents_dir / "bot-a"
        work.mkdir()
        sess_root = Path(self.tmp.name) / "sessions"
        enc = __import__("urllib.parse", fromlist=["quote"]).quote(str(work.resolve()), safe="")
        helper = sess_root / enc / "sid-h"
        helper.mkdir(parents=True)
        (helper / "chat_history.jsonl").write_text(
            json.dumps({"type": "user", "content": "<user_query>helper only</user_query>"}) + "\n",
            encoding="utf-8",
        )
        old_sess = server.SESSIONS_ROOT
        server.SESSIONS_ROOT = sess_root
        self.addCleanup(lambda: setattr(server, "SESSIONS_ROOT", old_sess))
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {
                        "label": "A",
                        "slug": "bot-a",
                        "session_id": "sid-missing",
                        "helpers": [{"slug": "h--bot-a-1", "session_id": "sid-h"}],
                    }
                }
            )
        )
        msgs = server.messages_for_meta(
            {
                "slug": "bot-a",
                "cwd": str(work),
                "ai": "grok",
                "session_id": "sid-missing",
            },
            allow_cwd_fallback=True,
        )
        self.assertEqual(msgs, [])
        self.assertEqual(rosterlib.load_roster()["agents"]["bot-a"]["session_id"], "sid-missing")

    def test_stamp_chat_times_from_updates(self):
        d = Path(self.tmp.name) / "sess-ts"
        d.mkdir()
        (d / "updates.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "method": "session/update",
                            "timestamp": 1786967787,
                            "params": {
                                "update": {
                                    "sessionUpdate": "user_message_chunk",
                                    "content": {"type": "text", "text": "Hallo daar"},
                                }
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "method": "session/update",
                            "timestamp": 1786967792,
                            "params": {
                                "update": {
                                    "sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": "Ik kijk even"},
                                }
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        msgs = [
            {"role": "user", "text": "Hallo daar"},
            {"role": "assistant", "text": "Ik kijk even naar je vraag."},
        ]
        got = server.stamp_chat_times(d, msgs)
        self.assertEqual(got[0]["at"], 1786967787)
        self.assertEqual(got[1]["at"], 1786967792)
        pieces = [
            {"role": "user", "text": "Hallo daar"},
            {"role": "assistant", "text": "Leest · file", "meta": "tool"},
            {"role": "assistant", "text": "Ik kijk even naar je vraag."},
        ]
        stamped = server.stamp_chat_times(d, pieces)
        self.assertEqual(stamped[1]["at"], 1786967792)
        self.assertEqual(stamped[2]["at"], 1786967792)

    def test_line_to_msg_keeps_timestamp(self):
        line = json.dumps(
            {
                "type": "user",
                "content": "<user_query>met tijd</user_query>",
                "timestamp": 1786967000,
            }
        ).encode("utf-8")
        msg = server._line_to_msg(line)
        self.assertEqual(msg["text"], "met tijd")
        self.assertEqual(msg["at"], 1786967000)

    def test_parse_chat_keeps_last_good_on_empty_rewrite(self):
        d = Path(self.tmp.name) / "sess"
        d.mkdir()
        hist = d / "chat_history.jsonl"
        hist.write_text(
            json.dumps({"type": "user", "content": "<user_query>blijf staan</user_query>"}) + "\n",
            encoding="utf-8",
        )
        first = server.parse_chat(d)
        self.assertEqual(first[0]["text"], "blijf staan")
        hist.write_text("", encoding="utf-8")
        still = server.parse_chat(d)
        self.assertEqual([m["text"] for m in still], ["blijf staan"])

    def test_start_helper_spawns_even_if_home(self):
        rosterlib.save_roster(
            _roster({"bot-a": {"label": "A", "home": True, "tmux": "heavy-bot-a"}})
        )
        rosterlib.set_home("bot-a")
        win = {"slug": "bot-a", "tmux": "heavy-bot-a", "id": 1}
        spawned = {"tmux": "heavy-h--bot-a-1", "slug": "h--bot-a-1", "session_id": "sid-h"}
        with mock.patch.object(server.agents_tmux, "list_sessions", return_value=[]):
            with mock.patch.object(server.agents_tmux, "spawn_helper", return_value=spawned) as sp:
                with mock.patch.object(server.threading, "Thread"):
                    out = server.start_helper(win, "bot-a", "doe dit als agent")
        sp.assert_called_once()
        self.assertEqual(out.get("tmux"), "heavy-h--bot-a-1")
        self.assertFalse(out.get("queued"))
        hs = rosterlib.helpers_of("bot-a")
        self.assertTrue(hs)
        self.assertIn("doe dit", hs[0].get("task") or "")
        self.assertEqual(rosterlib.load_queue("bot-a"), [])

    def test_replace_helpers_can_drop_idle(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {
                        "label": "A",
                        "helpers": [
                            {"slug": "h--bot-a-1", "tmux": "heavy-h--bot-a-1", "task": "oud"}
                        ],
                    }
                }
            )
        )
        rosterlib.replace_helpers("bot-a", [])
        self.assertEqual(rosterlib.helpers_of("bot-a"), [])

    def test_prune_orphan_helpers_keeps_assigned(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {
                        "label": "A",
                        "helpers": [{"slug": "h--bot-a-1", "tmux": "heavy-h--bot-a-1"}],
                    }
                }
            )
        )
        killed = []
        live = [
            {"tmux": "heavy-h--bot-a-1", "slug": "h--bot-a-1"},
            {"tmux": "heavy-h--orphan-1", "slug": "h--orphan-1"},
        ]
        with mock.patch.object(server.agents_tmux, "list_sessions", return_value=live):
            with mock.patch.object(server.agents_tmux, "kill", side_effect=lambda n: killed.append(n)):
                server.prune_orphan_helpers()
        self.assertIn("heavy-h--orphan-1", killed)
        self.assertNotIn("heavy-h--bot-a-1", killed)
        self.assertTrue(rosterlib.helpers_of("bot-a"))

    def test_busy_helper_still_shows_progress(self):
        out = server.fold_helper_thread(
            [
                {"role": "user", "text": "Fix dit venster"},
                {"role": "assistant", "text": "Ik kijk even"},
            ],
            busy=True,
            task="Fix dit venster",
            thread="h1",
        )
        self.assertGreaterEqual(len(out), 2)
        self.assertEqual(out[0]["text"], "Fix dit venster")
        self.assertEqual(out[1]["text"], "Ik kijk even")

    def test_done_helper_keeps_question_and_all_answers(self):
        out = server.fold_helper_thread(
            [
                {"role": "user", "text": "[Swarm extra] Fix\n---\nJe bent een extra agent"},
                {"role": "assistant", "text": "Ik kijk even"},
                {"role": "assistant", "text": "Klaar, contrast gefixt."},
            ],
            busy=False,
            task="Fix dit venster",
            thread="h1",
        )
        texts = [m["text"] for m in out]
        self.assertIn("Fix", texts)
        self.assertIn("Ik kijk even", texts)
        self.assertIn("Klaar, contrast gefixt.", texts)
        self.assertTrue(all(m["helper"] for m in out))

    def test_fold_helper_keeps_full_thread_even_if_busy(self):
        got = server.fold_helper_thread(
            [
                {"role": "user", "text": "[Swarm extra] waar zijn de ideeen\n---\nJe bent een extra agent"},
                {"role": "assistant", "text": "Ik zoek de eerdere scan."},
                {"role": "assistant", "text": "Vijf plays: UGC-ads, clipping, shop, VSL, high-RPM."},
            ],
            True,
            "waar zijn de ideeen",
            "h--x-1",
        )
        texts = [m["text"] for m in got]
        self.assertIn("waar zijn de ideeen", texts)
        self.assertTrue(any("Vijf plays" in t for t in texts))
        self.assertTrue(any("Ik zoek" in t for t in texts))
        self.assertFalse(any(t.startswith("Je bent een extra") for t in texts))

    def test_union_helpers_keeps_old_sessions(self):
        old = [{"slug": "h--a-1", "session_id": "sid-old", "task": "ideeën"}]
        new = [{"slug": "h--a-2", "session_id": "sid-new", "task": "fix venster"}]
        got = rosterlib._union_helpers(old, new)
        sids = {h.get("session_id") for h in got}
        self.assertEqual(sids, {"sid-old", "sid-new"})

    def test_save_roster_does_not_wipe_helpers(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {
                        "label": "A",
                        "helpers": [
                            {"slug": "h--a-1", "session_id": "sid-old", "task": "scan"}
                        ],
                    }
                }
            )
        )
        stale = _roster({"bot-a": {"label": "A", "helpers": [{"slug": "h--a-2", "session_id": "sid-new"}]}})
        rosterlib.save_roster(stale)
        hs = rosterlib.load_roster()["agents"]["bot-a"]["helpers"]
        sids = {h.get("session_id") for h in hs}
        self.assertIn("sid-old", sids)
        self.assertIn("sid-new", sids)

    def test_helper_session_loads_when_owner_is_thread_slug(self):
        work = self.agents_dir / "bot-x"
        work.mkdir()
        sess_root = Path(self.tmp.name) / "sessions"
        enc = __import__("urllib.parse", fromlist=["quote"]).quote(str(work.resolve()), safe="")
        sid = "sid-helper-1"
        hidden = sess_root / enc / sid
        hidden.mkdir(parents=True)
        (hidden / "chat_history.jsonl").write_text(
            json.dumps({"type": "user", "content": "<user_query>waar zijn de ideeen</user_query>"})
            + "\n"
            + json.dumps({"type": "assistant", "content": "Vijf plays: UGC en clipping."})
            + "\n",
            encoding="utf-8",
        )
        old_sess = server.SESSIONS_ROOT
        server.SESSIONS_ROOT = sess_root
        self.addCleanup(lambda: setattr(server, "SESSIONS_ROOT", old_sess))
        rosterlib.save_roster(
            _roster(
                {
                    "bot-x": {
                        "label": "X",
                        "slug": "bot-x",
                        "session_id": "sid-main",
                        "helpers": [{"slug": "sess-sid-hel", "session_id": sid, "task": "waar zijn de ideeen"}],
                    }
                }
            )
        )
        msgs = server.messages_for_meta(
            {"slug": "sess-sid-hel", "session_id": sid, "ai": "grok"},
            allow_cwd_fallback=False,
        )
        self.assertTrue(any("Vijf plays" in (m.get("text") or "") for m in msgs))

    def test_extra_threads_find_workspace_sessions(self):
        work = self.agents_dir / "bot-a"
        work.mkdir()
        sess_root = Path(self.tmp.name) / "sessions"
        enc = __import__("urllib.parse", fromlist=["quote"]).quote(str(work.resolve()), safe="")
        hidden = sess_root / enc / "sid-hidden"
        hidden.mkdir(parents=True)
        (hidden / "chat_history.jsonl").write_text(
            json.dumps({"type": "user", "content": "<user_query>waar zijn de ideeen</user_query>"}) + "\n",
            encoding="utf-8",
        )
        old_sess = server.SESSIONS_ROOT
        server.SESSIONS_ROOT = sess_root
        agents_tmux = __import__("agents_tmux")
        old_root = agents_tmux.WORK_ROOT
        agents_tmux.WORK_ROOT = self.agents_dir
        self.addCleanup(lambda: setattr(agents_tmux, "WORK_ROOT", old_root))
        self.addCleanup(lambda: setattr(server, "SESSIONS_ROOT", old_sess))
        rosterlib.save_roster(_roster({"bot-a": {"label": "A", "slug": "bot-a"}}))
        with mock.patch.object(agents_tmux, "list_sessions", return_value=[]):
            threads = server.extra_threads_for(
                "bot-a",
                "sid-main",
                {"slug": "bot-a", "cwd": str(work), "helpers": []},
            )
        sids = {t.get("session_id") for t in threads}
        # Leftover cwd sessions must not become extra cards — that doubled
        # helpers in the CEO window ("2x") and dumped old chats.
        self.assertNotIn("sid-hidden", sids)

    def test_bind_helper_sessions_fills_empty_sid(self):
        work = self.agents_dir / "bot-a"
        work.mkdir()
        sess_root = Path(self.tmp.name) / "sessions"
        enc = __import__("urllib.parse", fromlist=["quote"]).quote(str(work.resolve()), safe="")
        hidden = sess_root / enc / "sid-ideas"
        hidden.mkdir(parents=True)
        (hidden / "chat_history.jsonl").write_text(
            json.dumps({"type": "user", "content": "<user_query>waar zijn de ideeen</user_query>"}) + "\n",
            encoding="utf-8",
        )
        old_sess = server.SESSIONS_ROOT
        server.SESSIONS_ROOT = sess_root
        agents_tmux = __import__("agents_tmux")
        old_root = agents_tmux.WORK_ROOT
        agents_tmux.WORK_ROOT = self.agents_dir
        self.addCleanup(lambda: setattr(agents_tmux, "WORK_ROOT", old_root))
        self.addCleanup(lambda: setattr(server, "SESSIONS_ROOT", old_sess))
        items = [{"slug": "h--bot-a-1", "tmux": "heavy-h--bot-a-1", "session_id": "", "task": "waar zijn de ideeen"}]
        got = server.bind_helper_sessions("bot-a", "sid-main", items, str(work))
        self.assertEqual(got[0]["session_id"], "sid-ideas")

    def test_crew_helpers_are_numbered(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {
                        "label": "CEO",
                        "helpers": [
                            {
                                "slug": "h--bot-a-1",
                                "tmux": "heavy-h--bot-a-1",
                                "task": "fix vensters mis veel info",
                            },
                            {
                                "slug": "h--bot-a-2",
                                "tmux": "heavy-h--bot-a-2",
                                "task": "navigatie naar beneden",
                            },
                        ],
                    }
                }
            )
        )
        live = [
            {"tmux": "heavy-h--bot-a-1", "busy": True, "activity": "Bezig"},
            {"tmux": "heavy-h--bot-a-2", "busy": True, "activity": "Schrijft"},
        ]
        with mock.patch.object(server.agents_tmux, "_list_cache", (time.time(), live)):
            crew = server.crew_for_slug("bot-a")
        helpers = [c for c in crew if c.get("helper")]
        self.assertEqual([c["name"] for c in helpers], ["Agent 1", "Agent 2"])
        self.assertEqual(helpers[0]["n"], 1)
        self.assertIn("fix vensters", helpers[0]["summary"])
        self.assertIn("navigatie", helpers[1]["task"])

    def test_dedupe_helper_merges_sess_onto_live(self):
        got = server.dedupe_helper_items(
            [
                {"slug": "h--bot-a-1", "tmux": "heavy-h--bot-a-1", "session_id": "", "task": "waar zijn de ideeen"},
                {"slug": "sess-ideas", "tmux": "", "session_id": "sid-ideas", "task": "waar zijn de ideeen"},
            ]
        )
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["slug"], "h--bot-a-1")
        self.assertEqual(got[0]["session_id"], "sid-ideas")

    def test_clip_pane_drops_tui_chrome_and_typed_prompt(self):
        raw = """
:393k [↓]top]
> nee niet goed, staat nog wat andesr onder en wat ik typ
valt onder           |
|                    term
|
always-approve
Enter:queue | Shift+Tab:mode | Esc:cancel | Ctrl+o:send
now | Ctrl+b
 Grok 4.6 (high) ·
"""
        self.assertEqual(server.clip_pane(raw), "")

    def test_clip_pane_keeps_tool_work_not_chrome(self):
        raw = """
● Read server.py
  1431|def clip_pane(raw):
always-approve
Enter:queue | Shift+Tab:mode | Esc:cancel
> leftover typed text
"""
        out = server.clip_pane(raw)
        self.assertIn("clip_pane", out)
        self.assertIn("Read server.py", out)
        self.assertNotIn("always-approve", out)
        self.assertNotIn("leftover typed", out)
        self.assertNotIn("Enter:queue", out)

    def test_clip_pane_keeps_streaming_reply(self):
        raw = """
     Het werkt niet goed: de UI toont een oude sessie.
  ❙  ◈ Searched 1 pattern, Read 1 file
  ┃  ◆ Run Inspect CEO live work
    ⠼ Inspect CEO live work… 0.2s              3m0s ⇣89.8k [↓][stop]
  ╭──────────────────────────────────────────────────────────────────────────╮
  │ ❯                                                                        │
  ╰─────────────────────────────────────── Grok 4.6 (high) · always-approve ─╯
  Shift+Tab:mode  │  Esc:cancel  │  Ctrl+x:shortcuts
"""
        out = server.clip_pane(raw)
        self.assertIn("oude sessie", out)
        self.assertIn("Searched", out)
        self.assertNotIn("always-approve", out)
        self.assertNotIn("Shift+Tab", out)

    def test_tool_extra_parses_json_args(self):
        self.assertEqual(
            server._tool_extra('{"command":"df -h /","timeout":120}'),
            "df -h /",
        )
        self.assertEqual(
            server._tool_extra({"target_file": "/tmp/a.py"}),
            "/tmp/a.py",
        )

    def test_collect_chat_prefers_live_tmux_session(self):
        server._collect_cache.clear()
        work = self.agents_dir / "bot-a"
        work.mkdir()
        sess_root = Path(self.tmp.name) / "sessions"
        enc = __import__("urllib.parse", fromlist=["quote"]).quote(str(work.resolve()), safe="")
        old = sess_root / enc / "sid-old"
        new = sess_root / enc / "sid-live"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        (old / "chat_history.jsonl").write_text(
            json.dumps({"type": "user", "content": "<user_query>oude vraag</user_query>"}) + "\n",
            encoding="utf-8",
        )
        (new / "chat_history.jsonl").write_text(
            json.dumps({"type": "user", "content": "<user_query>Werkt het nu goed</user_query>"}) + "\n",
            encoding="utf-8",
        )
        old_sess = server.SESSIONS_ROOT
        server.SESSIONS_ROOT = sess_root
        self.addCleanup(lambda: setattr(server, "SESSIONS_ROOT", old_sess))
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {
                        "label": "Nieuwe bot",
                        "slug": "bot-a",
                        "session_id": "sid-old",
                        "tmux": "heavy-bot-a",
                        "cwd": str(work),
                    }
                }
            )
        )
        win = {"id": 1, "tmux": "heavy-bot-a", "slug": "bot-a", "title": "bot-a — grok", "busy": True}
        with mock.patch.object(server.agents_tmux, "live_session_id", return_value="sid-live"):
            with mock.patch.object(server.agents_tmux, "capture_pane", return_value=""):
                with mock.patch.object(server.agents_tmux, "list_sessions", return_value=[]):
                    chat = server.collect_chat(win)
        texts = [m.get("text") for m in chat.get("messages") or []]
        self.assertIn("Werkt het nu goed", texts)
        self.assertNotIn("oude vraag", texts)
        self.assertEqual(chat.get("session"), "sid-live")
        self.assertEqual(rosterlib.load_roster()["agents"]["bot-a"]["session_id"], "sid-live")

    def test_collect_chat_stays_waiting_while_pane_is_live(self):
        server._collect_cache.clear()
        rosterlib.save_roster(
            _roster({"bot-a": {"label": "A", "tmux": "heavy-bot-a", "session_id": "sid-a"}})
        )
        win = {"id": 1, "tmux": "heavy-bot-a", "slug": "bot-a", "title": "bot-a — grok"}
        msgs = [
            {"role": "user", "text": "fix send stop"},
            {"role": "assistant", "text": "Ik ga paintStop aanpassen."},
        ]
        with mock.patch.object(server, "messages_for_meta", return_value=msgs):
            with mock.patch.object(server, "live_busy", return_value=True):
                with mock.patch.object(server, "live_progress", return_value={"waiting": True, "activity": "Writing"}):
                    with mock.patch.object(server.agents_tmux, "list_sessions", return_value=[]):
                        chat = server.collect_chat(win)
        prog = chat.get("progress") or {}
        self.assertTrue(prog.get("waiting"))
        self.assertNotEqual(prog.get("activity"), "Ready")

    def test_send_text_uses_private_tmux_buffer(self):
        src = (ROOT / "agents_tmux.py").read_text(encoding="utf-8")
        self.assertIn('buf = f"heavy-{uuid.uuid4().hex[:12]}"', src)
        self.assertIn("_send_lock", src)
        self.assertNotIn('"-b", "heavy"', src)

    def test_match_tty_uses_window_id_not_shared_words(self):
        tabs = [
            {"id": 17944, "tty": "/dev/ttys002", "title": "timgrootes — Responding - Remote iPhone - grok"},
            {"id": 18484, "tty": "/dev/ttys000", "title": "timgrootes — apple ads verstuurd - grok"},
        ]
        with mock.patch.object(server, "terminal_tabs", return_value=tabs):
            self.assertEqual(
                server.match_tty({"id": 17944, "slug": "bot-17944", "title": "stale"}),
                "/dev/ttys002",
            )
            self.assertEqual(
                server.match_tty({"id": 18484, "slug": "bot-18484"}),
                "/dev/ttys000",
            )
            self.assertEqual(server.match_tty({"id": 1, "title": "timgrootes — grok"}), "")

    def test_match_tty_window_id_beats_stale_foreign_tty(self):
        tabs = [
            {"id": 17944, "tty": "/dev/ttys002", "title": "CEO work"},
            {"id": 18484, "tty": "/dev/ttys000", "title": "Apple ADS"},
        ]
        with mock.patch.object(server, "terminal_tabs", return_value=tabs):
            self.assertEqual(
                server.match_tty({"id": 17944, "tty": "/dev/ttys000"}),
                "/dev/ttys002",
            )

    def test_match_tty_does_not_reuse_ads_tty_when_ids_differ(self):
        tabs = [
            {"id": 90001, "tty": "/dev/ttys002", "title": "timgrootes — apple ads verstuurd door carewormerveer - grok"},
            {"id": 90002, "tty": "/dev/ttys014", "title": "timgrootes — Remote iPhone Control of Grok Build Term - grok"},
        ]
        with mock.patch.object(server, "terminal_tabs", return_value=tabs):
            self.assertEqual(
                server.match_tty(
                    {
                        "id": 17944,
                        "slug": "bot-17944",
                        "tty": "/dev/ttys002",
                        "title": "timgrootes — Remote iPhone Control of Grok Build Term… - grok",
                    }
                ),
                "/dev/ttys014",
            )
            self.assertEqual(
                server.match_tty(
                    {
                        "id": 18484,
                        "slug": "bot-18484",
                        "title": "timgrootes — apple ads verstuurd door carewormerveer … - grok",
                    }
                ),
                "/dev/ttys002",
            )

    def test_unique_title_hint_skips_username(self):
        self.assertEqual(server.unique_title_hint("timgrootes — apple ads verstuurd - grok"), "apple ads verstuurd")
        self.assertEqual(server.unique_title_hint("timgrootes — grok — grok — 80×39"), "")
        self.assertFalse(server.unique_title_hint("timgrootes — Remote iPhone Control").lower().startswith("timgrootes"))

    def test_deliver_text_prefers_live_tmux_over_stale_tty(self):
        with mock.patch.object(
            server.agents_tmux, "list_sessions", return_value=[{"tmux": "heavy-bot-17944"}]
        ):
            with mock.patch.object(server.agents_tmux, "send_text") as send:
                with mock.patch.object(server, "type_text") as typ:
                    with mock.patch.object(server, "focus_tty") as foc:
                        server.deliver_text(
                            {
                                "id": 17944,
                                "slug": "bot-17944",
                                "tmux": "heavy-bot-17944",
                                "tty": "/dev/ttys002",
                                "title": "timgrootes — Remote iPhone - grok",
                            },
                            "lek naar ads",
                            True,
                        )
                        send.assert_called_once_with("heavy-bot-17944", "lek naar ads", enter=True)
                        typ.assert_not_called()
                        foc.assert_not_called()

    def test_session_dir_by_id_picks_newest(self):
        root = Path(self.tmp.name) / "sessroot"
        old = root / "home" / "sid-x"
        new = root / "work" / "sid-x"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        (old / "chat_history.jsonl").write_text("old\n", encoding="utf-8")
        time.sleep(0.05)
        (new / "chat_history.jsonl").write_text("new\n", encoding="utf-8")
        prev = server.SESSIONS_ROOT
        server.SESSIONS_ROOT = root
        self.addCleanup(lambda: setattr(server, "SESSIONS_ROOT", prev))
        self.assertEqual(server.session_dir_by_id("sid-x"), new)

    def test_persist_tty_refuses_other_window(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-17944": {"label": "CEO", "window_id": 17944, "tty": "/dev/ttys014"},
                    "bot-18484": {"label": "Apple ADS", "window_id": 18484},
                }
            )
        )
        server.persist_tty({"slug": "bot-17944", "id": 17944, "tty": "/dev/ttys002"})
        # ADS does not yet own it, so CEO may store it — unless ADS window id differs
        # and we pass CEO id with a tty that ADS already has.
        rosterlib.save_roster(
            _roster(
                {
                    "bot-17944": {"label": "CEO", "window_id": 17944, "tty": "/dev/ttys014"},
                    "bot-18484": {"label": "Apple ADS", "window_id": 18484, "tty": "/dev/ttys002"},
                }
            )
        )
        server.persist_tty({"slug": "bot-17944", "id": 17944, "tty": "/dev/ttys002"})
        self.assertEqual(rosterlib.load_roster()["agents"]["bot-17944"]["tty"], "/dev/ttys014")
        self.assertEqual(rosterlib.load_roster()["agents"]["bot-18484"]["tty"], "/dev/ttys002")

    def test_old_swarm_users_do_not_reappear_after_current_turn(self):
        session = [
            {"role": "user", "text": "huidige vraag"},
            {"role": "assistant", "text": "bezig antwoord"},
        ]
        have = {server._norm_txt("huidige vraag")}
        swarm = [
            {"role": "user", "text": "ok test dan", "at": "2026-08-17T03:54:54+00:00"},
            {"role": "user", "text": "huidige vraag", "at": "2026-08-17T12:00:00+00:00"},
            {"role": "user", "text": "net getypt", "at": datetime.now(timezone.utc).isoformat()},
        ]
        got = server.merge_swarm_users(session, swarm, have)
        texts = [m["text"] for m in got]
        self.assertIn("ok test dan", texts)
        self.assertIn("huidige vraag", texts)
        self.assertIn("bezig antwoord", texts)
        self.assertEqual(texts[-1], "net getypt")
        self.assertLess(texts.index("ok test dan"), texts.index("huidige vraag"))

    def test_turn_live_keeps_tools_and_thought(self):
        root = Path(self.tmp.name) / "sess"
        root.mkdir()
        hist = root / "chat_history.jsonl"
        hist.write_text(
            "\n".join(
                [
                    json.dumps({"type": "user", "content": "fix the live pill"}),
                    json.dumps(
                        {
                            "type": "reasoning",
                            "summary": [
                                {
                                    "type": "summary_text",
                                    "text": "I will read index.html to see why progress stays generic.",
                                }
                            ],
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "name": "read_file",
                                    "input": {
                                        "target_file": "/Users/timgrootes/Projects/imac-phone/static/index.html"
                                    },
                                },
                                {
                                    "name": "search_replace",
                                    "input": {
                                        "file_path": "/Users/timgrootes/Projects/imac-phone/server.py"
                                    },
                                },
                            ],
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        old = datetime.fromtimestamp(time.time() - 600, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
        (root / "events.jsonl").write_text(
            json.dumps({"ts": old, "type": "tool_started", "tool_name": "search_replace"})
            + "\n",
            encoding="utf-8",
        )
        with mock.patch.object(server, "session_dir_by_id", return_value=root):
            out = server.live_progress("sid-live", "", "Waiting for response… — bot-43016 — grok", "")
        self.assertTrue(out["waiting"])
        self.assertEqual(out["activity"], "Writing")
        self.assertIn("server.py", out["line"])
        self.assertEqual(out["step_n"], 2)
        self.assertEqual(out["on"], "")
        self.assertNotIn("fix the live pill", out["line"])
        self.assertNotIn("fix the live pill", out["headline"])

    def test_clean_live_target_survives_bad_urls(self):
        self.assertTrue(
            server._clean_live_target(
                "https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828"
            )
        )
        self.assertEqual(server._clean_live_target('https://[^\\"'), "web")
        self.assertIn("example.com", server._clean_live_target("https://example.com/path"))

    def test_describe_live_is_human(self):
        d = server.describe_live("Leest", "/Users/timgrootes/Projects/imac-phone/static/index.html", "maak de pill mooi")
        self.assertEqual(d["headline"], "Reading the code")
        self.assertIn("index.html", d["line"])
        self.assertNotIn("/Users/", d["line"])
        self.assertIn("maak de pill", d["on"])
        long_q = "maak de realtime pill mooier zodat tekst niet wordt afgesneden op iMac of iPhone en vertrouwen geeft"
        full = server.describe_live("Schrijft", "static/index.html", long_q)
        self.assertEqual(full["on"], long_q)
        self.assertNotIn("…", full["on"])
        self.assertIn("index.html", full["line"])
        raw = server.describe_live(
            "Terminal",
            "TOKEN=$(cat /tmp/x) curl http://127.0.0.1:8790/api/state",
            "",
        )
        self.assertNotIn("TOKEN=", raw["line"])
        self.assertNotIn("curl", raw["line"])
        thought = server.describe_live("Denkt", "", "vraag", "Ik bedenk hoe de live-pill duidelijker kan.")
        self.assertIn("live-pill", thought["line"])
        thinking = server.describe_live("Denkt", "/Users/timgrootes/Projects/imac-phone/static/index.html", "lange vraag blijft helemaal staan")
        self.assertEqual(thinking["headline"], "Thinking through the approach")
        self.assertNotIn("index.html", thinking["line"])
        self.assertEqual(thinking["on"], "lange vraag blijft helemaal staan")

    def test_grok_title_and_pane_progress(self):
        step = server._grok_title_step("⠼ - Thinking - Checking If Recent Changes Work Correctl… - grok")
        self.assertIn("Checking If Recent Changes", step)
        self.assertNotIn("Thinking", step)
        self.assertEqual(server._grok_title_step("Waiting for response… — bot-43016 — grok"), "")
        tally = server._grok_pane_tally("◈ Searched 2 patterns, Read 1 file, Listed 1 dir\n◆ Thinking…")
        self.assertIn("Searched 2 patterns", tally)
        self.assertIn("Read 1 file", tally)
        self.assertIn("Listed 1 dir", tally)
        q = server._clean_live_question("[Boss → Swarm]: lukt het?")
        self.assertEqual(q, "lukt het?")

    def test_progress_notes_are_stable_and_merge(self):
        t = server.progress_note_text({"activity": "Reading", "done": "Reading index.html"}, 90)
        self.assertEqual(t, "Read index.html")
        self.assertNotIn("Bezig", t)
        self.assertNotIn("?", t)
        w = server.progress_note_text({"activity": "Writing", "line": "Editing agents_tmux.py"}, 12)
        self.assertEqual(w, "Edited agents_tmux.py")
        q = server.progress_note_text({"activity": "Thinking", "line": "maak de pill mooi alsjeblieft?"}, 12)
        self.assertEqual(q, "Thinking")
        self.assertNotIn("maak de pill", q)
        self.assertEqual(
            server.pills_from_progress_text(
                "Bezig · Leest in de code · Read 11 files, Searched 1 pattern, Read 4 files"
            ),
            ["Read 11 files", "Searched 1 pattern", "Read 4 files"],
        )
        self.assertEqual(
            server.pills_from_progress_text("Bezig · Denkt na · 2m"),
            ["Thinking"],
        )
        self.assertEqual(
            server.pills_from_progress_text("Bezig · Plant de volgende stappen · Read 11 files en Bezig"),
            ["Read 11 files"],
        )
        merged = server.merge_progress_notes(
            [{"role": "user", "text": "doe dit", "at": 10}],
            [{"role": "assistant", "text": "Denkt na", "meta": "progress", "at": 11}],
        )
        self.assertEqual(merged[-1]["meta"], "progress")
        self.assertEqual(merged[-1]["text"], "Thinking")

    def test_progress_notes_repeat_after_new_user_turn(self):
        msgs = [
            {"role": "user", "text": "eerste", "at": 10},
            {"role": "assistant", "text": "Thinking", "meta": "progress", "at": 11},
            {"role": "assistant", "text": "Klaar.", "at": 12},
            {"role": "user", "text": "klaar nu", "at": 20},
        ]
        swarm = [
            {"role": "assistant", "text": "Thinking", "meta": "progress", "at": 11},
            {"role": "assistant", "text": "Thinking", "meta": "progress", "at": 21},
            {"role": "assistant", "text": "Play Console login via Swarm-browser fix", "meta": "progress", "at": 22},
        ]
        got = server.merge_progress_notes(msgs, swarm)
        after = [m for m in got if (m.get("at") or 0) >= 20]
        texts = [m.get("text") for m in after if m.get("meta") == "progress"]
        self.assertIn("Thinking", texts)
        self.assertIn("Play Console login via Swarm-browser fix", texts)
        dropped = server.merge_progress_notes(
            [{"role": "user", "text": "go", "at": 1}],
            [
                {"role": "assistant", "text": "Read 7 files", "meta": "progress", "at": 2},
                {"role": "assistant", "text": "Searched 3 patterns", "meta": "progress", "at": 3},
                {"role": "assistant", "text": "Looking for the iPhone", "meta": "progress", "at": 4},
            ],
        )
        texts = [m["text"] for m in dropped if m.get("meta") == "progress"]
        self.assertEqual(texts, ["Looking for the iPhone"])

    def test_progress_pills_prefer_live_headline(self):
        pills = server.progress_pills(
            {
                "activity": "Thinking",
                "headline": "Play Console login via Swarm-browser fix",
                "line": "Deciding the next step",
            }
        )
        self.assertEqual(pills, ["Play Console login via Swarm-browser fix"])
        self.assertEqual(
            server.progress_pills(
                {"activity": "Thinking", "done": "Read 7 files, Searched 3 patterns"}
            ),
            ["Thinking"],
        )
        self.assertIn("build 27", server._task_from_pane("Task Wait for build 27 and add to Naptara Internal  (12) 2m16s"))
        self.assertIn(
            "klaar nu",
            server._thought_from_pane('  ┃  The user is saying "klaar nu" which means ready now.'),
        )

    def test_maybe_note_progress_writes_on_change_not_every_tick(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A"}}))
        started = time.time() - 30
        prog = {"waiting": True, "activity": "Writing", "line": "Editing agents_tmux.py"}
        server.maybe_note_progress("bot-a", prog, 20, last_user_at=started)
        server.maybe_note_progress("bot-a", prog, 21, last_user_at=started)
        notes = [m for m in rosterlib.load_swarm_msgs("bot-a") if m.get("meta") == "progress"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["text"], "Edited agents_tmux.py")
        path = rosterlib.agent_dir("bot-a") / "live_note.json"
        prev = json.loads(path.read_text(encoding="utf-8"))
        prev["at"] = time.time() - 5
        path.write_text(json.dumps(prev), encoding="utf-8")
        server.maybe_note_progress(
            "bot-a",
            {
                "waiting": True,
                "activity": "Reading",
                "done": "Read 2 files",
                "headline": "Checking TestFlight build 27",
            },
            40,
            last_user_at=started,
        )
        notes = [m for m in rosterlib.load_swarm_msgs("bot-a") if m.get("meta") == "progress"]
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[-1]["text"], "Checking TestFlight build 27")
        self.assertFalse(any(server._is_tally_pill(n["text"]) for n in notes))

    def test_enqueue_and_remove_queue(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A"}}))
        item = rosterlib.enqueue("bot-a", "doe dit daarna")
        self.assertTrue(item["id"])
        pub = rosterlib.public_queue("bot-a")
        self.assertEqual(len(pub), 1)
        self.assertEqual(pub[0]["text"], "doe dit daarna")
        rosterlib.remove_queue("bot-a", item["id"])
        self.assertEqual(rosterlib.public_queue("bot-a"), [])

    def test_hold_pill_auto_queues(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A"}}))
        item = rosterlib.enqueue("bot-a", "docmint check", hold=True)
        self.assertEqual(item["status"], "hold")
        pub = rosterlib.public_queue("bot-a")
        self.assertEqual(pub[0]["status"], "hold")
        items = rosterlib.load_queue("bot-a")
        items[0]["hold_until"] = time.time() - 1
        rosterlib.save_queue("bot-a", items)
        pub2 = rosterlib.public_queue("bot-a")
        self.assertEqual(pub2[0]["status"], "queued")
        kept = rosterlib.assign_queue("bot-a", item["id"], "queue")
        self.assertEqual(kept["status"], "queued")
        taken = rosterlib.assign_queue("bot-a", item["id"], "helper")
        self.assertEqual(taken["text"], "docmint check")
        self.assertEqual(rosterlib.public_queue("bot-a"), [])

    def test_reassign_helper_to_queue_or_steer(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A"}}))
        rosterlib.add_helper(
            "bot-a",
            {"slug": "h--a1", "tmux": "heavy-h--a1", "task": "tweede vraag over ads"},
        )
        win = {"slug": "bot-a", "tmux": "heavy-bot-a", "id": 1}
        with mock.patch.object(server.agents_tmux, "kill") as kill:
            out = server.reassign_helper(win, "bot-a", "h--a1", "queue")
        kill.assert_called_once_with("heavy-h--a1")
        self.assertTrue(out.get("queued"))
        self.assertEqual(out.get("via"), "queue")
        q = rosterlib.public_queue("bot-a")
        self.assertEqual(len(q), 1)
        self.assertIn("ads", q[0]["text"])
        self.assertEqual(rosterlib.helpers_of("bot-a"), [])
        rosterlib.add_helper(
            "bot-a",
            {"slug": "h--a2", "tmux": "heavy-h--a2", "task": "nog een taak"},
        )
        with mock.patch.object(server.agents_tmux, "kill"):
            with mock.patch.object(server, "live_busy", return_value=False):
                with mock.patch.object(server, "recently_submitted", return_value=False):
                    with mock.patch.object(server, "deliver_text") as deliver:
                        out = server.reassign_helper(win, "bot-a", "h--a2", "steer")
        deliver.assert_called_once()
        self.assertEqual(out.get("via"), "steer")
        self.assertEqual(rosterlib.helpers_of("bot-a"), [])
        notes = rosterlib.load_swarm_msgs("bot-a")
        self.assertTrue(any("nog een taak" in str(m.get("text")) for m in notes if m.get("role") == "user"))

    def test_followup_while_busy_waits_instead_of_blind_paste(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A", "tmux": "heavy-bot-a"}}))
        win = {"slug": "bot-a", "tmux": "heavy-bot-a", "id": 1}
        with mock.patch.object(server, "live_busy", return_value=True):
            with mock.patch.object(server, "recently_submitted", return_value=True):
                with mock.patch.object(server, "is_new_question", return_value=False):
                    with mock.patch.object(server, "steer_into_chat", return_value={"via": "steer", "text": "ok en dan css"}) as steer:
                        with mock.patch.object(server, "deliver_text") as deliver:
                            out = server.dispatch_text(win, "ok en dan css", True)
        steer.assert_called_once()
        self.assertEqual(steer.call_args.kwargs.get("interrupt_first"), False)
        deliver.assert_not_called()
        self.assertEqual(out.get("via"), "steer")

    def test_steer_into_chat_writes_bubble_and_interrupts_when_busy(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A", "tmux": "heavy-bot-a"}}))
        win = {"slug": "bot-a", "tmux": "heavy-bot-a", "id": 1}
        with mock.patch.object(server, "live_busy", return_value=True):
            with mock.patch.object(server, "recently_submitted", return_value=True):
                with mock.patch.object(server, "interrupt_chat") as stop:
                    with mock.patch.object(server, "deliver_text"):
                        out = server.steer_into_chat(win, "bot-a", "zet dit in de chat")
        stop.assert_called_once()
        self.assertEqual(out.get("via"), "steer")
        notes = rosterlib.load_swarm_msgs("bot-a")
        self.assertEqual(notes[-1]["role"], "user")
        self.assertEqual(notes[-1]["text"], "zet dit in de chat")

    def test_classify_second_followup_is_steer(self):
        win = {"slug": "bot-a"}
        self.assertEqual(server.classify_second(win, "ok en dan css", "bot-a"), "steer")
        self.assertEqual(server.classify_second(win, "ja", "bot-a"), "steer")
        self.assertEqual(
            server.classify_second(
                win, "↩ Reply to this message:\n> oude bubbel\n\nmaak dit korter", "bot-a"
            ),
            "steer",
        )
        self.assertEqual(
            server.classify_second(win, "Bestand ontvangen: /tmp/a.png", "bot-a"),
            "steer",
        )

    def test_classify_second_new_task_is_helper_or_queue(self):
        win = {"slug": "bot-a"}
        with mock.patch.object(server, "is_new_question", return_value=True):
            with mock.patch.object(server, "helper_slots_full", return_value=False):
                self.assertEqual(
                    server.classify_second(win, "bouw een nieuwe landing page", "bot-a"),
                    "helper",
                )
            with mock.patch.object(server, "helper_slots_full", return_value=True):
                self.assertEqual(
                    server.classify_second(win, "bouw een nieuwe landing page", "bot-a"),
                    "queue",
                )

    def test_busy_new_task_starts_helper(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A", "tmux": "heavy-bot-a"}}))
        win = {"slug": "bot-a", "tmux": "heavy-bot-a", "id": 1}
        with mock.patch.object(server, "live_busy", return_value=True):
            with mock.patch.object(server, "is_new_question", return_value=True):
                with mock.patch.object(server, "helper_slots_full", return_value=False):
                    with mock.patch.object(
                        server,
                        "start_helper",
                        return_value={"slug": "h--a1", "tmux": "heavy-h--a1"},
                    ) as helper:
                        with mock.patch.object(server, "deliver_text") as deliver:
                            out = server.dispatch_text(win, "maak een heel ander ads plan", True)
        helper.assert_called_once()
        deliver.assert_not_called()
        self.assertEqual(out.get("choice"), "helper")
        self.assertTrue(out.get("helper"))
        self.assertEqual(out.get("chosen_by"), "swarm")
        self.assertEqual((rosterlib.load_roster()["agents"]["bot-a"].get("last_route") or {}).get("choice"), "helper")

    def test_busy_new_task_queues_when_helpers_full(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A", "tmux": "heavy-bot-a"}}))
        win = {"slug": "bot-a", "tmux": "heavy-bot-a", "id": 1}
        with mock.patch.object(server, "live_busy", return_value=True):
            with mock.patch.object(server, "is_new_question", return_value=True):
                with mock.patch.object(server, "helper_slots_full", return_value=True):
                    with mock.patch.object(server, "start_helper") as helper:
                        with mock.patch.object(server, "deliver_text") as deliver:
                            out = server.dispatch_text(win, "andere taak over ads", True)
        helper.assert_not_called()
        deliver.assert_not_called()
        self.assertEqual(out.get("choice"), "queue")
        self.assertTrue(out.get("queued"))
        q = rosterlib.load_queue("bot-a")
        self.assertEqual(q[0]["status"], "queued")
        self.assertEqual(q[0]["choice"], "queue")
        self.assertEqual(q[0]["chosen_by"], "swarm")

    def test_keep_screen_on_setting_exists(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-k="awake"', html)
        self.assertIn("function holdAwake(", html)
        self.assertIn('wakeLock.request("screen")', html)
        self.assertTrue(server.DEFAULT_SETTINGS.get("awake"))

    def test_auto_drive_voice_setting(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-k="drive"', html)
        self.assertIn("function startVoice(drive)", html)
        self.assertIn("function ceoWin(", html)
        self.assertIn("function fetchBrief(", html)
        self.assertIn("function dayHello(", html)
        self.assertIn("Goedemiddag, waar kan ik je mee helpen?", html)
        self.assertIn("SpeechSynthesisUtterance", html)
        self.assertIn("function wantsWork(", html)
        self.assertIn("/api/tts", html)
        self.assertIn("/api/stt", html)
        self.assertIn("function tapTalk(", html)
        self.assertNotIn('error==="not-allowed"', html)
        self.assertIn('voice:"eve"', html)
        self.assertEqual(server.TTS_VOICE, "eve")
        self.assertEqual(server.speakable("**Hallo** `wereld`"), "Hallo wereld")
        self.assertEqual(server.stt_bytes(b""), "")
        self.assertGreaterEqual(server.json_body_limit("/api/stt"), server.JSON_BODY_MAX)
        self.assertFalse(server.DEFAULT_SETTINGS.get("drive"))
        self.assertTrue(hasattr(server, "spoken_brief"))
        with mock.patch.object(server, "list_windows_cached", return_value=[]):
            with mock.patch.object(server, "attach_busy_times", return_value=[]):
                with mock.patch.object(
                    rosterlib,
                    "public_roster",
                    return_value={"agents": [{"slug": "bot-a", "label": "Van Dorp", "crew": [{"helper": False, "busy": False}]}]},
                ):
                    with mock.patch.object(server, "decorate_roster", side_effect=lambda p: p):
                        text = server.spoken_brief()
        self.assertIn("klaar", text.lower())

    def test_mobile_uses_png_apple_touch_icon(self):
        index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        go = (ROOT / "static" / "go.html").read_text(encoding="utf-8")
        self.assertIn('rel="apple-touch-icon"', index)
        self.assertIn("icons/apple-touch-icon.png", index)
        self.assertIn('apple-mobile-web-app-title" content="Swarm"', index)
        self.assertIn("icons/app.png?v=7", index)
        self.assertIn('rel="apple-touch-icon"', go)
        self.assertIn("icons/apple-touch-icon.png", go)
        self.assertIn('apple-mobile-web-app-title" content="Swarm"', go)
        icon = ROOT / "static" / "icons" / "apple-touch-icon.png"
        self.assertTrue(icon.is_file())
        self.assertGreater(icon.stat().st_size, 2000)

    def test_parse_login_pane_extracts_device_code(self):
        pane = (
            "Visit https://auth.x.ai/device\n"
            "Enter code: AB12-CD34\n"
        )
        info = server.agents_tmux.parse_login_pane(pane)
        self.assertTrue(info["needed"])
        self.assertIn("auth.x.ai", info["url"])
        self.assertEqual(info["code"], "AB12-CD34")
        idle = "❯ \nGrok 4.6 · always-approve"
        self.assertFalse(server.agents_tmux.parse_login_pane(idle)["needed"])

    def test_login_ui_has_sign_in_controls(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("id=\"loginbar\"", html)
        self.assertIn("id=\"asetlogin\"", html)
        self.assertIn("/api/cli-login", html)

    def test_new_bot_picker_allows_other_cli(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function openNewBot(", html)
        self.assertIn("data-newai", html)
        self.assertIn("asetai-other", html)
        self.assertIn("gemini", html)
        self.assertIn("aider", html)

    def test_inbox_ui_always_has_three_change_buttons(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function assignActs(", html)
        self.assertIn('data-assign="helper"', html)
        self.assertIn('data-assign="steer"', html)
        self.assertIn('data-assign="queue"', html)
        self.assertIn('This chat', html)
        self.assertIn('op:"reassign"', html)
        self.assertIn('starthelper', html)
        self.assertIn("function placeSteer(", html)
        self.assertIn("chose ·", html)
        self.assertIn("Tap another to change", html)
        self.assertIn("inboxSig", html)
        self.assertIn("assignLock", html)
        self.assertIn("button:active", html)
        self.assertIn("button.down", html)
        self.assertIn("def classify_second(", (ROOT / "server.py").read_text(encoding="utf-8"))
        self.assertIn("function closeJobPop(", html)
        self.assertIn("closeJobPop();", html)
        self.assertIn("raw>1e9", html)
        self.assertIn('if(/^\\d{4}-\\d{2}-\\d{2}$/.test(s))', html)

    def test_upload_body_limit_is_large_enough_for_files(self):
        self.assertEqual(server.json_body_limit("/api/type"), server.JSON_BODY_MAX)
        self.assertGreaterEqual(server.json_body_limit("/api/upload"), server.UPLOAD_BODY_MAX)
        self.assertGreater(server.UPLOAD_BODY_MAX, 12_000_000)

    def test_chat_has_file_drag_drop(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="dropmask"', html)
        self.assertIn("function bindFileDrop", html)
        self.assertIn("function looksLikeFileDrag", html)
        self.assertIn("swarmNativeDrop", html)
        self.assertIn("sendFiles(files, win)", html)

    def test_pane_busy_sees_command_still_running(self):
        idle = "❯ \nGrok 4.6 · always-approve"
        running = "◎ 1 command still running · 1 queued — Enter to send now\n│ ❯"
        self.assertFalse(server.agents_tmux.pane_busy(idle))
        self.assertTrue(server.agents_tmux.pane_busy(running))
        self.assertTrue(server.agents_tmux.title_busy("Waiting for response… — bot-61627"))
        loop = "❯ [Loop · Ga degero na en]: check seo\n◎ 1 loop still running\n"
        self.assertFalse(server.agents_tmux.pane_busy(loop))
        rewind = loop + "Rewind to which turn?\n· (no preview)\n"
        self.assertEqual(server.agents_tmux.pane_overlay(rewind), "rewind")
        self.assertFalse(server.agents_tmux.pane_busy(rewind))

    def test_pane_busy_idle_box_after_worked_for(self):
        done = (
            "     De 7 vragen blijven zo.\n"
            "     Worked for 1m15s\n"
            "  ╭──────────────────────────────────────────╮\n"
            "  │ ❯                                        │\n"
            "  ╰────────────────── Grok 4.6 · always-approve ─╯\n"
            "  Shift+Tab:mode  │  Ctrl+x:shortcuts\n"
        )
        self.assertTrue(server.agents_tmux.pane_has_idle_prompt(done))
        self.assertFalse(server.agents_tmux.pane_busy(done))

    def test_pane_busy_ignores_old_spinner_above_idle_prompt(self):
        leftover = (
            "     ⠹ Waiting for response… 4s                    3m12s [stop]\n"
            "     Worked for 12s\n"
            + ("     old scrollback line\n" * 16)
            + "  ╭──────────────────────────────────────────╮\n"
            "  │ ❯                                        │\n"
            "  ╰────────────────── Grok 4.6 · always-approve ─╯\n"
            "  Shift+Tab:mode  │  Ctrl+x:shortcuts\n"
        )
        self.assertFalse(server.agents_tmux.pane_busy(leftover))

    def test_pane_busy_when_composer_stays_open_during_turn(self):
        live = (
            "  ❙  ◈ Read 11 files, Searched 3 patterns\n"
            "  ┃  ◆ Run Inspect live bot busy state\n"
            "    #1 ik zeg stop maar hij gaat maar door\n"
            "    ⠧ Inspect live bot busy state… 0.2s     1m39s [stop]\n"
            "  ╭──────────────────────────────────────────╮\n"
            "  │ ❯                                        │\n"
            "  ╰────────────────── Grok 4.6 · always-approve ─╯\n"
            "  Enter:send now  │  Shift+Tab:mode  │  Esc:cancel  │  Ctrl+b:send to bg\n"
        )
        self.assertTrue(server.agents_tmux.pane_working(live))
        self.assertTrue(server.agents_tmux.pane_busy(live))
        self.assertFalse(server.agents_tmux.pane_has_idle_prompt(live))

    def test_pane_fingerprint_ignores_elapsed_clock(self):
        a = (
            "⠦ Waiting for response… 4s                    12m40s ⇣134k [stop]\n"
            "◆ Run Activate Chrome and click Volgende"
        )
        b = (
            "⠋ Waiting for response… 18s                   12m55s ⇣135k [stop]\n"
            "◆ Run Activate Chrome and click Volgende"
        )
        self.assertEqual(
            server.agents_tmux.pane_fingerprint(a),
            server.agents_tmux.pane_fingerprint(b),
        )
        self.assertTrue(server.agents_tmux.pane_gui_loop(a + "\n◆ Run Click email field"))

    def test_stall_reason_frozen_and_gui_loop(self):
        frozen = "⠦ Waiting for response… 10s   5m00s [stop]\nEsc:cancel"
        self.assertEqual(
            server.stall_reason(frozen, frozen_for=500, busy_for=500),
            "geen voortgang",
        )
        self.assertIsNone(server.stall_reason(frozen, frozen_for=130, busy_for=130))
        gui = (
            "◆ Run Activate Chrome and click Volgende\n"
            "◈ Read 1 file\n"
            "◆ Run Click email field at corrected coordinates\n"
            "⠦ Waiting for response… 1s   16m00s [stop]\n"
            "Esc:cancel\n"
        )
        self.assertEqual(
            server.stall_reason(gui, frozen_for=10, busy_for=16 * 60),
            "browser/klik-lus",
        )
        self.assertIsNone(server.stall_reason(gui, frozen_for=10, busy_for=8 * 60))
        idle = "❯ \nGrok 4.6 · always-approve"
        self.assertIsNone(server.stall_reason(idle, frozen_for=400, busy_for=400))

    def test_unstick_stalled_interrupts_frozen_turn(self):
        server._stall_fp.clear()
        server._last_unstick.clear()
        rosterlib.save_roster(
            _roster({"bot-a": {"label": "A", "tmux": "heavy-bot-a", "busy_since": time.time() - 200}})
        )
        sess = [{"tmux": "heavy-bot-a", "slug": "bot-a", "busy": True}]
        pane = "⠦ Waiting for response… 10s   5m00s [stop]\nEsc:cancel"
        t0 = time.time()
        with mock.patch.object(server.agents_tmux, "list_sessions", return_value=sess):
            with mock.patch.object(server.agents_tmux, "capture_pane", return_value=pane):
                with mock.patch.object(server.agents_tmux, "interrupt") as stop:
                    with mock.patch.object(server.time, "time", return_value=t0):
                        self.assertEqual(server.unstick_stalled(), [])
                    stop.assert_not_called()
                    with mock.patch.object(server.time, "time", return_value=t0 + 500):
                        out = server.unstick_stalled()
        stop.assert_called_once_with("heavy-bot-a")
        self.assertEqual(out[0]["reason"], "geen voortgang")
        notes = rosterlib.load_swarm_msgs("bot-a")
        self.assertTrue(any("vastgelopen" in str(m.get("text")) for m in notes))

    def test_unstick_ignores_stale_roster_busy_since(self):
        server._stall_fp.clear()
        server._last_unstick.clear()
        server._busy_seen.clear()
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {
                        "label": "A",
                        "tmux": "heavy-bot-a",
                        "busy_since": time.time() - 6 * 3600,
                        "last_submit_at": time.time() - 6 * 3600,
                    }
                }
            )
        )
        sess = [{"tmux": "heavy-bot-a", "slug": "bot-a", "busy": True}]
        pane = "⠦ Waiting for response… 10s   5m00s [stop]\nEsc:cancel"
        with mock.patch.object(server.agents_tmux, "list_sessions", return_value=sess):
            with mock.patch.object(server.agents_tmux, "capture_pane", return_value=pane):
                with mock.patch.object(server.agents_tmux, "interrupt") as stop:
                    self.assertEqual(server.unstick_stalled(), [])
                    # First look starts the episode clock; 30s later is not 25 min.
                    with mock.patch.object(server.time, "time", return_value=time.time() + 30):
                        self.assertEqual(server.unstick_stalled(), [])
        stop.assert_not_called()

    def test_live_progress_idle_pane_wins_over_stale_title(self):
        idle = "❯ \nGrok 4.6 · always-approve\nShift+Tab:mode"
        with mock.patch.object(server.agents_tmux, "capture_pane", return_value=idle):
            out = server.live_progress(
                "", "heavy-bot-a", "Waiting for response… — bot-a — grok", ""
            )
        self.assertFalse(out["waiting"])
        self.assertEqual(out["activity"], "Ready")

    def test_crew_uses_live_pane_not_stale_title(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {
                        "label": "A",
                        "tmux": "heavy-bot-a",
                        "title": "Waiting for response… — bot-a — grok",
                    }
                }
            )
        )
        live = [
            {
                "tmux": "heavy-bot-a",
                "slug": "bot-a",
                "busy": False,
                "activity": "Ready",
                "title": "bot-a — grok",
            }
        ]
        with mock.patch.object(server.agents_tmux, "list_sessions", return_value=live):
            crew = server.crew_for_slug("bot-a")
        self.assertFalse(crew[0]["busy"])

    def test_show_busy_ui_does_not_use_unanswered_alone(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        fn = html.split("function showBusy(w){", 1)[1].split("function isMini", 1)[0]
        self.assertNotIn("unansweredUser(w)", fn)
        self.assertNotIn("turnOpen(chatCache[id])", fn)
        self.assertIn("pendingWork(w)", fn)
        self.assertIn("function lastTurnDone(w)", html)
        self.assertIn("turnPills", html)
        self.assertIn("function justStopped(w)", html)
        self.assertIn("function markStopped(w)", html)
        self.assertIn("if(justStopped(w)) return false", html)
        self.assertIn('btn.addEventListener("pointerdown"', html)
        self.assertIn("const stopLatch={}", html)
        self.assertIn("idleAt<400", html)
        self.assertNotIn("unansweredUser(current)", html.split("function applyChatState", 1)[1].split("function paintBusyNow", 1)[0])
        self.assertIn("function pendingWork(w)", html)
        fn = html.split("function showBusy(w){", 1)[1].split("function isMini", 1)[0]
        self.assertLess(fn.find("w.busy===true"), fn.find("lastTurnDone(w)"))
        self.assertNotIn("progress.waiting=false", fn)

    def test_browse_ids_detects_bot_from_tmux_and_cwd(self):
        import browse_ids

        self.assertEqual(browse_ids.normalize_bot("heavy-bot-6344"), "bot-6344")
        self.assertEqual(browse_ids.normalize_bot(""), "shared")
        self.assertEqual(
            browse_ids.detect_bot(env={"SWARM_SLUG": "naptara"}, cwd="/tmp", tmux_name=""),
            "naptara",
        )
        self.assertEqual(
            browse_ids.detect_bot(
                env={},
                cwd="/Users/tim/.grok/imac-phone/workspaces/bot-71128",
                tmux_name="",
            ),
            "bot-71128",
        )
        self.assertEqual(
            browse_ids.detect_bot(env={}, cwd="/tmp", tmux_name="heavy-degero"),
            "degero",
        )
        self.assertTrue(str(browse_ids.profile_dir("shared")).endswith("/browser/profile"))
        self.assertIn("/profiles/bot-6344", str(browse_ids.profile_dir("bot-6344")))
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function browseBot()", html)
        self.assertIn("function browseBody(o)", html)

    def test_mobile_chat_can_open_browser(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="chatbr"', html)
        self.assertNotIn('id="findbtn"', html)
        self.assertNotIn('id="findbar"', html)
        self.assertIn("function openChatBrowser(", html)
        self.assertIn("browseFromChat", html)
        self.assertIn("#mhead #chatbr", html)
        paint = html.split("function paintBrowse(d){", 1)[1].split("function openBrowseView", 1)[0]
        self.assertIn("hasPage", paint)
        self.assertNotIn("botIsBrowsing(current)", paint)
        self.assertIn("browseFromChat=current?{id:current.id, slug:slugOf(current)}:null", html)
        self.assertIn("if($(\"chatbr\")) $(\"chatbr\").onclick=()=>openChatBrowser();", html)
        back = html.split('$("bback").onclick=async()=>{', 1)[1].split("$(\"bstage\")", 1)[0]
        self.assertIn("browseFromChat", back)
        self.assertIn("selectAgent(w.id, slugOf(w))", back)

    def test_mobile_browser_zoom_and_select(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function bindBrowsePad(", html)
        self.assertIn("function stickBrowseComposer(", html)
        self.assertIn('id="bzoombar"', html)
        self.assertIn("/api/browse/mouse", html)
        self.assertIn('sendMouse("dbl"', html)
        self.assertIn("d.editable", html)
        self.assertIn("Pinch to zoom", html)
        daemon = (ROOT / "browse_daemon.py").read_text(encoding="utf-8")
        self.assertIn('elif cmd == "mouse":', daemon)
        self.assertIn("def _focus_info(", daemon)
        self.assertIn("def _selected_text(", daemon)
        self.assertIn('"mouse"', daemon)

    def test_browse_actions_do_not_wait_for_shot(self):
        src = (ROOT / "browse_daemon.py").read_text(encoding="utf-8")
        click = src.split('elif cmd == "click":', 1)[1].split('elif cmd == "mouse":', 1)[0]
        typ = src.split('elif cmd == "type":', 1)[1].split('elif cmd == "key":', 1)[0]
        key = src.split('elif cmd == "key":', 1)[1].split('elif cmd == "scroll":', 1)[0]
        scroll = src.split('elif cmd == "scroll":', 1)[1].split('elif cmd == "back":', 1)[0]
        for block in (click, typ, key, scroll):
            self.assertNotIn("snap(", block)
            self.assertIn("request_shot()", block)
        self.assertIn("insert_text", typ)
        self.assertIn("disable-renderer-backgrounding", src)
        self.assertIn("want_shot", src)
        self.assertNotIn('got = call("shot"', src)
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("return 320", html)

    def test_all_chat_windows_pin_messages_above_composer(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("#chatpad,#nowturn,#nowlog{display:none !important}", html)
        self.assertIn("#chatpad{display:block", html)
        self.assertIn('pad.id="chatpad"', html)
        self.assertIn("el.insertBefore(pad, el.firstChild)", html)
        self.assertIn("el.clientHeight-el.scrollHeight", html)
        self.assertIn("#chatlog > .daychip:first-child", html)
        self.assertIn("#chatlog > .msg:first-child", html)
        self.assertIn("#chatlog > .msg.user:first-child", html)
        self.assertIn("margin-top:auto", html)
        self.assertIn("#chatlog .msg.user{align-self:flex-end;margin-left:auto", html)
        size_fn = html.split("function sizeChatEnd(){", 1)[1].split("function nearBottom", 1)[0]
        self.assertIn('el.querySelector(".empty")', size_fn)
        self.assertIn("if(pad) pad.remove()", size_fn)

    def test_turn_open_until_answer_after_tools(self):
        asked = [
            {"role": "user", "text": "doe dit"},
            {"role": "assistant", "text": "Leest · file", "meta": "tool"},
        ]
        self.assertTrue(server.turn_open(asked))
        talking = asked + [{"role": "assistant", "text": "Ik ga kijken."}]
        self.assertFalse(server.turn_open(talking))
        still = talking + [{"role": "assistant", "text": "Terminal · ls", "meta": "tool"}]
        self.assertTrue(server.turn_open(still))
        self.assertFalse(server.turn_open(still, live_waiting=False))
        done = still + [{"role": "assistant", "text": "Klaar, het werkt."}]
        self.assertFalse(server.turn_open(done))
        with_file = done + [{"role": "user", "text": "shot.png", "meta": "file"}]
        self.assertFalse(server.turn_open(with_file, live_waiting=False))
        self.assertTrue(server.turn_answered(None, "bot-x", with_file))

    def test_dispatch_after_answer_ignores_stale_busy(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A", "tmux": "heavy-bot-a"}}))
        win = {"slug": "bot-a", "tmux": "heavy-bot-a", "id": 1}
        msgs = [
            {"role": "user", "text": "eerste vraag over de site"},
            {"role": "assistant", "text": "Hier is het antwoord."},
        ]
        with mock.patch.object(server, "live_busy", return_value=True):
            with mock.patch.object(server, "turn_answered", return_value=True):
                with mock.patch.object(server, "start_helper") as helper:
                    with mock.patch.object(server, "steer_into_chat") as steer:
                        with mock.patch.object(server, "deliver_text") as deliver:
                            out = server.dispatch_text(win, "tweede vraag later", True)
        helper.assert_not_called()
        steer.assert_not_called()
        deliver.assert_called_once()
        self.assertFalse(out.get("choice"))
        self.assertFalse(out.get("inbox"))

    def test_unmatched_old_swarm_stays_out_when_session_has_newer(self):
        session = [{"role": "user", "text": "nieuwe vraag"}]
        have = {server._norm_txt("nieuwe vraag")}
        swarm = [
            {
                "role": "user",
                "text": "oude vraag die al weg was",
                "at": "2026-08-17T11:54:18+00:00",
            },
            {"role": "user", "text": "nieuwe vraag", "at": "2026-08-17T12:23:38+00:00"},
        ]
        got = server.merge_swarm_users(session, swarm, have)
        texts = [m["text"] for m in got]
        self.assertEqual(texts, ["oude vraag die al weg was", "nieuwe vraag"])

    def test_deliver_text_refuses_without_tty(self):
        with mock.patch.object(server.agents_tmux, "list_sessions", return_value=[]):
            with mock.patch.object(server, "ensure_tmux_for_agent", side_effect=RuntimeError("no bot to start")):
                with mock.patch.object(server, "type_text") as typ:
                    with mock.patch.object(server, "focus_window") as foc:
                        with self.assertRaises(RuntimeError):
                            server.deliver_text(
                                {"id": 17944, "slug": "bot-17944", "title": "timgrootes — grok"},
                                "lek naar ads",
                                True,
                            )
                        typ.assert_not_called()
                        foc.assert_not_called()

    def test_upsert_loop_persists_and_lists(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A"}}))
        item = rosterlib.upsert_loop("bot-a", "Check site", "30m", "kijk of de site live is")
        self.assertEqual(item["name"], "Check site")
        self.assertEqual(item["every_min"], 30)
        self.assertEqual(item["prompt"], "kijk of de site live is")
        disk = json.loads((self.agents_dir / "bot-a" / "loops.json").read_text())
        self.assertEqual(len(disk), 1)
        self.assertEqual(disk[0]["prompt"], "kijk of de site live is")
        pub = rosterlib.public_loops("bot-a")
        self.assertEqual(len(pub), 1)
        self.assertEqual(pub[0]["every"], "every 30 min")
        self.assertIn("kijk of de site", pub[0]["prompt"])

    def test_upsert_loop_dedupes_same_prompt_and_interval(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "A"}}))
        a = rosterlib.upsert_loop("bot-a", "A", "1h", "check ads")
        b = rosterlib.upsert_loop("bot-a", "B", "1h", "check ads")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(rosterlib.load_loops("bot-a")), 1)

    def test_interval_from_fields_and_parse(self):
        self.assertEqual(rosterlib.interval_from_fields(30, "m"), "30m")
        self.assertEqual(rosterlib.interval_from_fields("2", "uur"), "2h")
        self.assertEqual(rosterlib.parse_interval("2h"), ("2h", 120))
        self.assertEqual(rosterlib.parse_interval(rosterlib.interval_from_fields(2, "h")), ("2h", 120))
        self.assertIsNone(rosterlib.parse_interval("0m"))
        with self.assertRaises(ValueError):
            rosterlib.upsert_loop("bot-a", "x", "30m", "")

    def test_schema_bar_does_not_flex_shrink(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("#schema.on{display:flex}", html)
        self.assertIn('el.classList.add("on")', html)
        self.assertIn("function paintSchema(w)", html)
        self.assertIn('class="looprow"', html)
        self.assertIn('id="schema"', html)

    def test_public_roster_exposes_loops_for_header(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "ADS bot", "window_id": 1}}))
        rosterlib.upsert_loop("bot-a", "Ads checken", "1h", "check apple ads")
        pub = rosterlib.public_roster([], lambda *_: "ADS bot")
        ads = next(a for a in pub["agents"] if a["slug"] == "bot-a")
        self.assertEqual(len(ads["loops"]), 1)
        self.assertEqual(ads["loops"][0]["name"], "Ads checken")
        self.assertEqual(ads["loops"][0]["every"], "every hour")


    def test_empty_session_keeps_full_swarm_history(self):
        swarm = [
            {"role": "user", "text": "eerste vraag van ads", "at": "2026-08-17T10:00:00+00:00"},
            {"role": "user", "text": "tweede vraag van ads", "at": "2026-08-17T11:00:00+00:00"},
            {"role": "assistant", "text": "Working", "meta": "progress", "at": "2026-08-17T11:00:01+00:00"},
        ]
        got = server.merge_swarm_users([], swarm, set())
        texts = [m["text"] for m in got]
        self.assertEqual(texts, ["eerste vraag van ads", "tweede vraag van ads"])

    def test_slug_from_title_ignores_random_grok_tui(self):
        self.assertEqual(rosterlib.slug_from_title("bot-71128 — grok"), "bot-71128")
        self.assertEqual(rosterlib.slug_from_title("Waiting for response… — bot-43016 — grok"), "bot-43016")
        self.assertEqual(rosterlib.slug_from_title("naptara — grok"), "naptara")
        self.assertEqual(
            rosterlib.slug_from_title(
                "timgrootes — ⠸ - Writing command… - Fix swarm bot windows - grok"
            ),
            "",
        )
        self.assertTrue(server.is_grok_agent_window("timgrootes — Writing command… - grok", 800, 600))
        self.assertTrue(server.is_grok_agent_window("bot-71128 — grok", 800, 600))

    def test_stray_grok_window_does_not_mint_agent(self):
        rosterlib.save_roster(_roster({"bot-a": {"label": "ADS bot", "window_id": 1, "tmux": "heavy-bot-a"}}))
        rost = rosterlib.sync_from_windows(
            [
                {"id": 1, "title": "bot-a — grok", "tmux": "heavy-bot-a"},
                {
                    "id": 22947,
                    "title": "timgrootes — Writing command… - Fix swarm - grok",
                },
            ],
            lambda w: "Bot",
        )
        self.assertIn("bot-a", rost["agents"])
        self.assertNotIn("bot-22947", rost["agents"])
        self.assertEqual(len(rost["agents"]), 1)

    def test_find_agent_by_title_after_window_id_change(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-71128": {
                        "label": "ADS bot",
                        "window_id": 111,
                        "tmux": "heavy-bot-71128",
                    }
                }
            )
        )
        slug = rosterlib._find_agent_slug(
            rosterlib.load_roster(),
            {"id": 999, "title": "bot-71128 — grok"},
        )
        self.assertEqual(slug, "bot-71128")

    def test_window_for_slug_survives_missing_window(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {
                        "label": "ADS bot",
                        "window_id": 1,
                        "tmux": "heavy-bot-a",
                        "session_id": "sid-a",
                    }
                }
            )
        )
        with mock.patch.object(server, "find_window", return_value=None):
            win = server.window_for_slug("bot-a")
        self.assertIsNotNone(win)
        self.assertEqual(win["slug"], "bot-a")
        self.assertEqual(win["tmux"], "heavy-bot-a")

    def test_slug_beats_recycled_window_id(self):
        rosterlib.save_roster(
            _roster(
                {
                    "bot-a": {"label": "ADS bot", "window_id": 5, "tmux": "heavy-bot-a"},
                    "bot-b": {"label": "Degero", "window_id": 9, "tmux": "heavy-bot-b"},
                }
            )
        )
        # Window 5 now belongs to Degero's Terminal (recycled Quartz id).
        got = server.slug_for_window({"id": 5, "title": "bot-b — grok", "tmux": "heavy-bot-b"})
        self.assertEqual(got, "bot-b")

    def test_prune_does_nothing_without_roster(self):
        rosterlib.save_roster(_roster({}))
        with mock.patch.object(server.agents_tmux, "kill") as killed:
            with mock.patch.object(server.os, "kill") as oskill:
                server.prune_orphan_sessions()
        killed.assert_not_called()
        oskill.assert_not_called()

    def test_frontend_loads_chat_by_slug(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function chatKey(w)", html)
        self.assertIn("function sameChat(w, want)", html)
        self.assertIn("&slug=", html)
        self.assertIn("chatKey(current)", html)


class IsolationLive(unittest.TestCase):
    """Hits the running Swarm server when it is up."""

    def setUp(self) -> None:
        token = Path.home() / ".grok" / "imac-phone" / "token"
        if not token.is_file():
            self.skipTest("no token")
        self.key = token.read_text().strip()
        try:
            self.state = self._get("/api/state")
        except Exception as exc:
            self.skipTest(f"server down: {exc}")

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:8790{path}",
            headers={"X-Remote-Key": self.key},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())

    def test_each_bot_has_unique_session(self):
        seen = {}
        for a in (self.state.get("roster") or {}).get("agents") or []:
            sid = a.get("session") or ""
            # session lives on roster file, not always public; skip if absent
            slug = a.get("slug")
            seen[slug] = a.get("window_id")
        self.assertGreaterEqual(len(seen), 2)
        ids = [a.get("window_id") for a in (self.state.get("roster") or {}).get("agents") or []]
        self.assertEqual(len(ids), len(set(ids)), "window_id must be unique")

    def test_chat_payloads_do_not_share_transcripts(self):
        agents = (self.state.get("roster") or {}).get("agents") or []
        bags = []
        for a in agents:
            wid = a.get("window_id")
            if wid is None:
                continue
            chat = self._get(f"/api/chat?id={wid}")
            self.assertEqual(str(chat.get("for_id")), str(wid))
            if chat.get("slug"):
                self.assertEqual(chat["slug"], a.get("slug"))
            texts = tuple(
                (m.get("role"), (m.get("text") or "")[:160])
                for m in (chat.get("messages") or [])
                if not m.get("helper")
            )
            if texts:
                bags.append((a.get("slug"), texts, chat.get("session") or ""))
        # Distinct sessions must not carry the same full transcript.
        by_sid = {}
        for slug, texts, sid in bags:
            if not sid:
                continue
            if sid in by_sid:
                self.assertEqual(by_sid[sid][0], slug, f"session {sid} claimed by {by_sid[sid][0]} and {slug}")
            by_sid[sid] = (slug, texts)
        pairs = [(s, t) for _, t, s in bags if t]
        for i, (sa, ta) in enumerate(pairs):
            for sb, tb in pairs[i + 1 :]:
                if sa and sb and sa != sb:
                    self.assertNotEqual(ta, tb, f"identical chat for different sessions {sa} vs {sb}")

    def test_swarm_send_stays_on_one_bot(self):
        agents = (self.state.get("roster") or {}).get("agents") or []
        target = next((a for a in agents if a.get("slug") == "bot-97564"), None)
        if not target or target.get("window_id") is None:
            self.skipTest("Sitebirds not in roster")
        marker = f"ISO-{int(time.time())} zeg alleen ISO-OK"
        body = json.dumps(
            {
                "id": target["window_id"],
                "slug": "bot-97564",
                "tmux": "heavy-bot-97564",
                "text": marker,
                "submit": True,
                "busy": False,
            }
        ).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:8790/api/type",
            data=body,
            headers={"X-Remote-Key": self.key, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            out = json.loads(r.read())
        self.assertTrue(out.get("ok"), out)
        self.assertFalse(out.get("helper"), f"idle bot spawned helper: {out}")
        time.sleep(1.4)
        hits = []
        for a in agents:
            wid = a.get("window_id")
            if wid is None:
                continue
            chat = self._get(f"/api/chat?id={wid}")
            blob = "\n".join(m.get("text") or "" for m in (chat.get("messages") or []))
            if marker.split()[0] in blob:
                hits.append(a.get("slug"))
        self.assertEqual(hits, ["bot-97564"], f"marker leaked to {hits}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
