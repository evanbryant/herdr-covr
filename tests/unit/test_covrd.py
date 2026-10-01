"""Unit tests for the daemon's pure logic (stdlib unittest, Python 3.8+, no herdr needed).

    python3 -m unittest discover -s tests/unit -v

herdr is replaced by a fake `call()` fed with agent/workspace/tab lists, so compute() runs exactly as in the
daemon. Several tests pin down regressions from real use (marked "regression:").
"""
import json, os, sys, tempfile, time, unittest

_TMP = tempfile.mkdtemp(prefix="covr-unit-")
for k, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
               ("HERDR_PLUGIN_CONFIG_DIR", os.path.join("herdr-config", "plugins", "config", "covr.sidebar")),
               ("HERDR_PLUGIN_STATE_DIR", os.path.join("herdr-state", "plugins", "covr.sidebar"))):
    os.environ[k] = os.path.join(_TMP, sub)
    os.makedirs(os.environ[k], exist_ok=True)
os.environ["HERDR_SOCKET_PATH"] = os.path.join(_TMP, "none", "herdr.sock")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin"))
import covrd  # noqa: E402
import layout  # noqa: E402

BLANK = covrd.BLANK


class FakeHerdr:
    """Stands in for the socket: serves agent/workspace/tab/pane lists, records every write."""

    def __init__(self, agents, spaces, tabs=()):
        self.agents, self.spaces, self.tabs, self.calls = agents, spaces, list(tabs), []

    def __call__(self, method, params=None):
        self.calls.append((method, params))
        if method == "agent.list":
            return {"agents": self.agents}
        if method == "workspace.list":
            return {"workspaces": self.spaces}
        if method == "tab.list":
            return {"tabs": self.tabs}
        if method == "pane.list":
            return {"panes": [{"pane_id": a["pane_id"], "workspace_id": a["workspace_id"], "cwd": None} for a in self.agents]}
        if method == "pane.read":
            return {"read": {"text": "Bash(rm -rf build/)\nDo you want to proceed?\n❯ 1. Yes"}}
        return {}


class FakeClock:
    """Replaces the daemon's `time` module so tests can move the clock."""

    def __init__(self, now=1_790_000_000.0):
        self.now = now

    def time(self):
        return self.now

    def sleep(self, _):
        pass

    def strftime(self, *a):
        return time.strftime(*a)


def agent(pid, ws, status, kind="claude", seq=1, title="", tab=None):
    return {"pane_id": pid, "workspace_id": ws, "agent_status": status, "agent": kind, "state_change_seq": seq,
            "terminal_title_stripped": title, "tab_id": tab or f"{ws}:t1", "cwd": None, "tokens": {}}


def space(wid, label, number, **kw):
    return dict({"workspace_id": wid, "label": label, "number": number}, **kw)


def opts(**kw):
    o = dict(covrd.DEFAULTS)
    o.update(kw)
    return o


class Base(unittest.TestCase):
    def setUp(self):
        self._call = covrd.call
        with open(covrd.PINS, "w", encoding="utf-8") as f:
            json.dump({"agents": [], "spaces": []}, f)

    def tearDown(self):
        covrd.call = self._call

    def run_compute(self, herdr, opt=None, memo=None):
        covrd.call = herdr
        memo = memo if memo is not None else {}
        memo.setdefault("dirty_at", 1e18)  # skip the git dirty check
        out, wout, view = covrd.compute(opt or opts(), memo)
        return out, wout, view, memo

    @staticmethod
    def text(head):
        """A $head without its padding and age: 'glyph label · parts'."""
        return head.split(BLANK)[0].rstrip()


