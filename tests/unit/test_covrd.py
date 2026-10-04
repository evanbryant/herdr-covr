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
        self.screen, self.scroll = "Bash(rm -rf build/)\nDo you want to proceed?\n❯ 1. Yes", {}

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
            return {"read": {"text": self.screen}}
        if method == "pane.get":
            return {"pane": {"pane_id": params["pane_id"], "scroll": {"offset_from_bottom": self.scroll.get(params["pane_id"], 0)}}}
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
    o = dict(covrd.DEFAULTS, kind_icon="off")  # row-text tests read cleaner without the kind icon; icon tests turn it on
    o.update(kw)
    return o


class Base(unittest.TestCase):
    def setUp(self):
        self._call = covrd.call
        covrd.save_pins({"agents": [], "spaces": []})

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
        row = covrd.pinned_row("◗", "data-pipeline-service · billing", "39m", 32)
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


class WaitReason(unittest.TestCase):
    def reason(self, screen):
        old = covrd.call
        covrd.call = lambda m, p=None: {"read": {"text": screen}}
        try:
            return covrd.wait_reason("p1")
        finally:
            covrd.call = old

    def test_regression_wrapped_question_shows_its_start(self):
        screen = ("● Posted the digest.\n\n"
                  "● The Steam Visibility digest will post again on Monday around 15:00 UTC. Should I stop it now from\n"
                  "  our side?\n\n"
                  "❯ \n")
        self.assertEqual(self.reason(screen), "The Steam Visibility digest will post again on Monday…")

    def test_short_question_unchanged(self):
        self.assertEqual(self.reason("● Ship it?\n\n❯ \n"), "Ship it?")

    def test_approval_prefers_the_command_above(self):
        screen = ("╭──────────╮\n│ Bash command │\n│ rm -rf build │\n│\n│ Do you want to proceed? │\n"
                  "│ ❯ 1. Yes │\n│ 2. No │\n╰──────────╯\n")
        self.assertEqual(self.reason(screen), "rm -rf build")

    def test_question_form_shows_its_header(self):
        screen = ("● Summary of the cleanup.\n"
                  "─────────────────────────\n"
                  "←  ☐ Teardown  ☐ Rollback  ✔ Submit  →\n\n"
                  "│ The web staging stack still serves the docs preview, not just the old API path. How far\n"
                  "│ should the teardown go?\n\n"
                  "❯ 1. Delete the branches only (Recommended)\n"
                  "     Removes the last code that reads the token.\n"
                  "  2. Full teardown\n"
                  "  3. Type something.\n"
                  "─────────────────────────\n"
                  "  4. Chat about this\n\n"
                  "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n")
        self.assertEqual(self.reason(screen), "Teardown: The web staging stack still serves the docs…")

    FOOTER = "Enter to select · ↑/↓ to navigate · Esc to cancel\n"

    def test_single_question_header(self):
        screen = "─────\n ☐ Deploy\n\nShip it?\n\n❯ 1. Yes\n  2. No\n\n" + self.FOOTER
        self.assertEqual(self.reason(screen), "Deploy: Ship it?")

    def test_header_skips_answered_questions(self):
        screen = "←  ☒ Deploy  ☐ Rollback  ✔ Submit  →\n\nKeep the old build?\n\n❯ 1. Yes\n\n" + self.FOOTER
        self.assertEqual(self.reason(screen), "Rollback: Keep the old build?")

    def test_regression_todo_list_is_not_a_header(self):
        screen = ("● Update Todos\n  ⎿  ☒ Read the parser\n     ☐ Fix the header rule\n     ☐ Run the tests\n\n"
                  "● I found two ways to fix it. Should I keep the old behaviour for\n  single questions?\n\n❯ \n")
        self.assertEqual(self.reason(screen), "I found two ways to fix it. Should I keep the old…")

    def test_no_header_for_plain_questions(self):
        self.assertEqual(self.reason("● All done. ☐ is a box. Ship it?\n\n❯ \n"), "All done. ☐ is a box. Ship it?")

    def test_full_width_question_mark(self):
        self.assertEqual(self.reason("● どの形式で出力しますか？\n\n❯ \n"), "どの形式で出力しますか？")

    def test_approval_with_a_choice_suffix(self):
        self.assertEqual(self.reason("  npm test -- --coverage\n\n  Do you want to proceed? [y/N]\n"), "npm test -- --coverage")

    def test_nothing_found(self):
        self.assertEqual(self.reason("working...\n"), "waiting for you")


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
        self.assertTrue(out["p1"]["head"].startswith("◗ api"))

    def test_pinned_agent_leads_and_is_marked(self):
        covrd.save_pins({"agents": ["p1"], "spaces": []})
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
        self.assertTrue(out["p1"]["head"].startswith("◗ api"))

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

    def test_regression_show_kind_always_works_under_every_grouping(self):
        # with group_by = kind (and one kind running) "always" printed no kind at all
        for kinds in ("claude", "codex"):
            h = FakeHerdr([agent("p1", "w1", "idle"), agent("p2", "w2", "blocked", kind=kinds, seq=2),
                           agent("p3", "w2", "working", kind=kinds, seq=3)],
                          [space("w1", "api", 1), space("w2", "docs", 2)])
            for group in ("none", "project", "kind"):
                for label in ("space", "task"):
                    out, *_ = self.run_compute(h, opt=opts(group_by=group, label=label, show_kind="always"))
                    for pid, t in out.items():
                        self.assertIn("claude" if pid == "p1" else kinds, t["head"], (kinds, group, label))
                out, *_ = self.run_compute(h, opt=opts(group_by=group, show_kind="auto"))
                named = sum(1 for t in out.values() if "claude" in t["head"] or "codex" in t["head"])
                self.assertEqual(named, 0 if kinds == "claude" else 2 if group == "kind" else 3, (kinds, group))

    def test_kind_groups_label_first_row_and_close_with_a_rule(self):
        h = FakeHerdr([agent("p1", "w1", "idle", kind="claude"), agent("p2", "w2", "working", kind="codex", seq=2),
                       agent("p3", "w2", "idle", kind="codex", seq=3)],
                      [space("w1", "api", 1), space("w2", "web", 2)])
        out, *_ = self.run_compute(h, opt=opts(group_by="kind"))
        labelled = [p for p, t in out.items() if "codex" in t["head"] or "claude" in t["head"]]
        self.assertEqual(len(labelled), 2)  # once per group
        self.assertEqual(sum(1 for t in out.values() if "rule" in t), 1)

    def test_kind_icon_positions(self):
        h = FakeHerdr([agent("p1", "w1", "idle", kind="claude"), agent("p2", "w2", "blocked", seq=2, kind="gemini"),
                       agent("p3", "w3", "idle", seq=3, kind="mystery")],
                      [space("w1", "api", 1), space("w2", "web", 2), space("w3", "docs", 3)])
        self.assertEqual(covrd.DEFAULTS["kind_icon"], "right")
        out, *_ = self.run_compute(h, opt=opts(kind_icon="inline"))   # [status] [kind] [label], state colour
        self.assertEqual(self.text(out["p1"]["head"]), "○ ✻ api")
        self.assertEqual(self.text(out["p2"]["head"]), "× ✦ web")
        self.assertEqual(self.text(out["p3"]["head"]), f"○ {covrd.ICON_OTHER} docs")
        self.assertFalse(any("icon" in t or "icon_r" in t for t in out.values()))
        for where, key in (("left", "icon"), ("right", "icon_r")):           # own token, brand colour, 4 cells
            out, *_ = self.run_compute(h, opt=opts(kind_icon=where))
            self.assertEqual(out["p2"][key], "✦")              # blocked: brand colour
            self.assertEqual(out["p1"][key], "✻")   # idle: brand colour too
            self.assertEqual(self.text(out["p1"]["head"]), "○ api")
            self.assertTrue(all(covrd.cells(t["head"]) <= covrd.sidebar_width() - 7 for t in out.values()))
            self.assertNotIn("icon_r" if key == "icon" else "icon", out["p1"])
        out, *_ = self.run_compute(h, opt=opts(kind_icon="off"))
        self.assertEqual(self.text(out["p1"]["head"]), "○ api")
        self.assertFalse(any("icon" in t or "icon_r" in t for t in out.values()))

    def test_icon_token_dims_when_asleep(self):
        h = FakeHerdr([agent("p1", "w1", "idle", kind="claude")], [space("w1", "web", 1)])
        out, *_ = self.run_compute(h, opt=opts(kind_icon="right"),
                                   memo={"since": {"p1": {"seq": 1, "since": time.time() - 7200, "st": "idle"}}})
        self.assertEqual(out["p1"]["icon_r"], covrd.ZW * 2 + "✻")   # asleep: dimmed with the row

    def test_show_icon_false_from_0_5_0_still_turns_icons_off(self):
        with open(os.path.join(covrd.CFG_DIR, "config.toml"), "w") as f:
            f.write("show_icon = false\n")
        try:
            self.assertEqual(covrd.options()["kind_icon"], "off")
        finally:
            os.remove(os.path.join(covrd.CFG_DIR, "config.toml"))

    def test_icons_never_reuse_a_state_glyph(self):
        marks = set(covrd.ICON.values()) | {covrd.ICON_OTHER}
        self.assertFalse(marks & set(covrd.GLYPH.values()))
        self.assertTrue(all(covrd.cells(m) == 1 for m in marks))

    def test_header_counts_every_agent_whatever_the_view_shows(self):
        h = FakeHerdr([agent("p1", "w1", "idle"), agent("p2", "w1", "blocked", seq=2)], [space("w1", "web", 1)])
        *_, view, _ = self.run_compute(h, opt=opts(view="needs me"))
        self.assertTrue(view["label"].startswith("[2]") and view["label"].endswith("needs me"))

    def test_count_sits_beside_the_header_word(self):
        # herdr right-aligns the label: blanks push the count left, to just after "agents"
        label = covrd.view_params(opts(), "none", 12, 34)["label"]
        self.assertEqual(label.replace(BLANK, "_"), "[12]" + "_" * (34 - 9 - 4 - 6) + "triage")
        # regression: a label longer than its row is drawn over the word "agents"; the view name gives way, not the count
        for width in range(10, 60):
            for o, mode in ((opts(), "none"), (opts(view="needs me", group_by="project"), "project")):
                label = covrd.view_params(o, mode, 128, width)["label"]
                self.assertTrue(label.startswith("[128]"))
                self.assertLessEqual(covrd.cells(label), max(width - 9, 5), (width, label))
        self.assertEqual(covrd.view_params(opts(view="needs me", group_by="project"), "project", 22, 26)["label"],
                         "[22] · by projec…")

    def test_look_alike_rows_get_task_tags(self):
        h = FakeHerdr([agent("p1", "w1", "idle", title="Docs refresh"), agent("p2", "w1", "idle", seq=2, title="Login bug")],
                      [space("w1", "web", 1)])
        out, *_ = self.run_compute(h)
        self.assertEqual(self.text(out["p1"]["head"]), "○ web · Docs refresh")
        self.assertEqual(self.text(out["p2"]["head"]), "○ web · Login bug")

    def test_look_alikes_in_one_named_tab_get_tags_under_project_grouping(self):
        h = FakeHerdr([agent("p1", "w1", "idle", title="Docs refresh"), agent("p2", "w1", "idle", seq=2, title="Login bug"),
                       agent("p3", "w1", "idle", seq=3, title="Cache rewrite")],
                      [space("w1", "web", 1)], [{"tab_id": "w1:t1", "label": "review"}])
        out, *_ = self.run_compute(h, opt=opts(group_by="project"))  # 26 cells: no room for the icon too
        self.assertEqual(len({t["head"] for t in out.values()}), 3)
        self.assertTrue(all("rev" in t["head"] for t in out.values()))
        out, *_ = self.run_compute(h, opt=opts(group_by="project", disambiguate=False))
        self.assertEqual(len({t["head"] for t in out.values()}), 2)

    def test_regression_stale_and_idle_are_not_look_alikes(self):
        h, memo = FakeHerdr([agent("p1", "w1", "idle", title="Docs refresh"), agent("p2", "w1", "idle", seq=2, title="Login bug")],
                            [space("w1", "web", 1)]), {}
        self.run_compute(h, memo=memo)
        memo["since"]["p1"]["since"] = time.time() - 7200
        out, *_ = self.run_compute(h, opt=opts(stale_after="1h"), memo=memo)
        self.assertEqual(self.text(out["p1"]["head"]), "◗ web")
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
        self.assertTrue(view["label"].startswith("[3]") and view["label"].endswith("by project"))


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

    def test_tokens_are_taken_back_when_a_pane_stops_being_an_agent(self):
        sent, saved = [], (covrd.report, covrd.call)
        try:
            covrd.report = lambda kind, target, set_=None, clear=(): sent.append((target, dict(set_ or {}), list(clear)))
            covrd.call = lambda method, params=None: {"agents": []} if method == "agent.list" else {}
            memo = {"pushed": {"p1": {"head": "× api", "wait": "↳ Allow?"}, "p2": {"head": "○ web"}}}
            covrd.apply({"p2": {"head": "○ web"}}, {}, {"x": 1}, memo)   # p1's agent exited
            self.assertEqual(sent, [("p1", {}, ["head", "wait"])])
            self.assertEqual(set(memo["pushed"]), {"p2"})
        finally:
            covrd.report, covrd.call = saved

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

    def test_lost_tokens_needs_all_of_a_panes_tokens_gone_or_its_head(self):
        memo = {"pushed": {"p1": {"head": "x", "rank": "1", "wait": "?"}}, "live": {"p1": {"head", "rank"}}}
        self.assertFalse(covrd.lost_tokens(memo))  # one value dropped (e.g. sanitised): not a restart
        memo["live"] = {"p1": set()}
        self.assertTrue(covrd.lost_tokens(memo))
        memo["live"] = {"p1": {"icon_r"}}          # the row went but herdr kept the icon: still lost
        memo["pushed"]["p1"]["icon_r"] = "✻"
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


