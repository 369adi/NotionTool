"""Unit tests for the resume brief in MainHandoffOps.summarize(); fakes only, no network."""
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from notiontool import handoff_files
from notiontool import main_handoff as mh_mod
from notiontool import main_handoff_ops as ops_mod
from notiontool.main_handoff_ops import MainHandoffOps, build_resume_brief, extract_next_steps

ACCT = 'acct'
T0 = 1_000_000.0
TOKEN_RE = re.compile(r'ack token alone on its own line:\n([0-9a-f]+)')
UMLAUT_O = '\u00f6'
UMLAUT_A = '\u00e4'


class FakeStore:
    def __init__(self, directory, messages=()):
        self.directory = Path(directory)
        self.messages = list(messages)
        self.events = []

    def all(self, kind):
        return []

    def get(self, kind, ident, default=None):
        return default

    def put(self, kind, doc):
        return doc

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))

    def settings(self):
        return {}

    def message_page(self, chat_id, limit=100):
        return {'messages': [m for m in self.messages if m.get('chat_id') == chat_id][-limit:]}


class FakeAdapter:
    """Any remote call fails the test: summarize must stay local."""

    def __getattr__(self, name):
        raise AssertionError('summarize must not call the adapter: ' + name)


class ResumeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mh_resume_')
        self.store = FakeStore(self.tmp)
        self.ops = MainHandoffOps(self.store, FakeAdapter(), ACCT, old_chat_id='old-thread',
                                  clock=lambda: T0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self):
        return os.path.join(self.tmp, 'handoffs', self.ops.file_id() + '.json')

    def record(self):
        with open(self.path(), 'r', encoding='utf-8') as fh:
            return json.load(fh)


class PayloadTests(ResumeBase):
    def test_all_fields_present_with_data(self):
        sources = dict(memory_tail='## Next\n- write the docs\n', claims=[],
                       open_runs=[dict(id='r1', state='executing', goal='Build the page', tasks=[])],
                       messages=[dict(role='user', text='Please add the page'),
                                 dict(role='assistant', text='I started the page')])
        message = self.ops.summarize(sources)
        rec = self.record()
        for key in ('kind', 'account_id', 'handoff', 'old_thread', 'new_thread', 'schema_version',
                    'token', 'created', 'acked_at', 'first_task_done_at'):
            self.assertIn(key, rec)
        self.assertEqual(rec['kind'], 'main_handoff')
        self.assertEqual(rec['account_id'], ACCT)
        self.assertEqual(rec['old_thread'], 'old-thread')
        self.assertIsNone(rec['new_thread'])
        self.assertEqual(rec['handoff']['memory_tail'], sources['memory_tail'])
        brief = rec['resume_brief']
        self.assertEqual(sorted(brief), ['last_answer', 'last_user_request', 'next_steps', 'open_items'])
        self.assertEqual(brief['last_user_request'], 'Please add the page')
        self.assertEqual(brief['last_answer'], 'I started the page')
        self.assertEqual(brief['next_steps'], ['write the docs'])
        self.assertEqual(brief['open_items'], ['Build the page (executing)'])
        self.assertEqual(rec['resume_instructions'], ops_mod.RESUME_INSTRUCTIONS)
        self.assertEqual(TOKEN_RE.search(message).group(1), rec['token'])

    def test_empty_inputs_keep_every_field(self):
        self.ops.summarize({})
        brief = self.record()['resume_brief']
        self.assertEqual(brief, dict(last_user_request='', last_answer='', next_steps=[], open_items=[]))
        for value in (None, {}, dict(messages=None, memory_tail=None, open_runs=None)):
            self.assertEqual(build_resume_brief(value),
                             dict(last_user_request='', last_answer='', next_steps=[], open_items=[]))

    def test_messages_come_from_store_when_sources_have_none(self):
        self.store.messages = [dict(chat_id='chat-1', role='user', text='Older question'),
                               dict(chat_id='chat-1', role='assistant', text='Older answer'),
                               dict(chat_id='chat-1', role='user', text='Newest question')]
        self.ops.main_chat_id = 'chat-1'
        self.ops.summarize(dict(memory_tail='', open_runs=[], claims=[]))
        brief = self.record()['resume_brief']
        self.assertEqual(brief['last_user_request'], 'Newest question')
        self.assertEqual(brief['last_answer'], 'Older answer')

    def test_unacked_file_is_returned_unchanged(self):
        first = handoff_files.write_handoff(os.path.join(self.tmp, 'handoffs'), self.ops.file_id(),
                                            dict(kind='main_handoff', handoff={'memory_tail': 'old'}), T0 - 50)
        before = Path(first['path']).read_bytes()
        message = self.ops.summarize(dict(memory_tail='## Next\n- new step', open_runs=[], claims=[],
                                          messages=[dict(role='user', text='new')]))
        self.assertEqual(Path(first['path']).read_bytes(), before)
        self.assertEqual(TOKEN_RE.search(message).group(1), first['token'])
        self.assertNotIn('resume_brief', self.record())
        again = self.ops.summarize({})
        self.assertEqual(TOKEN_RE.search(again).group(1), first['token'])
        self.assertEqual(Path(first['path']).read_bytes(), before)