class Layout(unittest.TestCase):
    def test_age_is_flush_right_at_common_widths(self):
        for width in (26, 32, 36):
            row = covrd.pinned_row("○", "api · auth", "12m", width)
            self.assertEqual(covrd.cells(row), width - 3, width)
            self.assertTrue(row.endswith(" 12m"))

    def test_regression_space_name_is_shortened_before_the_tab(self):
        row = covrd.pinned_row("⏾", "data-pipeline-service · billing", "39m", 32)
        self.assertIn("· billing", row)
        self.assertIn("…", row.split(" · ")[0])
        self.assertTrue(row.endswith(" 39m"))

    def test_ellipsis_never_eats_the_age(self):
        row = covrd.pinned_row("×", "x" * 200, "3h", 26)
        self.assertTrue(row.endswith(" 3h"))
        self.assertIn("…", row)
        self.assertEqual(covrd.cells(row), 23)

    def test_wide_sidebar_stays_under_herdrs_80_char_cap(self):
        row = covrd.pinned_row("◐", "api", "<1m", 150)
        self.assertLessEqual(len(row), 80)
        self.assertTrue(row.endswith(" <1m"))

    def test_wide_characters_count_two_cells(self):
        self.assertEqual(covrd.cells("日本"), 4)
        self.assertEqual(covrd.cells("é"), 1)
        row = covrd.pinned_row("○", "日本語のプロジェクト名です", "5m", 26)
        self.assertLessEqual(covrd.cells(row), 23)

    def test_no_age_means_no_padding(self):
        self.assertEqual(covrd.pinned_row("○", "api", "", 30), "○ api")

    def test_fmt_age_boundaries(self):
        cases = {None: "", 0: "<1m", 59: "<1m", 60: "1m", 3599: "59m", 3600: "1h", 5400: "1h30m",
                 10 * 3600 + 60: "10h", 86399: "23h", 86400: "1d", 3 * 86400: "3d"}
        for sec, want in cases.items():
            self.assertEqual(covrd.fmt_age(sec), want, sec)

    def test_short_tag_never_cuts_words(self):
        self.assertEqual(covrd.short_tag("Fix login bug in auth"), "Fix login")
        self.assertEqual(covrd.short_tag("Supercalifragilistic word"), "Supercalifr…")
        self.assertEqual(covrd.short_tag("Refactoring everything"), "Refactoring")

    def test_tab_label_modes(self):
        self.assertEqual(covrd.tab_label("3", "named"), "")
        self.assertEqual(covrd.tab_label("review", "named"), "review")
        self.assertEqual(covrd.tab_label("3", "always"), "3")
        self.assertEqual(covrd.tab_label("review", "never"), "")

    def test_clip(self):
        self.assertEqual(covrd.clip("short"), "short")
        c = covrd.clip("word " * 40)
        self.assertEqual(len(c), 80)
        self.assertTrue(c.endswith("…"))


class Ordering(Base):
    def herdr(self):
        return FakeHerdr(
            [agent("p1", "w1", "idle", seq=1), agent("p2", "w2", "blocked", seq=2), agent("p3", "w3", "working", seq=3),
             agent("p4", "w1", "done", seq=4), agent("p5", "w2", "idle", seq=5)],
            [space("w1", "api", 1), space("w2", "web", 2), space("w3", "infra", 3)])

    def test_regression_rank_is_stable_while_states_hold(self):
        # sorting on a ticking age made rows swap and revert; rank must only change with the state
        h, memo = self.herdr(), {}
        clock = FakeClock()
        saved, covrd.time = covrd.time, clock
        try:
            self.run_compute(h, memo=memo)       # first sight: start times unknown
            h.agents[0].update(agent_status="working", state_change_seq=9)
            first, *_ = self.run_compute(h, memo=memo)
            for _ in range(20):                   # 20 ticks, 5 s apart: ages move, ranks must not
                clock.now += 5
                out, *_ = self.run_compute(h, memo=memo)
                self.assertEqual({p: t["rank"] for p, t in out.items()}, {p: t["rank"] for p, t in first.items()})
            self.assertNotEqual(covrd.fmt_age(int(clock.now - memo["since"]["p1"]["since"])), "<1m")
        finally:
            covrd.time = saved

    def test_rank_changes_when_state_changes(self):
        h, memo = self.herdr(), {}
        before, *_ = self.run_compute(h, memo=memo)
        h.agents[0].update(agent_status="working", state_change_seq=9)
        after, *_ = self.run_compute(h, memo=memo)
        self.assertNotEqual(before["p1"]["rank"], after["p1"]["rank"])
        self.assertEqual(before["p2"]["rank"], after["p2"]["rank"])

    def test_view_sorts_pins_then_attention_then_rank(self):
        _, _, view, _ = self.run_compute(self.herdr())
        fields = [s["field"] for s in view["sort"]]
        self.assertEqual(fields, [{"token": "pin"}, "attention", {"token": "rank"}])
        self.assertEqual(view["source"], "plugin:covr.sidebar")

    def test_views_filter(self):
        need = {"op": "in", "field": "status", "values": ["blocked", "done"]}
        self.assertNotIn("filter", covrd.view_params(opts(view="triage"), "none"))
        self.assertEqual(covrd.view_params(opts(view="needs me"), "none")["filter"], need)
        here = covrd.view_params(opts(view="here+"), "none")["filter"]
        self.assertEqual(here["op"], "any")
        self.assertIn(need, here["filters"])

    def test_glyphs_by_state(self):
        out, *_ = self.run_compute(self.herdr())
        self.assertTrue(out["p2"]["head"].startswith("× web"))
        self.assertTrue(out["p4"]["head"].startswith("✓ api"))
        self.assertTrue(out["p3"]["head"].startswith("◐ infra"))

    def test_blocked_gets_a_reason_row_and_done_a_summary(self):
        h = self.herdr()
        h.agents[3]["terminal_title_stripped"] = "Fix tab rename bug"
        out, *_ = self.run_compute(h)
        self.assertEqual(out["p2"]["wait"], "↳ Bash(rm -rf build/)")
        self.assertEqual(out["p4"]["done"], "↳ Fix tab rename bug")
        self.assertNotIn("wait", out["p1"])

    def test_stale_idle_falls_asleep(self):
        h, memo = self.herdr(), {}
        self.run_compute(h, memo=memo)
        memo["since"]["p1"]["since"] = time.time() - 7200  # idle for two hours
        out, *_ = self.run_compute(h, opt=opts(stale_after="1h"), memo=memo)
        self.assertTrue(out["p1"]["head"].startswith("⏾ api"))

    def test_pinned_agent_leads_and_is_marked(self):
        with open(covrd.PINS, "w", encoding="utf-8") as f:
            json.dump({"agents": ["p1"], "spaces": []}, f)
        out, *_ = self.run_compute(self.herdr())
        self.assertEqual(out["p1"]["pin"], "0")
        self.assertIn("★", out["p1"]["head"])
        self.assertNotIn("pin", out["p2"])