class Hardening(Base):
    """0.6.0: one test per finding of the 0.5.2 review (reports/bugcheck-0.5.2-2026-10-03)."""

    def test_git_dirty_never_runs_a_repos_filter_drivers(self):  # F1
        import shutil, subprocess
        if not shutil.which("git"):
            self.skipTest("no git")
        repo = tempfile.mkdtemp(prefix="covr-git-")
        flag = os.path.join(repo, "..", os.path.basename(repo) + ".ran")
        git = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, check=True)
        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        with open(os.path.join(repo, ".gitattributes"), "w") as f:
            f.write("a.txt filter=evil\n")
        for n in ("a.txt", "b.txt"):
            with open(os.path.join(repo, n), "w") as f:
                f.write("one\n")
        git("add", ".")
        git("commit", "-q", "-m", "x")
        git("config", "filter.evil.clean", f"sh -c 'echo ran > \"{flag}\"; cat'")
        git("config", "filter.evil.process", f"sh -c 'echo ran > \"{flag}\"'")
        os.utime(os.path.join(repo, "a.txt"), (1, 1))  # stat-dirty: plain git status would run the filter
        self.assertIn(covrd.git_dirty(repo), (False, None))
        self.assertFalse(os.path.exists(flag))
        with open(os.path.join(repo, "b.txt"), "a") as f:   # (a.txt itself uses the driver: left out of the check)
            f.write("two\n")
        self.assertTrue(covrd.git_dirty(repo))
        self.assertFalse(os.path.exists(flag))

    def test_a_failed_tick_leaves_no_old_wake_time(self):  # F2
        memo = {"wake_at": 1.0}

        def down(method, params=None):
            raise covrd.ServerGone("gone")
        covrd.call = down
        with self.assertRaises(covrd.ServerGone):
            covrd.compute(opts(), memo)
        self.assertIsNone(memo["wake_at"])

    def test_server_gone_is_only_the_socket(self):  # F13
        self.assertTrue(covrd.server_unreachable(covrd.ServerGone("x")))
        self.assertFalse(covrd.server_unreachable(FileNotFoundError(2, "herdr binary missing")))

    def test_pins_belong_to_the_session_and_are_pruned_after_a_grace(self):  # F5, F11
        self.assertTrue(covrd.PINS.startswith(covrd.RUN_DIR))
        covrd.save_pins({"agents": ["p1", "gone"], "spaces": ["w1", "wgone"]})
        memo = {}
        p = covrd.prune_pins(covrd.load_pins(), {"p1"}, {"w1"}, memo, 1000)
        self.assertEqual(p, {"agents": ["p1", "gone"], "spaces": ["w1", "wgone"]})   # a restore may be running
        p = covrd.prune_pins(covrd.load_pins(), {"p1"}, {"w1"}, memo, 1000 + covrd.PIN_GRACE)
        self.assertEqual(p, {"agents": ["p1"], "spaces": ["w1"]})
        self.assertEqual(covrd.load_pins(), {"agents": ["p1"], "spaces": ["w1"]})

    def test_legacy_shared_pins_move_into_the_default_session_only(self):  # F5, R5
        os.remove(covrd.PINS)
        with open(covrd.LEGACY_PINS, "w", encoding="utf-8") as f:
            json.dump({"agents": ["p9"], "spaces": []}, f)
        self.assertEqual(covrd.load_pins()["agents"], [])        # the tests run as a named session
        saved = covrd.DEFAULT_SOCK
        covrd.DEFAULT_SOCK = covrd.SOCK
        try:
            self.assertEqual(covrd.load_pins()["agents"], ["p9"])
        finally:
            covrd.DEFAULT_SOCK = saved
        self.assertFalse(os.path.exists(covrd.LEGACY_PINS))

    def test_pinning_a_shell_pane_is_refused(self):  # F11
        covrd.call = FakeHerdr([agent("p1", "w1", "idle")], [space("w1", "web", 1)])
        os.environ["HERDR_PANE_ID"] = "p7"
        try:
            self.assertEqual(covrd.toggle_pin("agents"), ("p7", None))
            os.environ["HERDR_PANE_ID"] = "p1"
            self.assertEqual(covrd.toggle_pin("agents"), ("p1", True))
        finally:
            os.environ.pop("HERDR_PANE_ID")
        with self.assertRaises(ValueError):
            covrd.toggle_pin("nonsense")

    def test_first_push_clears_tokens_an_earlier_daemon_left(self):  # F6
        sent, saved = [], covrd.report
        covrd.report = lambda kind, target, set_=None, clear=(): sent.append((kind, target, dict(set_ or {}), sorted(clear)))
        try:
            memo = {"live": {"a": {"head", "pin", "wait", "rank"}}, "space_ids": ["w1"]}
            covrd.call = lambda *a, **k: {}
            covrd.apply({"a": {"head": "○ api", "rank": "1"}}, {}, {"label": "x"}, memo)
            self.assertIn(("pane", "a", {"head": "○ api", "rank": "1"}, ["pin", "wait"]), sent)
            self.assertIn(("workspace", "w1", {}, ["alert", "dirty", "spin"]), sent)
            sent.clear()
            covrd.apply({"a": {"head": "○ api", "rank": "1"}}, {}, {"label": "x"}, memo)
            self.assertEqual(sent, [])  # once only
        finally:
            covrd.report = saved

    def test_a_state_change_seen_after_a_gap_has_no_age(self):  # F21
        clock, saved = FakeClock(), covrd.time
        covrd.time = clock
        try:
            h = FakeHerdr([agent("p1", "w1", "working", seq=1)], [space("w1", "web", 1)])
            _, _, _, memo = self.run_compute(h)
            clock.now += 5
            h.agents[0].update(agent_status="idle", state_change_seq=2)
            out, *_ = self.run_compute(h, memo=memo)
            self.assertTrue(out["p1"]["head"].endswith("<1m"))       # seen within a tick: it just happened
            clock.now += 7200                                         # the daemon was stopped for 2 h
            h.agents[0].update(agent_status="working", state_change_seq=3)
            out, *_ = self.run_compute(h, memo=memo)
            self.assertEqual(self.text(out["p1"]["head"]), "◐ web")
            self.assertFalse(out["p1"]["head"].endswith("<1m"))
        finally:
            covrd.time = saved

    def test_transcripts_only_for_claude_and_a_few_per_tick(self):  # F14
        looked = []
        saved = covrd.transcript_mtime
        covrd.transcript_mtime = lambda *a: looked.append(a) or 1_000_000.0
        try:
            agents = [agent(f"p{i}", "w1", "idle", title=f"t{i}") for i in range(5)] + \
                     [agent("px", "w1", "idle", kind="codex", title="tx")]
            h = FakeHerdr(agents, [space("w1", "web", 1)])
            _, _, _, memo = self.run_compute(h, opt=opts(age_source="claude-transcripts"))
            self.assertEqual(len(looked), covrd.LOOKUPS_PER_TICK)
            self.run_compute(h, opt=opts(age_source="claude-transcripts"), memo=memo)
            self.assertEqual(len(looked), 5)                           # the rest next tick; never the codex agent
        finally:
            covrd.transcript_mtime = saved

    def test_kind_group_rule_sits_on_the_last_shown_row(self):  # F25
        h = FakeHerdr([agent("p1", "w1", "blocked"), agent("p2", "w1", "idle"),
                       agent("p3", "w2", "done", kind="codex")], [space("w1", "web", 1), space("w2", "api", 2)])
        out, *_ = self.run_compute(h, opt=opts(group_by="kind", view="needs me"))
        self.assertIn("rule", out["p1"])         # p2 (idle) is hidden by the view
        self.assertNotIn("rule", out["p2"])

    def test_pin_star_survives_truncation(self):  # F26
        row = covrd.pinned_row("○", "averyveryverylongspacename-for-tests", "5m", 26, " ★")
        self.assertIn("… ★", row)
        self.assertEqual(covrd.cells(row), 23)

    def test_truncation_prefers_a_word_boundary(self):  # F31
        self.assertEqual(covrd.cut("alpha beta gammadelta", 15), "alpha beta…")
        self.assertEqual(covrd.cut("alpha betagammadelta", 15), "alpha betagamm…")  # no space late enough
        self.assertEqual(covrd.cut("alphabetagammadelta", 10), "alphabeta…")

    def test_pinned_worktree_space_moves_its_repo(self):  # F27
        spaces = {"w1": space("w1", "web", 1), "w2": space("w2", "api", 2, worktree={"repo_key": "r"}),
                  "w3": space("w3", "api-fix", 3, worktree={"repo_key": "r", "is_linked_worktree": True})}
        plan = covrd.plan_spaces(opts(), spaces, [], {"agents": [], "spaces": ["w3"]}, {}, 0)
        self.assertEqual(plan["want"], ["w2", "w3", "w1"])

    def test_zero_width_characters_take_no_cells(self):  # F30
        self.assertEqual(covrd.cells("a​b"), 2)
        self.assertEqual(covrd.cells("\u2764\ufe0f"), 2)              # emoji presentation: 2 cells, as herdr draws it
        self.assertEqual(covrd.cells("\u2764"), 1)
        self.assertEqual(covrd.cells("\U0001F468‍\U0001F4BB"), 2)

    def test_group_rule_follows_the_sidebar_width(self):  # F32
        saved = covrd.sidebar_width
        covrd.sidebar_width = lambda: 40
        try:
            h = FakeHerdr([agent("p1", "w1", "idle"), agent("p2", "w1", "idle", kind="codex")], [space("w1", "web", 1)])
            out, *_ = self.run_compute(h, opt=opts(group_by="kind"))
            rules = [t["rule"] for t in out.values() if "rule" in t]
            self.assertEqual(rules, ["─" * 37])
        finally:
            covrd.sidebar_width = saved


