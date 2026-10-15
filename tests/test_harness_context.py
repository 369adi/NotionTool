import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path

from notiontool.store import Store
from notiontool.chat_context import ChatContext, estimate_tokens
from notiontool.harness_account import selected_account, select_account
from notiontool.edge_transport import EdgeTransport
from notiontool.vendor.transport import NotionStreamError, NotionHttpError


class ContextTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).parent / '.test-data'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.store = Store(self.temp.name)
        self.store.put('accounts', dict(id='a', team_threads={'main': {'id': 'old'}}))
        self.worker = dict(id='logical-main', account_id='a', thread_id='old', purpose='main',
                           enabled=True, state='busy', lease='lease')
        self.store.put('workers', self.worker)
        self.task = dict(id='t', run_id='r', worker_id='logical-main', state='running')
        self.store.put('tasks', self.task)
        self.store.put('runs', dict(id='r', chat_id='c', goal='Keep every constraint', project_path='C:/project'))
        self.context = ChatContext(self.store)
        self.client = SimpleNamespace(info={'space_id': 'space'}, model=lambda:'opus', threads=lambda:[],
            transport=SimpleNamespace(stream=Mock(return_value=iter([]))), headers=lambda:{})
        self.adapter = SimpleNamespace(store=self.store, new_id=lambda *args:'new',
            welcome_body=lambda *args:{'createThread':True}, ensure_thread_model=Mock())
        self.idle = {'session': {'status':'idle'}, 'patches':[]}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def limit(self):
        settings = self.store.settings(); settings['context_turn_limit'] = 1
        self.store.put('settings', settings)
        self.store.put('tasks', dict(id='previous', state='done', thread_id='old', sent_at=1,
            submitted_prompt='Previous instructions', raw_response='Done'))

    def test_main_rotation_is_atomic_and_preserves_logical_references(self):
        self.limit()
        self.store.put('chats', {'id':'c', 'main_worker':'logical-main'})
        updated = self.context.rotate(self.adapter, self.client, self.worker, self.task, self.idle, threading.Event())
        self.assertEqual(updated['thread_id'], 'old')  # same Welcome chat
        self.assertFalse(updated['context_reset'])
        self.assertEqual(updated['context_mode'], 'soft')
        self.assertEqual(updated['id'], 'logical-main')
        self.assertEqual(updated['purpose'], 'main')
        self.assertEqual(updated['lease'], 'lease')
        self.assertIn('Keep every constraint', updated['handoff'])
        self.assertEqual(self.store.get('chats', 'c')['main_worker'], updated['id'])
        self.assertEqual(self.store.get('accounts','a')['team_threads']['main']['id'], 'old')
        gen = self.store.get('thread_generations','old:1')
        self.assertEqual((gen['successor'], gen['mode']), ('old', 'same_thread'))
        self.assertIsNone(self.store.get('rotations','logical-main'))

    def test_full_mode_requests_server_side_replacement(self):
        self.limit()
        settings = self.store.settings(); settings['context_replace_mode'] = 'full'
        self.store.put('settings', settings)
        updated = self.context.rotate(self.adapter, self.client, self.worker, self.task, self.idle, threading.Event())
        self.assertTrue(updated['context_reset'])
        self.assertEqual(updated['context_mode'], 'full')

    def test_reset_starts_a_new_usage_generation(self):
        self.limit()
        updated = self.context.rotate(self.adapter, self.client, self.worker, self.task, self.idle, threading.Event())
        self.assertEqual(updated['generation'], 1)
        self.assertEqual(self.context.usage(updated, self.idle)['turns'], 0)
        again = self.context.rotate(self.adapter, self.client, updated, self.task, self.idle, threading.Event())
        self.assertEqual(again['generation'], 1)

    def test_not_accepted_needs_idle_session_and_a_later_turn(self):
        import datetime, time
        from notiontool.notion import not_accepted
        sent = time.time() - 60
        iso = lambda ts: datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
        task, idle = dict(sent_at=sent, context_reset=True), {'session': {'status': 'idle'}}
        later = [dict(kind='assistant_message', sequence=9, created_at=iso(sent + 5))]
        self.assertTrue(not_accepted(task, idle, later))
        self.assertFalse(not_accepted(task, {'session': {'status': 'running'}}, later))
        self.assertFalse(not_accepted(task, dict(idle, session_pending=True), later))
        self.assertFalse(not_accepted(dict(task, sent_at=time.time()), idle, later))
        self.assertFalse(not_accepted(task, idle, [dict(later[0], created_at=iso(sent - 5))]))

    def test_same_thread_reset_creates_no_chat(self):
        self.limit()
        updated = ChatContext(self.store).rotate(self.adapter, self.client, self.worker, self.task, self.idle, threading.Event())
        self.client.transport.stream.assert_not_called()
        self.assertEqual(updated['thread_id'], 'old')
        self.assertEqual(updated['generation'], 1)
        self.assertFalse(updated['handoff_delivered'])

    def test_compress_request_triggers_reset_below_limit(self):
        worker = dict(self.worker, compress_requested=True, compress_reason='manual')
        self.store.put('workers', worker)
        updated = self.context.rotate(self.adapter, self.client, worker, self.task, self.idle, threading.Event())
        self.assertFalse(updated['context_reset'])
        self.assertEqual(updated['context_mode'], 'soft')
        self.assertFalse(updated['compress_requested'])
        self.assertEqual(updated['thread_id'], 'old')

    def test_pending_native_work_prevents_rotation(self):
        self.limit()
        with self.assertRaisesRegex(ValueError, 'idle'):
            self.context.rotate(self.adapter,self.client,self.worker,self.task,
                dict(self.idle,session_pending=True),threading.Event())
        self.client.transport.stream.assert_not_called()

    def test_uncertain_previous_assignment_prevents_rotation(self):
        self.limit()
        self.store.put('tasks', dict(id='unknown', worker_id=self.worker['id'], state='blocked',remote_unknown=True))
        with self.assertRaisesRegex(ValueError,'unresolved'):
            self.context.rotate(self.adapter,self.client,self.worker,self.task,self.idle,threading.Event())
        self.client.transport.stream.assert_not_called()

    def test_cancellation_before_creation_does_not_send_request(self):
        self.limit(); cancel = threading.Event(); cancel.set()
        with self.assertRaisesRegex(ValueError,'cancelled'):
            self.context.rotate(self.adapter,self.client,self.worker,self.task,self.idle,cancel)
        self.client.transport.stream.assert_not_called()

    def test_small_context_does_not_create_a_chat(self):
        updated = self.context.rotate(self.adapter,self.client,self.worker,self.task,self.idle,threading.Event())
        self.assertEqual(updated['thread_id'], 'old')
        self.client.transport.stream.assert_not_called()

    def test_observed_history_accumulates_without_counting_repeated_snapshots(self):
        one = {'patches':[{'op':'put','entity':{'id':'e1','sequence':1,'content':'a'*100}}]}
        self.context.observe(self.worker,one)
        first = self.context.observe(self.worker,one)
        self.assertEqual(len(first['entities']),1)
        self.context.observe(self.worker, {'patches':[{'op':'put','entity':{'id':'e2','sequence':2,'content':'later'}}]})
        record = self.store.get('thread_context','a:old')
        self.assertEqual(set(record['entities']), {'e1','e2'})
        self.assertGreaterEqual(estimate_tokens('漢字'), 2)

    def test_full_memory_survives_working_index_limits(self):
        self.store.remember('c', 'initial rule ' + 'x'*25000, 't1')
        self.store.remember('c', 'latest result ' + 'y'*25000, 't2')
        entries = self.store.all('memory_entries')
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[0]['text'].startswith('initial rule'))
        self.assertLessEqual(len(self.store.get('memory','c')['text']),24000)

    def test_account_selection_never_guesses_between_multiple_accounts(self):
        self.assertEqual(selected_account(self.store), 'a')
        self.store.put('accounts',{'id':'b'})
        self.assertEqual(selected_account(self.store), '')
        self.store.delete('tasks','t')
        select_account(self.store, 'b')
        self.assertEqual(selected_account(self.store), 'b')

    def test_account_cannot_change_during_unconfirmed_work(self):
        self.store.put('accounts',{'id':'b'})
        with self.assertRaisesRegex(ValueError, 'unconfirmed'):
            select_account(self.store, 'b')

    def test_semantic_checkpoint_recovers_once_before_welcome_creation(self):
        from notiontool.engine import PendingRemote, ContextCheckpointPending
        self.limit()
        self.adapter.semantic_context = True
        self.adapter.body = Mock(return_value={'summary':True})
        checkpoint = dict(summary='Continue the verified implementation', user_constraints=['English game text'],
            decisions=['Use one account'], completed=['Saved file'], open_work=['Check layout'],
            evidence=['Unit tests passed'], files=['app.py'], next_steps=['Inspect result'])
        self.adapter.recover = Mock(side_effect=[PendingRemote(), json.dumps(checkpoint)])
        self.context.archive_native = Mock()
        with self.assertRaises(ContextCheckpointPending):
            self.context.rotate(self.adapter,self.client,self.worker,self.task,self.idle,threading.Event())
        self.assertEqual(self.client.transport.stream.call_count, 1)
        self.assertEqual(self.store.get('workers',self.worker['id'])['thread_id'], 'old')
        updated = self.context.rotate(self.adapter,self.client,self.worker,self.task,self.idle,threading.Event())
        self.assertEqual(self.client.transport.stream.call_count, 1)  # One summary, no new chat.
        self.assertEqual(updated['thread_id'], 'old')
        self.assertIn('English game text', updated['handoff'])
        self.assertFalse(updated['handoff_delivered'])

    def test_incomplete_semantic_checkpoint_keeps_old_chat(self):
        self.limit()
        self.adapter.semantic_context = True
        self.adapter.body = Mock(return_value={})
        self.adapter.recover = Mock(return_value='{"summary":"missing evidence and constraints"}')
        self.context.archive_native = Mock()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.context.rotate(self.adapter,self.client,self.worker,self.task,self.idle,threading.Event())
        self.assertEqual(self.store.get('workers',self.worker['id'])['thread_id'], 'old')
        self.assertEqual(self.client.transport.stream.call_count, 1)

    def test_confirmed_checkpoint_turn_error_allows_one_fresh_summary(self):
        from notiontool.engine import RateLimited
        self.limit()
        self.adapter.semantic_context = True
        self.adapter.body = Mock(return_value={'summary': True})
        checkpoint = dict(summary='Fresh summary', user_constraints=['Keep Welcome chats'], decisions=[],
            completed=[], open_work=[], evidence=[], files=[], next_steps=[])
        self.adapter.recover = Mock(side_effect=[RateLimited(10, 'turn error', confirmed=True), json.dumps(checkpoint)])
        self.context.archive_native = Mock()
        with self.assertRaises(RateLimited):
            self.context.rotate(self.adapter, self.client, self.worker, self.task, self.idle, threading.Event())
        summary = [t for t in self.store.all('tasks') if t.get('role') == 'context_compactor']
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]['sent_at'], 0)  # Notion confirmed the failed turn: one new summary may follow.
        updated = self.context.rotate(self.adapter, self.client, self.worker, self.task, self.idle, threading.Event())
        self.assertEqual(self.client.transport.stream.call_count, 2)
        self.assertEqual(updated['thread_id'], 'old')
        self.assertIn('Keep Welcome chats', updated['handoff'])

    def test_unconfirmed_checkpoint_rate_limit_never_resends(self):
        from notiontool.engine import RateLimited
        self.limit()
        self.adapter.semantic_context = True
        self.adapter.body = Mock(return_value={'summary': True})
        self.adapter.recover = Mock(side_effect=RateLimited(10, 'busy'))
        self.context.archive_native = Mock()
        for _ in range(2):
            with self.assertRaises(RateLimited):
                self.context.rotate(self.adapter, self.client, self.worker, self.task, self.idle, threading.Event())
        summary = [t for t in self.store.all('tasks') if t.get('role') == 'context_compactor']
        self.assertGreater(summary[0]['sent_at'], 0)
        self.assertEqual(self.client.transport.stream.call_count, 1)  # Never send an unconfirmed summary twice.

    def test_native_archive_reads_all_saved_messages_in_batches(self):
        ids = [str(i) for i in range(205)]
        def call(path,body):
            requests = body['requests']
            table = requests[0]['pointer']['table']
            values = {'old':{'space_id':'space','messages':ids}} if table=='thread' else {
                r['pointer']['id']:{'step':{'type':'user','value':r['pointer']['id']}} for r in requests}
            return {'recordMap':{table:values}}
        self.client.call = Mock(side_effect=call)
        archived = self.context.archive_native(self.client,self.worker)
        self.assertEqual(len(archived['native_messages']),205)
        self.assertEqual(self.client.call.call_count,4)

    def test_restart_recovers_checkpoint_preparation_without_resending_assignment(self):
        from notiontool.engine import Engine
        task = dict(self.task, submission_phase='preparing', attempts=1)
        self.store.put('tasks',task)
        engine = Engine(self.store,SimpleNamespace())
        try:
            saved = self.store.get('tasks','t')
            self.assertEqual(saved['state'],'retry')
            self.assertEqual(saved['attempts'],0)
            self.assertEqual(self.store.get('workers',self.worker['id'])['state'],'ready')
        finally:
            engine.close()

    def test_restart_keeps_actual_inference_reserved_for_reconciliation(self):
        from notiontool.engine import Engine
        task = dict(self.task, submission_phase='inference', attempts=1, lease='lease')
        self.store.put('tasks',task)
        engine = Engine(self.store,SimpleNamespace())
        try:
            self.assertEqual(self.store.get('tasks','t')['state'],'uncertain')
            self.assertEqual(self.store.get('workers',self.worker['id'])['state'],'recovering')
        finally:
            engine.close()

    def test_refresh_preserves_logical_worker_and_excludes_archived_generations(self):
        from notiontool.notion import NotionAdapter
        import uuid
        old, new = str(uuid.uuid4()), str(uuid.uuid4())
        account = self.store.get('accounts','a'); account['email']='owner@example.test'
        self.store.put('accounts',account)
        self.store.put('workers',dict(self.worker,thread_id=new))
        self.store.put('thread_generations',dict(id=old,account_id='a'))
        adapter = NotionAdapter(self.store)
        adapter.client = lambda _:SimpleNamespace(identity=lambda:{'email':account['email']},
            model=lambda:'opus',threads=lambda:[{'id':old,'title':'Old chat'},{'id':new,'title':'Welcome to Notion'}])
        adapter.refresh('a')
        self.assertEqual(len(self.store.all('workers')),1)
        self.assertEqual(self.store.get('workers','logical-main')['thread_id'],new)
        self.assertEqual(self.store.get('workers','logical-main')['purpose'],'main')

    def test_message_page_keeps_older_messages_in_storage(self):
        for i in range(120):
            self.store.message('c','user',str(i))
        page=self.store.message_page('c')
        self.assertTrue(page['has_more'])
        self.assertEqual(len(page['messages']),100)
        self.assertEqual(page['messages'][0]['text'],'20')
        self.assertEqual(len(self.store.message_page('c',200)['messages']),120)

    def test_pending_checkpoint_counts_towards_account_capacity(self):
        from notiontool.engine import Engine
        account=self.store.get('accounts','a');account['state']='ready';self.store.put('accounts',account)
        self.store.put('workers',dict(self.worker,state='ready'))
        self.store.put('workers',dict(self.worker,id='busy',purpose='worker',state='busy'))
        self.store.put('workers',dict(self.worker,id='free',purpose='worker',state='ready'))
        self.store.put('tasks',dict(id='summary',state='checkpoint',sent_at=1))
        self.store.put('rotations',dict(id=self.worker['id'],task_id='t',summary_task_id='summary',state='reserved'))
        engine=Engine.__new__(Engine);engine.store=self.store;engine.adapter=SimpleNamespace()
        settings=self.store.settings();settings['per_account']=2
        run=dict(id='r',main_worker=self.worker['id'],capacity=2)
        self.assertIsNone(engine._choose_worker({'id':'other','role':'executor'},run,settings))
        self.assertEqual(engine._choose_worker({'id':'t','role':'planner'},run,settings)['id'],self.worker['id'])