class Viewed(Base):
    """A finished agent (✓) becoming viewed (○): the timer restarts, and the selected one is viewed automatically."""

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self._time, covrd.time = covrd.time, self.clock

    def tearDown(self):
        covrd.time = self._time
        super().tearDown()

    def herdr(self, focused):
        a = agent("p1", "w1", "working", seq=4)
        a["focused"] = focused
        return FakeHerdr([a], [space("w1", "api", 1)])

    def finish(self, h, memo, opt=None):
        self.run_compute(h, opt=opt, memo=memo)                  # seen while working
        h.agents[0].update(agent_status="done", state_change_seq=5)
        return self.run_compute(h, opt=opt, memo=memo)[0]

    def test_timer_restarts_when_a_finished_agent_is_viewed(self):
        h, memo = self.herdr(focused=False), {}
        self.assertTrue(self.finish(h, memo)["p1"]["head"].startswith("✓ api"))
        self.clock.now += 600                                     # ten minutes later you look at it:
        h.agents[0]["agent_status"] = "idle"                      # herdr reports idle, same state_change_seq
        out, *_ = self.run_compute(h, memo=memo)
        self.assertTrue(out["p1"]["head"].startswith("○ api"))
        self.assertTrue(out["p1"]["head"].endswith(" <1m"))       # idle counts from the view, not from the finish
        self.clock.now += 1900                                    # ... and so does asleep (30m)
        out, *_ = self.run_compute(h, memo=memo)
        self.assertTrue(out["p1"]["head"].startswith("⏾ api"))

    def test_selected_finished_agent_is_marked_viewed_after_seen_after(self):
        h, memo = self.herdr(focused=True), {}
        out = self.finish(h, memo)
        self.assertTrue(out["p1"]["head"].startswith("✓ api"))
        self.assertNotIn("agent.focus", [m for m, _ in h.calls])  # not before 5 s
        self.assertAlmostEqual(memo["wake_at"], self.clock.now + 5)
        self.clock.now += 5
        out, *_ = self.run_compute(h, memo=memo)
        self.assertIn(("agent.focus", {"target": "p1"}), h.calls)
        self.assertTrue(out["p1"]["head"].startswith("○ api"))    # shown as viewed at once
        self.assertTrue(out["p1"]["head"].endswith(" <1m"))
        self.assertIsNone(memo["wake_at"])

    def test_unselected_finished_agents_are_never_marked(self):
        h, memo = self.herdr(focused=False), {}
        self.finish(h, memo)
        self.clock.now += 60
        out, *_ = self.run_compute(h, memo=memo)
        self.assertNotIn("agent.focus", [m for m, _ in h.calls])
        self.assertTrue(out["p1"]["head"].startswith("✓ api"))

    def test_seen_after_off_and_no_busy_wake(self):
        h, memo = self.herdr(focused=True), {}
        self.finish(h, memo, opt=opts(seen_after="off"))
        self.clock.now += 60
        self.run_compute(h, opt=opts(seen_after="off"), memo=memo)
        self.assertNotIn("agent.focus", [m for m, _ in h.calls])
        self.assertIsNone(memo["wake_at"])
        # switching it off while one is pending must clear the wake-up, or the daemon would spin
        memo2 = {}
        self.finish(self.herdr(focused=True), memo2)
        h2 = self.herdr(focused=True); h2.agents[0].update(agent_status="done", state_change_seq=5)
        self.run_compute(h2, opt=opts(seen_after="off"), memo=memo2)
        self.assertIsNone(memo2["wake_at"])

    def test_failed_focus_is_retried_later_not_immediately(self):
        h, memo = self.herdr(focused=True), {}
        self.finish(h, memo)
        self.clock.now += 5
        real = h.__call__

        def failing(method, params=None):
            if method == "agent.focus":
                raise RuntimeError("boom")
            return FakeHerdr.__call__(h, method, params)
        covrd.call = failing
        memo.setdefault("dirty_at", 1e18)
        saved_log, covrd.log = covrd.log, lambda *a: None
        try:
            covrd.compute(opts(), memo)
        finally:
            covrd.log = saved_log
        self.assertGreater(memo["wake_at"], self.clock.now)       # pushed into the future

    def test_seen_after_values(self):
        for v in ("off", "5s", "90", "1m", "1h"):
            self.assertIsNone(covrd.validate("seen_after", v)[1], v)
        for v in ("soon", "0s", "2h", ""):
            self.assertIsNotNone(covrd.validate("seen_after", v)[1], v)