class OptionFile(unittest.TestCase):
    def setUp(self):
        self.path = covrd.OPTIONS

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_a_change_keeps_comments_and_other_lines(self):  # F3
        self.write('# mine\ngroup_by = "project"  # keep\nstale_after = "2h"\n')
        covrd.write_option("view", "cycle")
        self.assertEqual(self.read(), '# mine\ngroup_by = "project"  # keep\nstale_after = "2h"\nview = "needs me"\n')
        covrd.write_option("group_by", "kind")
        self.assertIn('group_by = "kind"  # keep\n', self.read())
        self.assertIn("# mine", self.read())

    @unittest.skipIf(covrd.tomllib is None, "needs tomllib to tell a broken file")
    def test_a_broken_file_is_never_overwritten(self):  # F3
        broken = 'group_by = "project"\nview = "triage\n'
        self.write(broken)
        with self.assertRaises(ValueError):
            covrd.write_option("view", "cycle")
        self.assertEqual(self.read(), broken)

    def test_byte_order_mark_is_accepted(self):  # F3
        self.write('﻿group_by = "project"\n')
        self.assertEqual(covrd.options()["group_by"], "project")

    def test_cycle_flips_booleans_and_refuses_value_options(self):  # F19
        self.assertIs(covrd.write_option("disambiguate", "cycle"), False)
        for key in ("stale_after", "seen_after", "tick_seconds"):
            with self.assertRaises(ValueError):
                covrd.write_option(key, "cycle")
        with self.assertRaises(ValueError):
            covrd.write_option("nope", "cycle")

    def test_kind_icon_cycles_in_the_popups_order(self):  # F19
        self.assertEqual(covrd.CYCLES["kind_icon"][0], covrd.DEFAULTS["kind_icon"])
        self.assertEqual(covrd.write_option("kind_icon", "cycle"), "left")

    def test_infinite_or_foreign_numbers_are_refused(self):  # F20, F29
        for v in (float("inf"), "inf", "nan", "٥", 7.5, True):
            self.assertIsNotNone(covrd.validate("tick_seconds", v)[1], v)
        self.assertIsNotNone(covrd.validate("stale_after", "٢h")[1])
        self.write("tick_seconds = inf\n")
        self.assertEqual(covrd.options()["tick_seconds"], covrd.DEFAULTS["tick_seconds"])

    def test_write_follows_a_symlink(self):  # F7
        if covrd.WIN:
            self.skipTest("symlinks need privileges on Windows")
        target = os.path.join(_TMP, "dotfiles-config.toml")
        link = os.path.join(_TMP, "linked-config.toml")
        with open(target, "w") as f:
            f.write("a = 1\n")
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(target, link)
        covrd.write_atomic(link, "a = 2\n")
        self.assertTrue(os.path.islink(link))
        with open(target) as f:
            self.assertEqual(f.read(), "a = 2\n")