class MessageLimitTests(unittest.TestCase):
    def test_user_request_cut_to_1500(self):
        brief = build_resume_brief(dict(messages=[dict(role='user', text='old'),
                                                  dict(role='user', text='u' * 2000)]))
        self.assertEqual(brief['last_user_request'], 'u' * 1500)

    def test_answer_cut_to_1500(self):
        brief = build_resume_brief(dict(messages=[dict(role='assistant', text='a' * 1600),
                                                  dict(role='user', text='q')]))
        self.assertEqual(brief['last_answer'], 'a' * 1500)
        self.assertEqual(brief['last_user_request'], 'q')

    def test_newest_non_empty_message_wins(self):
        brief = build_resume_brief(dict(messages=[dict(role='user', text='real'), dict(role='user', text='  ')]))
        self.assertEqual(brief['last_user_request'], 'real')


class NextStepTests(unittest.TestCase):
    def test_german_heading_with_umlaut(self):
        tail = 'Intro\n## N' + UMLAUT_A + 'chster Schritt\n- Tests schreiben\n## Erledigt\n- alter Punkt\n'
        self.assertEqual(extract_next_steps(tail), ['Tests schreiben'])

    def test_german_heading_without_umlaut(self):
        tail = '### Naechster Schritt\n1. Seite bauen\n2. Knopf testen\n# Notes\nnicht mitnehmen\n'
        self.assertEqual(extract_next_steps(tail), ['Seite bauen', 'Knopf testen'])

    def test_offen_heading_and_label(self):
        tail = '## OFFEN\n- Freigabe fehlt\nOffene Punkte:\n- Datei pruefen\nFertig:\n- nichts\n'
        self.assertEqual(extract_next_steps(tail), ['Freigabe fehlt', 'Datei pruefen'])

    def test_english_headings_and_labels(self):
        tail = ('## next steps\n- wire the module\n## Done\n- old\n'
                'Open:\n- check the bytes\n**Notes**\n- skip me\nNext: run the tests\n')
        self.assertEqual(extract_next_steps(tail),
                         ['wire the module', 'check the bytes', 'run the tests'])

    def test_bold_label_and_inline_german_label(self):
        tail = '**Open**\n* first\n## Other\n- no\nN' + UMLAUT_A + 'chster Schritt: zweiter\n'
        self.assertEqual(extract_next_steps(tail), ['first', 'zweiter'])

    def test_cap_of_eight(self):
        tail = '## Next\n' + ''.join('- step %d\n' % i for i in range(12))
        steps = build_resume_brief(dict(memory_tail=tail))['next_steps']
        self.assertEqual(steps, ['step %d' % i for i in range(8)])

    def test_no_matching_heading_gives_empty_list(self):
        self.assertEqual(extract_next_steps('## Done\n- a\nplain line\n'), [])
        self.assertEqual(extract_next_steps(''), [])
        self.assertEqual(extract_next_steps(None), [])


class OpenItemTests(unittest.TestCase):
    def test_title_is_used_with_state(self):
        brief = build_resume_brief(dict(open_runs=[dict(id='r1', title='Resume summary', goal='x' * 300,
                                                        state='blocked')]))
        self.assertEqual(brief['open_items'], ['Resume summary (blocked)'])

    def test_goal_cut_to_120_when_no_title(self):
        goal = 'g' * 119 + 'HIDDEN' + 'z' * 50
        brief = build_resume_brief(dict(open_runs=[dict(id='r2', goal=goal, state='executing')]))
        self.assertEqual(brief['open_items'], ['g' * 119 + 'H (executing)'])

    def test_one_line_per_run_without_ids(self):
        runs = [dict(id='run_abc123', goal='First goal', state='queued'),
                dict(id='run_def456', title='', goal='Second\ngoal', state='paused')]
        items = build_resume_brief(dict(open_runs=runs))['open_items']
        self.assertEqual(items, ['First goal (queued)', 'Second goal (paused)'])
        self.assertFalse(any('run_' in item for item in items))


class InstructionTests(unittest.TestCase):
    def test_instructions_name_the_three_german_headings(self):
        text = ops_mod.RESUME_INSTRUCTIONS
        for heading in ('Wo wir aufgeh' + UMLAUT_O + 'rt haben', 'Was ich noch einbauen wollte',
                        'Was noch offen ist'):
            self.assertIn(heading, text)

    def test_instructions_rules(self):
        text = ops_mod.RESUME_INSTRUCTIONS
        self.assertIn('wait for the user', text)
        self.assertIn('first message after the ack', text)
        self.assertIn('German', text)
        self.assertIn('plain and simple words', text)
        self.assertIn('no internal ids, hashes, run ids or file:line references', text)
        self.assertIn('continue with the last open request', text)
        self.assertIsNone(re.search(r'[0-9a-f]{8,}', text))