class Identity(Base):
    def test_regression_tab_names_show_on_every_row(self):
        h = FakeHerdr([agent("p1", "w1", "idle", tab="w1:t1"), agent("p2", "w1", "working", seq=2, tab="w1:t2")],
                      [space("w1", "web", 1)],
                      [{"tab_id": "w1:t1", "label": "review"}, {"tab_id": "w1:t2", "label": "2"}])
        out, *_ = self.run_compute(h)
        self.assertEqual(self.text(out["p1"]["head"]), "○ web · review")
        self.assertEqual(self.text(out["p2"]["head"]), "◐ web")  # an auto-numbered tab is hidden

    def test_regression_no_kind_label_when_only_one_kind_runs(self):
        # group_by = kind with only claude agents printed "claude" on the top row
        h = FakeHerdr([agent("p1", "w1", "idle"), agent("p2", "w2", "blocked", seq=2)],
                      [space("w1", "api", 1), space("w2", "docs", 2)])
        out, *_ = self.run_compute(h, opt=opts(group_by="kind"))
        for t in out.values():
            self.assertNotIn("claude", t["head"])
            self.assertNotIn("rule", t)

    def test_kind_groups_label_first_row_and_close_with_a_rule(self):
        h = FakeHerdr([agent("p1", "w1", "idle", kind="claude"), agent("p2", "w2", "working", kind="codex", seq=2),
                       agent("p3", "w2", "idle", kind="codex", seq=3)],
                      [space("w1", "api", 1), space("w2", "web", 2)])
        out, *_ = self.run_compute(h, opt=opts(group_by="kind"))
        labelled = [p for p, t in out.items() if "codex" in t["head"] or "claude" in t["head"]]
        self.assertEqual(len(labelled), 2)  # once per group
        self.assertEqual(sum(1 for t in out.values() if "rule" in t), 1)

    def test_look_alike_rows_get_task_tags(self):
        h = FakeHerdr([agent("p1", "w1", "idle", title="Docs refresh"), agent("p2", "w1", "idle", seq=2, title="Login bug")],
                      [space("w1", "web", 1)])
        out, *_ = self.run_compute(h)
        self.assertEqual(self.text(out["p1"]["head"]), "○ web · Docs refresh")
        self.assertEqual(self.text(out["p2"]["head"]), "○ web · Login bug")

    def test_regression_stale_and_idle_are_not_look_alikes(self):
        h, memo = FakeHerdr([agent("p1", "w1", "idle", title="Docs refresh"), agent("p2", "w1", "idle", seq=2, title="Login bug")],
                            [space("w1", "web", 1)]), {}
        self.run_compute(h, memo=memo)
        memo["since"]["p1"]["since"] = time.time() - 7200
        out, *_ = self.run_compute(h, opt=opts(stale_after="1h"), memo=memo)
        self.assertEqual(self.text(out["p1"]["head"]), "⏾ web")
        self.assertEqual(self.text(out["p2"]["head"]), "○ web")

    def test_project_grouping_names_the_project_once(self):
        h = FakeHerdr([agent("p1", "w1", "blocked", title="Checkout flow"), agent("p2", "w1", "idle", seq=2, title="Docs refresh"),
                       agent("p3", "w2", "working", seq=3)],
                      [space("w1", "web", 1), space("w2", "api", 2)])
        out, _, view, _ = self.run_compute(h, opt=opts(group_by="project"))
        self.assertTrue(self.text(out["p1"]["head"]).startswith("× web"))
        self.assertTrue(out["p2"]["head"].startswith("○ " + BLANK * 2))  # member row: indented, no project name
        self.assertLess(out["p1"]["grp"], out["p3"]["grp"])  # the blocked agent's project leads
        self.assertEqual(view["sort"][0]["field"], {"token": "grp"})
        self.assertEqual(view["label"], "by project")


