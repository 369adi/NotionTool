"""v6 UI polish 2026-10-26: badge instead of a second dot, honest task and coworker counts, context over-limit
warning, neutral idle Stop, sidebar scroll fades, calm tables and a message column as wide as the composer."""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / 'notiontool' / 'web' / 'desktop' / 'v6.js').read_text(encoding='utf-8')
CSS = (ROOT / 'notiontool' / 'web' / 'desktop' / 'v6.css').read_text(encoding='utf-8')


class V6PolishUiTest(unittest.TestCase):
    def test_coworker_rows_show_a_badge_not_a_second_dot(self):
        self.assertIn('function avatar(label)', JS)
        self.assertEqual(JS.count('+ avatar(w.label) +'), 2)
        self.assertIn('.nt-v6-cw > .nt-v6-dot::after { content: none; }', CSS)
        self.assertIn('.nt-v6-av {', CSS)

    def test_coworkers_heading_counts_active_instead_of_always_saying_live(self):
        self.assertNotIn('Coworkers - live', JS)
        self.assertIn("cwBusy + ' active' : 'all idle'", JS)

    def test_only_tasks_a_coworker_is_on_count_as_running(self):
        self.assertIn('var RUN_DONE = {', JS)
        self.assertIn('if (hasRuns && t.run_id && !liveRuns[str(t.run_id)]) return;', JS)
        self.assertIn('!busy[str(t.worker_id)])) waiting++;', JS)
        self.assertIn('return { run: run, queued: queued, waiting: waiting };', JS)
        self.assertIn("'No tasks running'", JS)
        self.assertIn("' waiting'", JS)

    def test_context_over_the_limit_is_flagged_and_switch_is_hinted(self):
        self.assertIn('cOver = cf >= 1', JS)
        self.assertIn("(cOver ? ' over' : cWarn ? ' warn' : '')", JS)
        self.assertIn("(cOver && !ask ? ' hint' : '')", JS)
        self.assertIn('.nt-v6-si.over .nt-v6-ctx b', CSS)

    def test_idle_stop_is_neutral_not_red(self):
        self.assertIn('.nt-v6-stop.off > span { opacity: 1;', CSS)

    def test_sidebar_list_fades_where_more_is_scrolled_away(self):
        self.assertIn('function fadeScroll()', JS)
        self.assertIn("sb.addEventListener('scroll', fadeScroll, true);", JS)
        self.assertIn('fadeScroll();', JS)
        self.assertIn('.nt-v6-scroll.fb', CSS)

    def test_tables_and_message_column_width(self):
        self.assertIn('.notion-table-block th', CSS)
        self.assertIn('div[style*="max-width: 798px"][style*="padding-inline: 52px"]', CSS)

    def test_open_chat_is_never_duplicated_in_the_chats_list(self):
        # User report 2026-10-27: after clicking a chat (e.g. Roblox) it appeared BOTH in the main
        # card and as a row in the Chats list (a private chat of the open thread was merged back in).
        self.assertIn('var curKey = currentThread();', JS)
        self.assertIn('chats = chats.filter', JS)
        self.assertIn("if (curKey && c.thread_id && key(c.thread_id) === curKey) return false;", JS)
        self.assertIn("if (curTitle && c.title === curTitle) return false;", JS)


if __name__ == '__main__':
    unittest.main()