class DictStore:
    """Small in-memory store with the calls MainHandoff.step and summarize use."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.data = {}
        self.messages = []
        self.events = []

    def all(self, kind):
        return [dict(doc) for doc in self.data.get(kind, {}).values()]

    def get(self, kind, ident, default=None):
        doc = self.data.get(kind, {}).get(ident)
        return dict(doc) if doc is not None else default

    def put(self, kind, doc):
        self.data.setdefault(kind, {})[doc['id']] = dict(doc)
        return doc

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))

    def settings(self):
        return {}

    def message_page(self, chat_id, limit=100):
        return {'messages': [m for m in self.messages if m.get('chat_id') == chat_id][-limit:]}


MEMORY_TEXT = ('Notes from earlier work\n'
               '## Open\n'
               '- Add the resume summary to the handoff file\n'
               '- Check the first message after the ack\n'
               '## Done\n'
               '- run_abc123 finished in engine.py:812\n')


class MemoryTailFromStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mh_resume_e2e_')
        self.store = DictStore(self.tmp)
        self.store.put('chats', dict(id='chat-main', folder_id='folder-1', main_worker='w-main'))
        self.store.put('memory', dict(id='folder-1', text=MEMORY_TEXT))
        self.store.put('memory', dict(id='chat-main', text='## Open\n- wrong memory source\n'))
        self.store.put('runs', dict(id='run_abc123', state='executing', goal='Build the resume summary',
                                    main_worker=ACCT + ':main'))
        self.store.messages = [dict(chat_id='chat-main', role='user', text='Please finish the resume summary'),
                               dict(chat_id='chat-main', role='assistant', text='I am on it')]
        self.ops = MainHandoffOps(self.store, FakeAdapter(), ACCT, old_chat_id='old-thread',
                                  main_chat_id='chat-main', clock=lambda: T0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def record(self):
        path = os.path.join(self.tmp, 'handoffs', self.ops.file_id() + '.json')
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)

    def test_step_from_summary_requested_writes_next_steps_from_main_memory(self):
        self.store.put('main_handoffs', dict(id='mh_1', account_id=ACCT, old_chat_id='old-thread',
                                             state='summary_requested'))
        doc = mh_mod.MainHandoff(self.store, self.ops).step('mh_1')
        self.assertEqual(doc['state'], 'summary_done')
        rec = self.record()
        self.assertEqual(TOKEN_RE.search(doc['summary']).group(1), rec['token'])
        brief = rec['resume_brief']
        self.assertEqual(brief['next_steps'], ['Add the resume summary to the handoff file',
                                               'Check the first message after the ack'])
        for step in brief['next_steps']:
            self.assertIsNone(re.search(r'run_|[0-9a-f]{8,}|\.py:\d+', step))
        self.assertEqual(brief['open_items'], ['Build the resume summary (executing)'])
        self.assertEqual(brief['last_user_request'], 'Please finish the resume summary')
        self.assertEqual(rec['handoff']['memory_tail'], MEMORY_TEXT)

    def test_memory_tail_keeps_last_4000_chars(self):
        long_text = 'x' * 5000 + '\n## Open\n- last step\n'
        self.store.put('memory', dict(id='folder-1', text=long_text))
        self.ops.summarize(dict(memory_tail='', open_runs=[], claims=[]))
        rec = self.record()
        self.assertEqual(rec['handoff']['memory_tail'], long_text[-4000:])
        self.assertEqual(len(rec['handoff']['memory_tail']), 4000)
        self.assertEqual(rec['resume_brief']['next_steps'], ['last step'])

    def test_chat_memory_is_used_without_folder(self):
        self.store.put('chats', dict(id='chat-main', main_worker='w-main'))
        self.ops.summarize(dict(open_runs=[], claims=[]))
        self.assertEqual(self.record()['resume_brief']['next_steps'], ['wrong memory source'])

    def test_given_memory_tail_is_not_replaced(self):
        self.ops.summarize(dict(memory_tail='## Next\n- given step\n', open_runs=[], claims=[]))
        rec = self.record()
        self.assertEqual(rec['resume_brief']['next_steps'], ['given step'])
        self.assertEqual(rec['handoff']['memory_tail'], '## Next\n- given step\n')

    def test_no_main_chat_gives_empty_memory(self):
        self.ops.main_chat_id = ''
        self.ops.summarize(dict(open_runs=[], claims=[]))
        self.assertEqual(self.record()['resume_brief']['next_steps'], [])


if __name__ == '__main__':
    unittest.main()