class Options(unittest.TestCase):
    def test_parse_value(self):
        cases = {'"a" # c': ("a", True), "'b'": ("b", True), '"a\\"b"': ('a"b', True), "true": (True, True),
                 "false # x": (False, True), " 7 ": (7, True), "-3": (-3, True), '"open': (None, False), "bare": (None, False)}
        for raw, want in cases.items():
            self.assertEqual(covrd.parse_value(raw), want, raw)

    def test_read_flat_reads_top_level_keys_only(self):
        p = os.path.join(_TMP, "flat.toml")
        with open(p, "w", encoding="utf-8") as f:
            f.write('# c\nview = "needs me"  # trailing\ntick_seconds = 7\n\n[table]\nview = "x"\n')
        self.assertEqual(covrd.read_flat(p), {"view": "needs me", "tick_seconds": 7})

    def test_validate(self):
        ok = [("view", "here+"), ("stale_after", "45m"), ("stale_after", "2h"), ("tick_seconds", "7"), ("disambiguate", "off")]
        bad = [("view", "bogus"), ("stale_after", "soon"), ("stale_after", "10s"), ("stale_after", "31d"),
               ("tick_seconds", "x"), ("tick_seconds", 1), ("disambiguate", "maybe"), ("nope", 1)]
        for k, v in ok:
            self.assertIsNone(covrd.validate(k, v)[1], (k, v))
        for k, v in bad:
            self.assertIsNotNone(covrd.validate(k, v)[1], (k, v))

    def test_options_ignore_bad_values_and_keep_good_ones(self):
        with open(os.path.join(covrd.CFG_DIR, "config.toml"), "w", encoding="utf-8") as f:
            f.write('view = "bogus"\nspace_sort = "alpha"\ntick_seconds = "x"\nunknown = 1\n')
        covrd.notify = lambda body: None
        o = covrd.options()
        self.assertEqual((o["view"], o["space_sort"], o["tick_seconds"]), ("triage", "alpha", 5))

    def test_options_without_tomllib_match(self):
        with open(os.path.join(covrd.CFG_DIR, "config.toml"), "w", encoding="utf-8") as f:
            f.write('group_by = "project"  # c\nspace_sort = \'alpha\'\ndisambiguate = false\nstale_after = "2h"\n')
        with_toml = covrd.options()
        saved, covrd.tomllib = covrd.tomllib, None
        try:
            self.assertEqual(covrd.options(), with_toml)
        finally:
            covrd.tomllib = saved

    def test_write_option_cycles_and_refuses_bad_values(self):
        covrd.write_option("view", "triage")
        self.assertEqual(covrd.write_option("view", "cycle"), "needs me")
        with self.assertRaises(ValueError):
            covrd.write_option("view", "bogus")
        self.assertEqual(covrd.options()["view"], "needs me")

    def test_seconds(self):
        self.assertEqual([covrd.seconds(s) for s in ("90", "45m", "2h", "1d")], [90, 2700, 7200, 86400])