class StopStays(unittest.TestCase):
    def test_stop_marker_keeps_hooks_from_restarting_the_daemon(self):  # F4
        spawned, saved = [], (covrd.spawn, covrd.signal_daemon, sys.argv)
        covrd.spawn, covrd.signal_daemon = lambda: spawned.append(1), lambda kind="wake": None
        try:
            covrd.set_stopped(True)
            sys.argv = ["covrd.py", "poke"]
            covrd.main()
            self.assertEqual(spawned, [])
            covrd.set_stopped(False)
            covrd.main()
            self.assertEqual(spawned, [1])
        finally:
            covrd.spawn, covrd.signal_daemon, sys.argv = saved
            covrd.set_stopped(False)

    def test_poke_fast_path_respects_the_marker(self):  # F4
        import subprocess
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "poke.py")
        covrd.set_stopped(True)
        try:
            r = subprocess.run([sys.executable, here], capture_output=True, timeout=10, env=os.environ.copy())
            self.assertEqual(r.returncode, 0)
            self.assertFalse(covrd.lock_held())
        finally:
            covrd.set_stopped(False)

    @unittest.skipIf(os.name == "nt", "Unix wakeup pipe")
    def test_wait_returns_on_a_signal_not_at_the_tick(self):  # F23
        import signal as sig
        woke = {"flag": False, "stop": False}
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.set_blocking(w, False)
        old = sig.signal(sig.SIGUSR1, lambda *_: woke.update(flag=True))
        prev = sig.set_wakeup_fd(w)
        try:
            sig.setitimer(sig.ITIMER_REAL, 0)
            t0 = time.time()
            import threading
            threading.Timer(0.2, lambda: os.kill(os.getpid(), sig.SIGUSR1)).start()
            covrd.wait(woke, {}, r, t0 + 10, 0.0)
            self.assertLess(time.time() - t0, 2)
            self.assertTrue(woke["flag"])
        finally:
            sig.set_wakeup_fd(prev)
            sig.signal(sig.SIGUSR1, old)