class EdgeTests(unittest.TestCase):
    def transport(self):
        return EdgeTransport(SimpleNamespace(settings=lambda:{'task_timeout':900}),None,'a',Mock(),browser=Mock())

    def test_stream_reassembles_split_json_lines(self):
        transport = self.transport()
        transport._chunks = Mock(return_value=iter(['{"type":"pa', 'tch"}\n{"x":', '2}\n']))
        self.assertEqual([json.loads(x) for x in transport.stream('runInferenceTranscript',{})],
                         [{'type':'patch'}, {'x':2}])

    def test_invalid_or_empty_success_stream_stays_ambiguous(self):
        for chunks in (['<html>Notion login</html>'], [], ['{"type":']):
            transport = self.transport(); transport._chunks = Mock(return_value=iter(chunks))
            with self.assertRaises(NotionStreamError):
                list(transport.stream('runInferenceTranscript',{}))

    def test_rate_limit_status_is_returned_to_client_without_retry(self):
        transport = self.transport(); transport._chunks = Mock(side_effect=NotionHttpError(429))
        self.assertEqual(transport.request('loadUserContent',{})[0], 429)
        self.assertEqual(transport._chunks.call_count,1)

    def test_browser_fetch_removes_manual_cookie_and_origin_headers(self):
        transport = self.transport(); transport.prepare = Mock(return_value={'id':'owned'})
        transport._start('loadUserContent',{}, {'Cookie':'secret','Origin':'other','x-notion-space-id':'space'},30,None)
        expression = transport.browser.evaluate.call_args.args[1]
        self.assertNotIn('secret',expression)
        self.assertIn('space',expression)
        self.assertIn("credentials:'include'",expression)
        self.assertIn("redirect:'error'",expression)

    def test_foreign_endpoint_is_rejected_before_browser_or_request(self):
        transport = self.transport()
        with self.assertRaises(ValueError):
            transport._start('https://example.test',{}, {},30,None)
        transport.browser.ensure.assert_not_called()


if __name__ == '__main__':
    unittest.main()