class Lifecycle(unittest.TestCase):
    def test_fnv1a64_known_vectors(self):
        self.assertEqual(covrd.fnv1a64(b""), 0xcbf29ce484222325)
        self.assertEqual(covrd.fnv1a64(b"a"), 0xaf63dc4c8601ec8c)

    def test_client_prefs_path_follows_the_session_socket(self):
        saved = covrd.SOCK
        try:
            covrd.SOCK = os.path.join(_TMP, "herdr", "sessions", "two", "herdr.sock")
            client = os.path.join(os.path.dirname(covrd.SOCK), "herdr-client.sock")
            want = f"local-{covrd.fnv1a64(client.encode()):016x}.json"
            self.assertTrue(covrd.client_prefs_path().endswith(os.path.join("client-shell", want)))
        finally:
            covrd.SOCK = saved

    def test_herdr_dirs_come_from_the_plugin_dirs(self):
        # herdr passes <config>/plugins/config/<id> and <state>/plugins/<id>; the herdr dirs are above them
        self.assertEqual(covrd.HERDR_CONFIG_DIR, os.path.join(_TMP, "herdr-config"))
        self.assertEqual(covrd.HERDR_STATE_DIR, os.path.join(_TMP, "herdr-state"))
        self.assertEqual(covrd.HERDR_CONFIG, os.path.join(_TMP, "herdr-config", "config.toml"))

    def test_file_contains_finds_text_across_chunk_boundaries(self):
        p = os.path.join(_TMP, "transcript.jsonl")
        with open(p, "wb") as f:
            f.write(b"x" * ((1 << 20) - 3) + "Fix checkout redirect".encode() + b"y" * 10)
        self.assertTrue(covrd.file_contains(p, "Fix checkout redirect"))
        self.assertFalse(covrd.file_contains(p, "Something else"))
        self.assertFalse(covrd.file_contains(os.path.join(_TMP, "missing.jsonl"), "x"))

    def test_wake_file_words(self):
        woke, memo = {"flag": False, "stop": False}, {}
        covrd.take_wake(woke, memo)  # no file: nothing happens
        self.assertEqual((woke["flag"], memo), (False, {}))
        os.makedirs(covrd.RUN_DIR, exist_ok=True)
        with open(covrd.WAKE, "w", encoding="utf-8") as f:
            f.write("wake\nresync\n")
        covrd.take_wake(woke, memo)
        self.assertTrue(woke["flag"] and memo.get("resync") and not woke["stop"])
        self.assertFalse(os.path.exists(covrd.WAKE))
        with open(covrd.WAKE, "w", encoding="utf-8") as f:
            f.write("stop\n")
        covrd.take_wake(woke, memo)
        self.assertTrue(woke["stop"])

    def test_seq_strictly_increases_even_if_the_clock_steps_back(self):
        covrd.SEQ["last"] = 10 ** 13  # far in the future
        a, b = covrd.next_seq(), covrd.next_seq()
        self.assertEqual((a, b), (10 ** 13 + 1, 10 ** 13 + 2))

    def test_lost_tokens_needs_all_of_a_panes_tokens_gone(self):
        memo = {"pushed": {"p1": {"head": "x", "rank": "1"}}, "live": {"p1": {"rank"}}}
        self.assertFalse(covrd.lost_tokens(memo))  # one value dropped (e.g. sanitised): not a restart
        memo["live"] = {"p1": set()}
        self.assertTrue(covrd.lost_tokens(memo))
        memo["live"] = {}
        self.assertFalse(covrd.lost_tokens(memo))  # the pane is gone, not lost

    def test_transcripts_are_only_read_when_opted_in(self):
        looked = []
        saved, covrd.transcript_mtime = covrd.transcript_mtime, lambda *a: looked.append(a) or 1_000_000.0
        saved_call = covrd.call
        covrd.call = FakeHerdr([agent("p1", "w1", "idle", title="Docs refresh")], [space("w1", "web", 1)])
        try:
            out, _, _ = covrd.compute(opts(), {"dirty_at": 1e18})
            self.assertEqual(looked, [])                       # default: nothing outside herdr is read
            self.assertEqual(out["p1"]["head"], "○ web")      # so there is no age yet
            out, _, _ = covrd.compute(opts(age_source="claude-transcripts"), {"dirty_at": 1e18})
            self.assertEqual(len(looked), 1)
            self.assertNotEqual(out["p1"]["head"], "○ web")
        finally:
            covrd.transcript_mtime, covrd.call = saved, saved_call

    def test_tcache_keys_are_hashes(self):
        k = covrd.tkey("/work/secret-repo", "Secret title")
        self.assertTrue(k.startswith("h:"))
        self.assertNotIn("secret", k.lower())