class LayoutDetails(unittest.TestCase):
    def test_light_themes_by_word(self):  # F24
        saved = covrd.options
        covrd.options = lambda: dict(covrd.DEFAULTS)
        try:
            for name, want in (("kanagawa-lotus", "light"), ("rose-pine-dawn", "light"), ("tokyo-night-day", "light"),
                               ("twilight", "dark"), ("catppuccin", "dark"), ("my_light_theme", "light")):
                self.assertEqual(layout.variant(f'[theme]\nname = "{name}"\n'), want, name)
        finally:
            covrd.options = saved


class ReviewFixes(Base):
    """0.6.0 review of the fixes (R1..R20)."""

    def test_a_closed_pane_does_not_turn_socket_reports_off(self):  # R1, R8, R16
        sent, saved = [], (covrd.cli, dict(covrd.REPORT))
        covrd.cli = lambda *a: sent.append(a)

        def herdr(method, params=None):
            if params.get("pane_id") == "gone":
                raise covrd.HerdrError(method, {"code": "pane_not_found", "message": "pane gone not found"})
            if method == "workspace.report_metadata":
                raise covrd.HerdrError(method, {"code": "invalid_request", "message": "unknown variant `workspace.report_metadata`"})
            return {}
        covrd.call = herdr
        try:
            covrd.report("pane", "gone", {}, ["head"])
            self.assertTrue(covrd.REPORT["socket"])
            covrd.report("pane", "live", {"head": "x"})
            self.assertEqual(sent, [])
            covrd.report("workspace", "w1", {"dirty": "±"})    # a server without the method: CLI from now on
            self.assertFalse(covrd.REPORT["socket"])
            self.assertEqual(sent[0][:2], ("workspace", "report-metadata"))
        finally:
            covrd.cli = saved[0]
            covrd.REPORT.update(saved[1])

    def git_repo(self, attrs, drivers):
        """A repo whose drivers are active when it is committed (as with git-lfs), then made stat-dirty."""
        import shutil, subprocess
        if not shutil.which("git"):
            self.skipTest("no git")
        repo = tempfile.mkdtemp(prefix="covr-git-")
        flag = repo + ".ran"
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        git = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, check=True, env=env)
        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        for name in drivers:
            git("config", f"filter.{name}.clean", f"sh -c 'echo ran >> \"{flag}\"; tr a-z A-Z'")
        os.makedirs(os.path.join(repo, "sub"))
        with open(os.path.join(repo, ".gitattributes"), "w") as f:
            f.write(attrs)
        for n in ("g", "f", os.path.join("sub", "s")):
            with open(os.path.join(repo, n), "w") as f:
                f.write("abc\n")
        git("add", ".")
        git("commit", "-q", "-m", "x")
        subprocess.run(["git", "-C", repo, "status"], capture_output=True, env=env)  # refresh the index
        if os.path.exists(flag):
            os.remove(flag)
        os.utime(os.path.join(repo, "g"), (1, 1))
        return repo, flag

    def test_a_driver_named_with_equals_is_never_run(self):  # R2
        repo, flag = self.git_repo("g filter=a=b\n", ["a=b"])
        self.assertIsNone(covrd.git_dirty(repo))
        self.assertFalse(os.path.exists(flag))

    def test_filtered_files_do_not_show_as_changed(self):  # R13
        repo, flag = self.git_repo("g filter=up\n", ["up"])
        self.assertIs(covrd.git_dirty(repo), False)          # g is stat-dirty and its filter is off: still clean
        self.assertIs(covrd.git_dirty(os.path.join(repo, "sub")), False)
        self.assertFalse(os.path.exists(flag))
        with open(os.path.join(repo, "f"), "a") as f:
            f.write("more\n")
        self.assertIs(covrd.git_dirty(repo), True)
        self.assertIs(covrd.git_dirty(os.path.join(repo, "sub")), True)   # a change outside the pane's subdirectory

    def test_odd_driver_names_still_get_a_dirty_check(self):  # final confirm: '.', ',', ':' in a driver name
        repo, flag = self.git_repo("g filter=a.b\n", ["a.b"])
        self.assertIsNotNone(covrd.git_dirty(repo))
        with open(os.path.join(repo, "f"), "a") as f:
            f.write("more\n")
        self.assertIs(covrd.git_dirty(os.path.join(repo, "sub")), True)
        self.assertFalse(os.path.exists(flag))

    def test_long_ticks_keep_the_age_of_live_changes(self):  # R3
        clock, saved = FakeClock(), covrd.time
        covrd.time = clock
        try:
            h = FakeHerdr([agent("p1", "w1", "working", seq=1)], [space("w1", "web", 1)])
            _, _, _, memo = self.run_compute(h, opt=opts(tick_seconds=45))
            clock.now += 40
            h.agents[0].update(agent_status="idle", state_change_seq=2)
            out, *_ = self.run_compute(h, opt=opts(tick_seconds=45), memo=memo)
            self.assertTrue(out["p1"]["head"].endswith("<1m"))
        finally:
            covrd.time = saved


