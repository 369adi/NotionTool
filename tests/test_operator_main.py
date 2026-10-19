import json
import tempfile
import unittest

from notiontool.chat_pool import pick_main
from notiontool.engine import Engine
from notiontool.harness_account import main_chat
from notiontool.main_repoint import repoint
from notiontool.operator_main import RESULT_LINE, delegate, operator_chat, register_operator, result_done
from notiontool.store import Store

ACC = 'acc1'
OP = ACC + ':op-thread'
MAIN = ACC + ':main-thread'
CO1, CO2 = ACC + ':co1', ACC + ':co2'


class Adapter:
    pass


def tasks(*idents, deps=None):
    deps = deps or {}
    return [dict(id=i, title='Task ' + i, prompt='Do ' + i, acceptance='Report ' + i, depends_on=deps.get(i, []),
                 use_main=True) for i in idents]


class OperatorMainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s = Store(self.temp.name)
        s.put('accounts', dict(id=ACC, state='ready', team_threads={'main': {'id': 'main-thread', 'pending': False}}))
        # The operator's own chat looks like a joined Welcome chat: exactly the case the pool used to re-enable.
        s.put('workers', dict(id=OP, account_id=ACC, thread_id='op-thread', purpose='worker', enabled=False,
                              state='ready', lease='', label='Welcome to Notion', welcome=True, join_state='joined'))
        s.put('workers', dict(id=MAIN, account_id=ACC, thread_id='main-thread', purpose='main', enabled=True,
                              state='ready', lease='', label='Welcome to Notion'))
        for ident in (CO1, CO2):
            s.put('workers', dict(id=ident, account_id=ACC, thread_id=ident.split(':')[1], purpose='worker',
                                  enabled=True, state='ready', lease='', label='Welcome to Notion', join_state='joined'))
        s.put('chats', dict(id='c4', title='Main 4', main_worker=MAIN, state='active'))
        self.engine = Engine(s, Adapter())

    def tearDown(self):
        self.engine.close()
        close = getattr(self.store, 'close', None)
        if close:
            close()
        self.temp.cleanup()

    def register(self):
        return register_operator(self.store, OP, project_path=self.temp.name)

    def test_register_makes_a_reserved_operator_chat(self):
        first = self.register()
        worker = self.store.get('workers', OP)
        self.assertEqual((worker['purpose'], worker['enabled'], worker['lease']), ('operator', False, ''))
        chat = self.store.get('chats', first['chat_id'])
        self.assertEqual((chat['operator'], chat['main_worker'], chat['title']), (True, OP, 'Operator Main'))
        again = self.register()
        self.assertEqual((again['chat_id'], again['created']), (first['chat_id'], False))
        self.assertEqual(operator_chat(self.store, ACC)['id'], first['chat_id'])

    def test_register_refuses_mains_and_busy_chats(self):
        with self.assertRaisesRegex(ValueError, 'is a Main'):
            register_operator(self.store, MAIN)
        busy = self.store.get('workers', CO1)
        busy.update(state='busy', lease='L')
        self.store.put('workers', busy)
        with self.assertRaisesRegex(ValueError, 'still holds a harness task'):
            register_operator(self.store, CO1)
        self.register()
        with self.assertRaisesRegex(ValueError, 'already has an operator Main'):
            register_operator(self.store, CO2)

    def test_operator_is_never_main_coworker_or_repointed(self):
        chat_id = self.register()['chat_id']
        worker = self.store.get('workers', OP)
        worker['label'] = 'Main'
        self.store.put('workers', worker)
        workers = self.store.all('workers')
        self.assertEqual(pick_main(workers, 'op-thread')['id'], MAIN)
        self.assertIsNone(pick_main([w for w in workers if w['id'] != MAIN], 'op-thread'))
        self.assertEqual(main_chat(self.store, ACC)['id'], 'c4')
        self.store.put('chats', dict(self.store.get('chats', 'c4'), main_worker=''))  # Main 4 chat gone: no chat holds MAIN
        repoint(self.store, ACC, self.store.get('workers', MAIN))
        self.assertEqual(self.store.get('chats', chat_id)['main_worker'], OP)

    def test_delegate_queues_coworker_tasks_without_any_main_turn(self):
        chat_id = self.register()['chat_id']
        result = delegate(self.engine, chat_id, 'Fix two things', tasks('a', 'b', 'c', deps={'c': ['a', 'b']}))
        run = self.store.get('runs', result['run_id'])
        self.assertEqual((run['state'], run['operator'], run['main_worker'], run['worker_budget']), ('executing', True, OP, 2))
        queued = self.engine.tasks(run['id'])
        self.assertEqual(sorted(t['role'] for t in queued), ['executor'] * 3)
        self.assertFalse(any(t['use_main'] for t in queued))
        by_title = {t['title']: t for t in queued}
        self.assertEqual(sorted(by_title['Task c']['deps']), sorted([by_title['Task a']['id'], by_title['Task b']['id']]))
        for task in queued:
            task.update(state='done', result='Fixed.\nRESULT: DONE')
            self.store.put('tasks', task)
        self.engine._advance(self.store.get('runs', run['id']))
        run = self.store.get('runs', run['id'])
        self.assertEqual(run['state'], 'done')  # every answer passed the machine check, so no Main stage
        self.assertIn('3 delegated task(s) finished', run['summary'])
        self.assertEqual([t['role'] for t in self.engine.tasks(run['id'])], ['executor'] * 3)  # no reviewer, no integrator

    def test_delegate_is_idempotent_and_defaults_to_the_operator_chat(self):
        self.register()
        first = delegate(self.engine, '', 'Goal', tasks('a'), request_id='req-1')
        again = delegate(self.engine, '', 'Goal', tasks('a'), request_id='req-1')
        self.assertEqual((again['run_id'], again['duplicate']), (first['run_id'], True))
        self.assertEqual(len([r for r in self.store.all('runs') if r.get('operator')]), 1)

    def test_delegate_refusals(self):
        with self.assertRaisesRegex(ValueError, 'Register this chat'):
            delegate(self.engine, 'c4', 'Goal', tasks('a'))
        chat_id = self.register()['chat_id']
        bad = tasks('a')
        bad[0]['acceptance'] = ''
        with self.assertRaisesRegex(ValueError, 'needs acceptance'):
            delegate(self.engine, chat_id, 'Goal', bad)
        with self.assertRaisesRegex(ValueError, 'non-empty list'):
            delegate(self.engine, chat_id, 'Goal', [])
        for ident in (CO1, CO2):
            worker = self.store.get('workers', ident)
            worker['enabled'] = False
            self.store.put('workers', worker)
        with self.assertRaisesRegex(ValueError, 'No enabled Coworker'):
            delegate(self.engine, chat_id, 'Goal', tasks('a'))
        self.assertEqual([r for r in self.store.all('runs') if r.get('operator')], [])

    def test_submit_refuses_the_operator_chat(self):
        chat_id = self.register()['chat_id']
        with self.assertRaisesRegex(ValueError, 'operator Main delegates'):
            self.engine.submit(chat_id, 'hello')


    def test_delegate_appends_the_result_line(self):
        chat_id = self.register()['chat_id']
        result = delegate(self.engine, chat_id, 'Goal', tasks('a', 'b'))
        queued = self.engine.tasks(result['run_id'])
        self.assertEqual(len(queued), 2)
        for task in queued:
            self.assertTrue(task['prompt'].startswith('Do '), task['prompt'])
            self.assertTrue(task['prompt'].endswith('\n\n' + RESULT_LINE), task['prompt'])
            self.assertEqual(task['prompt'].count(RESULT_LINE), 1)

    def test_result_done_needs_an_exact_line(self):
        self.assertTrue(result_done('Work done.\n  result: DONE  '))
        self.assertTrue(result_done(json.dumps(dict(status='done', summary='Fixed.\nRESULT: DONE'))))
        self.assertFalse(result_done('RESULT: NOT DONE: no access'))
        self.assertFalse(result_done('I wrote RESULT: DONE in a sentence'))
        self.assertFalse(result_done('RESULT: DONE.'))
        self.assertFalse(result_done(''))

    def finish_all(self, run_id, answers):
        ids = {}
        for task in self.engine.tasks(run_id):
            task.update(state='done', result=answers[task['title']])
            self.store.put('tasks', task)
            ids[task['title']] = task['id']
        self.engine._advance(self.store.get('runs', run_id))
        return self.store.get('runs', run_id), ids

    def test_operator_run_accepts_only_result_done_answers(self):
        chat_id = self.register()['chat_id']
        result = delegate(self.engine, chat_id, 'Goal', tasks('a', 'b', 'c', 'd'))
        run, ids = self.finish_all(result['run_id'], {
            'Task a': json.dumps(dict(status='done', summary='Fixed it.\n  result: done  ')),
            'Task b': json.dumps(dict(status='done', summary='Could not open the file.\nRESULT: NOT DONE: no access')),
            'Task c': 'ok',
            'Task d': 'I would write RESULT: DONE but could not'})
        rejected = [ids[k] for k in ('Task b', 'Task c', 'Task d')]
        # Three-stage acceptance: answers without RESULT: DONE stop the run for the Main.
        self.assertEqual(run['state'], 'awaiting_main')
        self.assertEqual(sorted(run['needs_main']), sorted(rejected))
        self.assertEqual(sorted(run['unaccepted']), sorted(rejected))
        self.assertIn('4 delegated task(s) finished; 1 accepted with RESULT: DONE; not accepted: ', run['summary'])
        for ident in rejected:
            self.assertIn(ident, run['summary'])
        self.assertNotIn(ids['Task a'], run['summary'])

    def test_operator_run_with_every_task_accepted(self):
        chat_id = self.register()['chat_id']
        result = delegate(self.engine, chat_id, 'Goal', tasks('a', 'b'))
        run, _ = self.finish_all(result['run_id'], {'Task a': 'Done.\nRESULT: DONE', 'Task b': 'result: done'})
        self.assertEqual((run['state'], run['unaccepted']), ('done', []))
        self.assertIn('2 delegated task(s) finished; 2 accepted with RESULT: DONE', run['summary'])
        self.assertNotIn('not accepted', run['summary'])

    def test_delegate_warns_about_missing_bridge_and_project_tools(self):
        chat_id = self.register()['chat_id']
        work = tasks('a', 'b')
        work[0]['files'] = ['notes.txt']
        result = delegate(self.engine, chat_id, 'Goal', work)
        self.assertEqual(self.store.get('runs', result['run_id'])['state'], 'executing')  # warnings never block
        warnings = result['warnings']
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any('bridge_connected' in w and '(a)' in w for w in warnings), warnings)
        self.assertTrue(any('advertise_project_tools' in w for w in warnings), warnings)
        self.assertEqual(delegate(self.engine, chat_id, 'Goal 2', tasks('c'))['warnings'],
                         ['Setting advertise_project_tools is off: Coworkers get no project tools'])
        account = self.store.get('accounts', ACC)
        account['bridge_connected'] = True
        self.store.put('accounts', account)
        settings = self.store.settings()
        settings['advertise_project_tools'] = True
        self.store.put('settings', settings)
        more = tasks('d')
        more[0]['files'] = ['other.txt']
        self.assertEqual(delegate(self.engine, chat_id, 'Goal 3', more)['warnings'], [])

class UnknownTaskKeyWarningTest(__import__('unittest').TestCase):
    def test_unknown_keys_are_reported_and_known_are_not(self):
        from notiontool import operator_main

        class Store:
            def get(self, *a, **k):
                return {}

            def settings(self):
                return {'advertise_project_tools': True}

        warnings = operator_main._warnings(Store(), {}, [
            {'id': 'T1', 'title': 'a', 'prompt': 'p', 'acceptance': 'x', 'depends_on': [], 'summary': 's'},
            {'id': 'T2', 'title': 'b', 'prompt': 'p', 'acceptance': 'x', 'needs': ['T1'], 'prio': 1}])
        self.assertEqual(warnings, ['Task T2: unknown key(s) needs, prio were ignored'])


if __name__ == '__main__':
    unittest.main()