class Spaces(Base):
    SPACES = [space("w1", "web", 1), space("w2", "api", 2), space("w3", "infra", 3)]

    def plan(self, sort, memo=None, pins=None, rows=()):
        memo = memo if memo is not None else {}
        return covrd.plan_spaces(opts(space_sort=sort), {w["workspace_id"]: w for w in self.SPACES}, list(rows),
                                 pins or {"agents": [], "spaces": []}, memo, 0), memo

    def test_manual_never_moves_anything(self):
        (plan, _) = self.plan("manual")
        self.assertEqual(plan["want"], plan["cur"])

    def test_alpha_and_pins_lead(self):
        self.assertEqual(self.plan("alpha")[0]["want"], ["w2", "w3", "w1"])
        pinned = self.plan("alpha", pins={"agents": [], "spaces": ["w1"]})[0]
        self.assertEqual(pinned["want"], ["w1", "w2", "w3"])

    def test_regression_manual_order_is_restored_after_another_sort(self):
        memo = {}
        self.plan("manual", memo)                     # snapshot: w1 w2 w3
        self.assertEqual(self.plan("alpha", memo)[0]["want"], ["w2", "w3", "w1"])
        self.SPACES = [space("w2", "api", 1), space("w3", "infra", 2), space("w1", "web", 3)]  # herdr now alpha
        self.assertEqual(self.plan("manual", memo)[0]["want"], ["w1", "w2", "w3"])
        del self.SPACES

    def test_worktree_children_follow_their_parent(self):
        self.SPACES = [space("w1", "web", 1), space("w2", "api", 2, worktree={"repo_key": "r", "is_linked_worktree": False}),
                       space("w3", "api-wt", 3, worktree={"repo_key": "r", "is_linked_worktree": True}), space("w4", "aaa", 4)]
        want = self.plan("alpha")[0]["want"]
        self.assertEqual(want.index("w3"), want.index("w2") + 1)
        del self.SPACES

    def test_fight_guard_pauses_after_repeated_reapplies(self):
        moves = []
        covrd.call = lambda m, p=None: moves.append(p) or {"workspaces": []}
        saved_log_once, covrd.log_once = covrd.log_once, lambda *a: None
        try:
            memo = {}
            for _ in range(6):
                covrd.move_spaces(["w2", "w1"], memo)
            self.assertEqual(len(moves), covrd.FIGHT_MOVES - 1)
            covrd.move_spaces(["w1", "w2"], {})  # a different order (another memo) is never held back
            self.assertEqual(len(moves), covrd.FIGHT_MOVES)
        finally:
            covrd.log_once = saved_log_once