class OptionLines(unittest.TestCase):
    def tearDown(self):
        if os.path.exists(covrd.OPTIONS):
            os.remove(covrd.OPTIONS)

    def roundtrip(self, before, key, value):
        with open(covrd.OPTIONS, "w", encoding="utf-8", newline="") as f:
            f.write(before)
        covrd.write_option(key, value)
        with open(covrd.OPTIONS, encoding="utf-8", newline="") as f:
            return f.read()

    def test_key_only_inside_a_table_gets_a_top_level_line(self):  # R10
        after = self.roundtrip('[extra]\nview = "here+"\n', "view", "needs me")
        self.assertEqual(after, 'view = "needs me"\n[extra]\nview = "here+"\n')

    def test_literal_quoted_key_is_rewritten_in_place(self):  # R11
        self.assertEqual(self.roundtrip("'view' = \"triage\"\n", "view", "here+"), 'view = "here+"\n')

    def test_crlf_and_trailing_comments_stay(self):  # R12
        after = self.roundtrip('# mine\r\nview = "triage"  # keep me\r\nlabel = "task"\r\n', "view", "needs me")
        self.assertEqual(after, '# mine\r\nview = "needs me"  # keep me\r\nlabel = "task"\r\n')


class LayoutNoFinalNewline(unittest.TestCase):
    def test_uninstall_restores_a_file_without_a_final_newline(self):  # R4
        import subprocess
        cfg = os.path.join(_TMP, "herdr-config-noeol.toml")
        saved = (covrd.HERDR_CONFIG, covrd.notify, layout.subprocess.run)
        covrd.HERDR_CONFIG, covrd.notify = cfg, lambda body: None
        layout.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
        try:
            for orig in ("onboarding = false\n[ui]\nsidebar_width = 30", "onboarding = false\n", "a = 1\r\nb = 2"):
                with open(cfg, "w", encoding="utf-8", newline="") as f:
                    f.write(orig)
                self.assertEqual(layout.install(), 0)
                self.assertEqual(layout.install(), 0)            # idempotent, marker kept
                self.assertEqual(layout.uninstall(), 0)
                with open(cfg, encoding="utf-8", newline="") as f:
                    self.assertEqual(f.read(), orig, repr(orig))
        finally:
            covrd.HERDR_CONFIG, covrd.notify, layout.subprocess.run = saved

