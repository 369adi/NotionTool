"""Static checks for the operator attention UI in notiontool/web (app.js, index.html).

No server is started and nothing is sent anywhere: the web files are read as text and the
routes they call are compared with the routes handled in notiontool/server.py.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'notiontool' / 'web'
APP = (WEB / 'app.js').read_text(encoding='utf-8')
HTML = (WEB / 'index.html').read_text(encoding='utf-8')
SERVER = (ROOT / 'notiontool' / 'server.py').read_text(encoding='utf-8')
ENGINE = (ROOT / 'notiontool' / 'engine.py').read_text(encoding='utf-8')
ROUTES = set(re.findall(r"path\s*==\s*'(/api/[^']+)'", SERVER))
NEW_ROUTES = ('run/confirm', 'task/reconcile', 'chat/compress', 'settings')
SETTINGS = ('coworker_rotate_mode', 'main_context_policy', 'coworker_delete_old_chat')
GERMAN = re.compile(r'\b(und|nicht|oder|Best\u00e4tigen|Abbrechen|Aufgabe|Aufgaben|Minuten|Einstellungen|'
                    r'Kontext|\u00dcbernehmen|Verwerfen|zuletzt|Ausgabe|Werkzeug|neuer|alten)\b|'
                    r'[\u00e4\u00f6\u00fc\u00c4\u00d6\u00dc\u00df]')


def block(text, start, end='\n}\n'):
    """Return text from start up to the next end marker (one function or one markup segment)."""
    i = text.index(start)
    j = text.find(end, i)
    return text[i:j if j >= 0 else len(text)]


class UiAttentionTests(unittest.TestCase):
    def test_server_routes_were_parsed(self):
        self.assertGreater(len(ROUTES), 20)

    def test_new_routes_are_called_and_exist_in_server(self):
        for route in NEW_ROUTES:
            self.assertIn("action('%s'" % route, APP, route)
            self.assertIn('/api/' + route, ROUTES, route)

    def test_every_action_route_exists_in_server(self):
        for route in set(re.findall(r"\b(?:action|api)\('([a-z/-]+)'", APP)):
            self.assertIn('/api/' + route, ROUTES, route)

    def test_create_chat_control_is_gone(self):
        for text in (APP, HTML):
            self.assertNotIn('create-chat', text)
            self.assertNotIn('own-chat', text)
            self.assertNotIn('Create own chat', text)
        self.assertNotIn('createOwnChat', APP)
        self.assertNotIn('/api/account/create-chat', ROUTES)
        self.assertIn('Create Welcome worker chats', APP)
        self.assertIn("action('account/provision'", APP)
        self.assertIn('/api/account/provision', ROUTES)
        self.assertIn('id="add-coworker-chat"', HTML)

    def test_attention_banner(self):
        self.assertIn('id="attention-banner"', HTML)
        self.assertRegex(HTML, r'id="attention-banner" class="[^"]*\bhidden\b')
        body = block(APP, 'function renderAttention(){')
        for needle in ("r.state==='awaiting_main'", 'mainReasons(r)', 'data-run-confirm', 'state.stalled',
                       'stall_age_s', "classList.toggle('hidden',!lines.length)"):
            self.assertIn(needle, body)
        self.assertIn('needs_main_reasons', APP)
        self.assertIn('renderAttention();', APP)
        self.assertIn("action('run/confirm',{id:d.runConfirm}", APP)
        self.assertIn("'/api/run/confirm'", SERVER)

    def test_uncertain_accept_and_abandon(self):
        row = block(APP, 'function taskRow(')
        self.assertIn("t.state==='uncertain'", row)
        self.assertIn("['accept','abandon']", row)
        self.assertIn('data-task-resolve', row)
        self.assertIn('t.remote_done_at', row)
        self.assertIn("action('task/reconcile',{id,resolution:res,note:", APP)
        self.assertIn("body.get('note'", SERVER)
        self.assertIn("body['resolution']", SERVER)
        self.assertIn('def resolve_uncertain', ENGINE)
        self.assertIn("'accept'", ENGINE)
        self.assertIn("'abandon'", ENGINE)

    def test_liveness_labels(self):
        row = block(APP, 'function taskRow(')
        dash = block(APP, 'function renderWorkerDashboard(){')
        for text in (row, dash):
            self.assertIn('last_output_at', text)
            self.assertIn('first_output_at', text)
            self.assertIn('last_tool_at', text)
        self.assertIn("'Last output '", row)
        self.assertIn("'Last tool step '", row)
        self.assertIn("cell('Last output'", dash)
        self.assertIn("cell('Last tool step'", dash)
        self.assertIn("' min ago'", APP)

    def test_worker_dashboard_context_details(self):
        dash = block(APP, 'function renderWorkerDashboard(){')
        for needle in ('context_usage', 'w.generation', 'retired_threads', "cell('Generation'",
                       "cell('Retired chats'", 'Fresh context', 'data-worker-fresh', "w.state==='ready'",
                       'stale_context_tasks'):
            self.assertIn(needle, dash)
        self.assertIn("action('chat/compress',{worker_id:d.workerFresh}", APP)
        self.assertIn("'/api/chat/compress'", SERVER)
        self.assertIn("body.get('worker_id')", SERVER)

    def test_settings_inputs(self):
        for name in SETTINGS:
            self.assertIn('name="%s"' % name, HTML)
            self.assertIn("'%s' in body" % name, SERVER)
        for value in ('new_welcome', 'same_thread', 'hint', 'auto'):
            self.assertIn('value="%s"' % value, HTML)
        self.assertIn('type="checkbox" name="coworker_delete_old_chat"', HTML)
        self.assertIn("element.type==='checkbox'", block(APP, 'function renderSettings(){'))
        self.assertIn("el.type==='checkbox')data[el.name]=el.checked", APP)

    def test_new_strings_are_english(self):
        segments = [block(APP, 'function renderAttention(){'), block(APP, 'function taskRow('),
                    block(APP, 'function renderWorkerDashboard(){'), block(APP, 'function mainReasons('),
                    block(APP, 'if(d.taskResolve)', 'if(d.workerFresh)'),
                    block(APP, 'if(d.workerFresh)', '\n'),
                    block(HTML, '<h2>Context rotation</h2>', '</label><h2>'),
                    block(HTML, '<div id="attention-banner"', '\n')]
        for text in segments:
            self.assertIsNone(GERMAN.search(text), text[:80])

    def test_web_files_stay_lf(self):
        for path in (WEB / 'app.js', WEB / 'index.html', Path(__file__)):
            self.assertNotIn(b'\r', path.read_bytes(), path.name)


if __name__ == '__main__':
    unittest.main()