class LayoutFile(unittest.TestCase):
    def setUp(self):
        import subprocess
        self.cfg = os.path.join(_TMP, "herdr-config.toml")
        self.saved = (covrd.HERDR_CONFIG, covrd.notify, layout.subprocess.run)
        self.reload_rc = 0
        covrd.HERDR_CONFIG, covrd.notify = self.cfg, lambda body: None
        layout.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, self.reload_rc, "", "rejected")

    def tearDown(self):
        covrd.HERDR_CONFIG, covrd.notify, layout.subprocess.run = self.saved

    def write(self, text):
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self):
        with open(self.cfg, encoding="utf-8") as f:
            return f.read()

    ORIG = ('[theme]\nname = "catppuccin-latte"\n\n# >>> covr.sidebar keys\n[[keys.command]]\nkey = "prefix+a"\n'
            'type = "plugin_action"\ncommand = "covr.sidebar.cycle-view"\n# <<< covr.sidebar keys\n')

    def test_install_is_idempotent_and_uninstall_restores_bytes(self):
        self.write(self.ORIG)
        self.assertEqual(layout.install(), 0)
        once = self.read()
        self.assertIn("# >>> covr.sidebar layout", once)
        self.assertIn("#d20f39", once)  # latte for a latte theme
        self.assertEqual(layout.install(), 0)
        self.assertEqual(self.read(), once)
        self.assertEqual(layout.uninstall(), 0)
        self.assertEqual(self.read(), self.ORIG)

    def test_dark_theme_gets_mocha(self):
        self.write(self.ORIG.replace("catppuccin-latte", "tokyo-night"))
        layout.install()
        self.assertIn("#f38ba8", self.read())
        self.assertNotIn("[theme.custom]", self.read())

    def test_conflicting_table_is_refused_untouched(self):
        self.write(self.ORIG + "\n[ui.sidebar.agents]\nrow_gap = 1\n")
        before = self.read()
        self.assertEqual(layout.install(), 1)
        self.assertEqual(self.read(), before)

    def test_failed_reload_restores_the_file(self):
        self.write(self.ORIG)
        self.reload_rc = 1  # herdr rejects the new config
        self.assertEqual(layout.install(), 1)
        self.assertEqual(self.read(), self.ORIG)

    def test_block_found_with_crlf_and_without_final_newline(self):
        with open(os.path.join(layout.LAYOUTS, "mocha.toml"), encoding="utf-8") as f:
            blk = f.read()
        self.assertTrue(layout.BLOCK.search(("a = 1\n\n" + blk).replace("\n", "\r\n")))
        self.assertTrue(layout.BLOCK.search("a = 1\n\n" + blk.rstrip("\n")))
        self.assertFalse(layout.BLOCK.search(self.ORIG))  # the keys block is not ours
        old = blk.replace("# >>> covr.sidebar layout", "# >>> covr.sidebar (older name) — herdr sidebar layout", 1)
        self.assertTrue(layout.BLOCK.search("a = 1\n\n" + old))  # blocks from older versions are still found


class Contract(unittest.TestCase):
    ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    EVENTS = {"workspace.created", "workspace.updated", "workspace.closed", "workspace.renamed", "workspace.moved",
              "workspace.reordered", "workspace.focused", "worktree.created", "worktree.opened", "worktree.removed",
              "tab.created", "tab.closed", "tab.renamed", "tab.moved", "tab.focused", "pane.created", "pane.closed",
              "pane.focused", "pane.moved", "pane.exited", "pane.agent_detected", "pane.agent_status_changed"}

    def manifest(self):
        if covrd.tomllib is None:
            self.skipTest("needs tomllib (Python 3.11+)")
        with open(os.path.join(self.ROOT, "herdr-plugin.toml"), "rb") as f:
            return covrd.tomllib.load(f)

    def test_manifest(self):
        m = self.manifest()
        for k in ("id", "name", "version", "min_herdr_version"):
            self.assertIn(k, m)
        self.assertEqual(m["min_herdr_version"], "0.9.0")  # CI proves 0.9.0; raising it makes older servers refuse the plugin
        ids = [a["id"] for a in m["actions"]] + [p["id"] for p in m["panes"]]
        self.assertTrue(all("." not in i for i in ids))
        for e in m["events"]:
            self.assertIn(e["on"], self.EVENTS)
        for item in m["startup"] + m["events"] + m["actions"] + m["panes"]:
            script = item["command"][1]
            self.assertTrue(os.path.exists(os.path.join(self.ROOT, script)), script)

    def test_every_glyph_has_one_colour_rule_in_each_layout(self):
        for variant in ("latte.toml", "mocha.toml"):
            with open(os.path.join(self.ROOT, "layouts", variant), encoding="utf-8") as f:
                text = f.read()
            for g in covrd.GLYPH.values():
                self.assertEqual(text.count(f'starts_with = "{g}"'), 1, (variant, g))

    def test_layout_references_every_row_token_the_daemon_pushes(self):
        with open(os.path.join(self.ROOT, "layouts", "latte.toml"), encoding="utf-8") as f:
            text = f.read()
        for tok in ("head", "wait", "done", "task", "rule", "alert", "dirty", "spin"):
            self.assertIn(f'"${tok}"', text)

    def test_token_names_are_valid(self):
        import re
        for t in covrd.TOKENS + ["alert", "dirty", "spin"]:
            self.assertRegex(t, r"^[A-Za-z0-9_-]{1,32}$")
        self.assertLessEqual(len(covrd.TOKENS), 16)  # one report may mention at most 16 keys

    def test_settings_popup_matches_the_options(self):
        import settings
        for key, _, vals in settings.ITEMS:
            self.assertIn(key, covrd.DEFAULTS)
            for v in vals:
                self.assertIsNone(covrd.validate(key, v)[1], (key, v))


if __name__ == "__main__":
    unittest.main()