class ScrollHold(Base):
    """A blocked row is a snapshot: scrolling the pane up neither turns it idle nor changes its reason."""

    def test_scrolling_up_holds_the_row_and_its_reason(self):
        clock, saved = FakeClock(), covrd.time
        covrd.time = clock
        try:
            h = FakeHerdr([agent("p1", "w1", "working", seq=1)], [space("w1", "web", 1)])
            _, _, _, memo = self.run_compute(h)
            clock.now += 10
            h.agents[0].update(agent_status="blocked", state_change_seq=2)
            out, *_ = self.run_compute(h, memo=memo)
            self.assertEqual(out["p1"]["wait"], "↳ Bash(rm -rf build/)")
            reads = sum(1 for m, _ in h.calls if m == "pane.read")
            # scrolled up: herdr loses the prompt and says idle; the row stays blocked, same reason, same age
            clock.now += 70
            h.scroll["p1"], h.screen = 30, "older output\n"
            h.agents[0].update(agent_status="idle", state_change_seq=3)
            out, *_ = self.run_compute(h, memo=memo)
            self.assertTrue(self.text(out["p1"]["head"]).startswith("×"))
            self.assertTrue(out["p1"]["head"].endswith("1m"))
            self.assertEqual(out["p1"]["wait"], "↳ Bash(rm -rf build/)")
            self.assertEqual(sum(1 for m, _ in h.calls if m == "pane.read"), reads)   # the snapshot, not a re-read
            # back at the bottom: blocked again under a new seq, still the same prompt and age
            h.scroll["p1"], h.screen = 0, "Bash(rm -rf build/)\nDo you want to proceed?\n❯ 1. Yes"
            h.agents[0].update(agent_status="blocked", state_change_seq=4)
            out, *_ = self.run_compute(h, memo=memo)
            self.assertTrue(out["p1"]["head"].endswith("1m"))
            self.assertEqual(sum(1 for m, _ in h.calls if m == "pane.read"), reads)
        finally:
            covrd.time = saved

    def test_answered_at_the_bottom_is_not_held(self):
        h = FakeHerdr([agent("p1", "w1", "blocked", seq=1)], [space("w1", "web", 1)])
        _, _, _, memo = self.run_compute(h)
        h.agents[0].update(agent_status="idle", state_change_seq=2)
        out, *_ = self.run_compute(h, memo=memo)
        self.assertTrue(self.text(out["p1"]["head"]).startswith("○"))
        self.assertNotIn("wait", out["p1"])
        self.assertEqual(memo["waits"], {})

    def test_working_while_scrolled_is_not_held(self):
        h = FakeHerdr([agent("p1", "w1", "blocked", seq=1)], [space("w1", "web", 1)])
        _, _, _, memo = self.run_compute(h)
        h.scroll["p1"] = 12
        h.agents[0].update(agent_status="working", state_change_seq=2)   # answered elsewhere: really moving
        out, *_ = self.run_compute(h, memo=memo)
        self.assertTrue(self.text(out["p1"]["head"]).startswith("◐"))

    def test_a_new_prompt_is_read_again(self):
        h = FakeHerdr([agent("p1", "w1", "blocked", seq=1)], [space("w1", "web", 1)])
        _, _, _, memo = self.run_compute(h)
        h.screen = "Which database should we use?\n❯ 1. Postgres"
        h.agents[0].update(state_change_seq=3)
        out, *_ = self.run_compute(h, memo=memo)
        self.assertEqual(out["p1"]["wait"], "↳ Which database should we use?")

    def test_the_reason_reads_the_bottom_of_the_pane(self):
        h = FakeHerdr([agent("p1", "w1", "blocked", seq=1)], [space("w1", "web", 1)])
        self.run_compute(h)
        self.assertEqual({p["source"] for m, p in h.calls if m == "pane.read"}, {"recent"})


if __name__ == "__main__":
    unittest.main()
