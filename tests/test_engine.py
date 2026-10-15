import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from notiontool.engine import Engine, PendingRemote, RateLimited
from notiontool.protocol import validate_plan, ContractError
from notiontool.store import Store
from notiontool.bridge import Bridge


class FakeAdapter:
    def __init__(self, count=4, hold=False):
        self.count,self.hold = count,hold
        self.gate = threading.Event()
        self.roles=[]
        self.active,self.peak=0,0
        self.lock=threading.Lock()
        self.ambiguous=False
        self.recoveries=0
        self.review_passed=True
        self.bad_json=False
    def run(self,worker,task,prompt,cancel,update):
        with self.lock:
            self.roles.append((task['run_id'],task['role']))
            self.active+=1
            self.peak=max(self.peak,self.active)
        try:
            if self.ambiguous and task['role']=='executor':
                raise PendingRemote()
            if self.hold and task['role']=='executor':
                self.gate.wait(12)
            time.sleep(0.01)
            if self.bad_json:
                return 'cowork_join please'
            if task['role']=='planner':
                return json.dumps({'summary':'parallel plan','tasks':[dict(id=f'T{i}',title=f'Part {i}',role='executor',
                    prompt=f'Execute part {i}',depends_on=[],files=[],acceptance='Verified outcome') for i in range(self.count)]})
            if task['role']=='reviewer':
                return json.dumps({'passed':self.review_passed,'summary':'independently checked','fixes':[] if self.review_passed else [
                    dict(id='F1',title='repair',prompt='repair it',depends_on=[],files=[],acceptance='fixed') ]})
            return json.dumps(dict(status='done',summary='Verified answer',evidence=['actual fake check'],context='durable finding'))
        finally:
            with self.lock:self.active-=1
    def recover(self,worker,task,cancel):
        self.recoveries+=1
        if self.gate.is_set():
            return json.dumps(dict(status='done',summary='Recovered answer',evidence=[]))
        raise PendingRemote()


class EngineTests(unittest.TestCase):
    def test_wrong_model_cannot_use_tools_before_final_response_check(self):
        from unittest.mock import Mock
        from notiontool.model_verification import ModelVerificationError
        self.store.put('runs',{'id':'model-tool','chat_id':'c','state':'executing','project_path':''})
        self.store.put('tasks',{'id':'model-task','run_id':'model-tool','state':'running','role':'executor','lease':'capability','worker_id':'main'})
        self.adapter.verify_response_model=Mock(side_effect=ModelVerificationError('opus-id',['sonnet-low']))
        with self.assertRaises(ModelVerificationError):
            Bridge(self.store,self.engine).call('memory',{'text':'Must not persist'},'capability')
        self.assertIsNone(self.store.get('memory','c'))
    def test_model_mismatch_blocks_without_retry_or_final_answer(self):
        from notiontool.model_verification import ModelVerificationError
        self.adapter.run=lambda *args:(_ for _ in ()).throw(ModelVerificationError('opus-id',['sonnet-low']))
        run=self.engine.submit('c','Hallo!')
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='blocked')
        task=self.engine.tasks(run['id'])[0]
        self.assertEqual(task['attempts'],1)
        self.assertEqual(task['model_verification'],'mismatch')
        self.assertEqual(task['reported_model_ids'],['sonnet-low'])
        self.assertFalse(any(m['role']=='assistant' for m in self.store.all('messages')))
    def test_restart_preserves_overdue_unknown_assignment_reservation(self):
        self.store.put('tasks',{'id':'pending-old','run_id':'old-run','state':'blocked',
                       'remote_unknown':True,'worker_id':'main','lease':'preserved-lease'})
        self.engine._restore()
        worker=self.store.get('workers','main')
        self.assertEqual(worker['state'],'recovering')
        self.assertEqual(worker['lease'],'preserved-lease')
    def test_full_pc_path_lock_conflicts_with_other_chat_project_lock(self):
        target=Path(self.temp.name)/'shared.py'
        for ident in ('r1','r2'):
            self.store.put('runs',{'id':ident,'project_path':self.temp.name})
        self.store.put('tasks',{'id':'a1','run_id':'r1','state':'running','lease':'one','files':[]})
        self.store.put('tasks',{'id':'a2','run_id':'r2','state':'running','lease':'two','files':[]})
        self.engine.claim_absolute('a1','one',target)
        with self.assertRaisesRegex(ValueError,'owns this file'):
            self.engine.claim_file('a2','two','shared.py')

    def setUp(self):
        root=Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.store=Store(self.temp.name)
        self.adapter=FakeAdapter()
        self.store.put('accounts',dict(id='a',slot=1,email='test@example.test',state='ready',cooldown_until=0))
        self.worker('main','main')
        for n in range(40):self.worker(f'w{n}')
        self.store.put('chats',dict(id='c',title='Main',main_worker='main',folder_id='f',project_path=self.temp.name))
        settings=self.store.settings();settings.update(max_workers=40,per_account=40);self.store.put('settings',settings)
        self.engine=Engine(self.store,self.adapter)
    def worker(self,ident,purpose='worker'):
        self.store.put('workers',dict(id=ident,account_id='a',thread_id=ident,purpose=purpose,enabled=True,state='ready',lease='',label=ident,last_used=0))
    def tearDown(self):
        self.adapter.gate.set();self.engine.close()
        time.sleep(.03)
        self.store.close();self.temp.cleanup()
    def pump(self,predicate,timeout=4):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.engine.tick()
            if predicate():return
            time.sleep(.015)
        self.fail('Coordinator did not reach expected state: '+str([(r['state'],r['error']) for r in self.store.all('runs')]))
    def test_full_harness_runs_plan_execute_review_integrate(self):
        run=self.engine.submit('c','Build and verify')
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='done')
        roles=[role for _,role in self.adapter.roles]
        self.assertLess(roles.index('planner'),roles.index('executor'))
        self.assertLess(roles.index('reviewer'),roles.index('integrator'))
        self.assertEqual(self.store.all('messages')[-1]['text'],'Verified answer')
    def test_greeting_uses_only_reserved_main(self):
        run=self.engine.submit('c','Hallo!')
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='done')
        self.assertEqual([role for _,role in self.adapter.roles],['integrator'])
        self.assertEqual(self.engine.tasks(run['id'])[0]['worker_id'],'main')

    def test_architect_routes_each_delegation_and_can_work_alone(self):
        self.adapter.task_model_routing=True
        original=self.adapter.run
        def routed(worker,task,prompt,cancel,update):
            if task['role']=='planner':
                solo='Work alone' in prompt
                models=['opus'] if solo else ['haiku','sonnet','opus']
                return json.dumps({'summary':'Architect choice','tasks':[
                    dict(id=f'T{i}',title=f'Part {i}',prompt='Specific work',depends_on=[],files=[],
                         acceptance='Checked',model=model,model_reason='Chosen for task difficulty',use_main=solo)
                    for i,model in enumerate(models)]})
            return original(worker,task,prompt,cancel,update)
        self.adapter.run=routed
        run=self.engine.submit('c','Delegate independent work')
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='done')
        tasks=self.engine.tasks(run['id'])
        self.assertEqual(next(t for t in tasks if t['role']=='planner')['worker_id'],'main')
        self.assertEqual({t['model'] for t in tasks if t['role']=='executor'},{'haiku','sonnet','opus'})
        self.assertFalse(any(t['role'] in ('architect','researcher','quality_planner') for t in tasks))
        run=self.engine.submit('c','Work alone')
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='done')
        tasks=self.engine.tasks(run['id'])
        self.assertTrue(all(t['worker_id']=='main' and t['model']=='opus' for t in tasks))
        self.assertFalse(any(t['role']=='reviewer' for t in tasks))

    def test_review_and_final_context_preserve_results_without_speculative_advice(self):
        captured={}; original=self.adapter.run
        def call(worker,task,prompt,cancel,update):
            captured[task['role']]=prompt
            if task['role'] in ('architect','researcher','quality_planner'):
                return json.dumps(dict(status='done',summary='SPECULATIVE_ADVICE',context=''))
            if task['role']=='executor':
                return json.dumps(dict(status='done',summary='ACTUAL_DELIVERABLE',context=''))
            return original(worker,task,prompt,cancel,update)
        self.adapter.run=call
        run=self.engine.submit('c','Build and verify')
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='done')
        self.assertIn('SPECULATIVE_ADVICE',captured['planner'])
        for role in ('reviewer','integrator'):
            self.assertIn('ACTUAL_DELIVERABLE',captured[role])
            self.assertNotIn('SPECULATIVE_ADVICE',captured[role])
        self.assertIn('independently checked',captured['integrator'])

    def test_small_question_is_answered_directly_by_main(self):
        run=self.engine.submit('c','Calculate 17 * 23')
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='done')
        roles=[role for _,role in self.adapter.roles]
        self.assertEqual(roles,['integrator'])
        self.assertNotIn('executor',roles)

    def test_provider_error_in_final_answer_blocks_without_discarding_work(self):
        original=self.adapter.run
        def run(worker,task,prompt,cancel,update):
            if task['role']=='integrator':return json.dumps({'status':'done','summary':'Something went wrong. Please try again later.'})
            return original(worker,task,prompt,cancel,update)
        self.adapter.run=run
        run=self.engine.submit('c','finish carefully')
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='blocked')
        tasks=self.engine.tasks(run['id'])
        final=next(t for t in tasks if t['role']=='integrator')
        self.assertEqual(final['attempts'],1)
        self.assertTrue(all(t['state']=='done' for t in tasks if t['role']=='executor'))
        self.assertFalse(any(m['role']=='assistant' for m in self.store.all('messages')))
    def test_verified_tool_free_final_failure_retries_without_reexecuting_work(self):
        from notiontool.notion import VerifiedReply
        original=self.adapter.run;final_calls=[]
        def call(worker,task,prompt,cancel,update):
            if task['role']=='integrator':
                final_calls.append(1)
                if len(final_calls)==1:return VerifiedReply(json.dumps({'status':'done','summary':'Something went wrong. Please try again later.'}),False)
            return original(worker,task,prompt,cancel,update)
        self.adapter.run=call;run=self.engine.submit('c','safe final retry')
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='done',timeout=8)
        self.assertEqual(len(final_calls),2)
        self.assertTrue(all(t['attempts']==1 for t in self.engine.tasks(run['id']) if t['role']=='executor'))
    def test_verified_final_failure_with_tool_activity_is_not_repeated(self):
        from notiontool.notion import VerifiedReply
        original=self.adapter.run
        def call(worker,task,prompt,cancel,update):
            if task['role']=='integrator':return VerifiedReply(json.dumps({'status':'done','summary':'Something went wrong. Please try again later.'}),True)
            return original(worker,task,prompt,cancel,update)
        self.adapter.run=call;run=self.engine.submit('c','do not repeat side effects')
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='blocked')
        self.assertEqual(next(t for t in self.engine.tasks(run['id']) if t['role']=='integrator')['attempts'],1)
    def test_verified_final_retry_is_bounded_when_provider_stays_unavailable(self):
        from notiontool.notion import VerifiedReply
        original=self.adapter.run
        def call(worker,task,prompt,cancel,update):
            if task['role']=='integrator':return VerifiedReply(json.dumps({'status':'done','summary':'Something went wrong. Please try again later.'}),False)
            return original(worker,task,prompt,cancel,update)
        self.adapter.run=call;run=self.engine.submit('c','bounded final outage')
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='blocked',timeout=10)
        self.assertEqual(next(t for t in self.engine.tasks(run['id']) if t['role']=='integrator')['attempts'],3)
        self.assertFalse(any(m['role']=='assistant' for m in self.store.all('messages')))
    def test_verified_tool_free_execution_failure_recovers_without_repeating_other_parts(self):
        from notiontool.notion import VerifiedReply
        original=self.adapter.run;failed=[]
        def call(worker,task,prompt,cancel,update):
            if task['role']=='executor' and not failed:
                failed.append(task['id']);return VerifiedReply(json.dumps({'status':'done','summary':'Something went wrong. Please try again later.'}),False)
            return original(worker,task,prompt,cancel,update)
        self.adapter.run=call;run=self.engine.submit('c','recover one pure calculation')
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='done',timeout=8)
        execution=[t for t in self.engine.tasks(run['id']) if t['role']=='executor']
        self.assertEqual(sum(t['attempts'] for t in execution),len(execution)+1)
    def test_forty_concurrent_api_workers(self):
        self.adapter.count=40;self.adapter.hold=True
        run=self.engine.submit('c','40 independent parts')
        self.pump(lambda:self.adapter.peak==40,timeout=9)
        self.assertEqual(sum(w['state']=='busy' for w in self.store.all('workers')),40)
        self.adapter.gate.set()
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='done')
    def test_account_capacity_is_respected(self):
        settings=self.store.settings();settings['per_account']=2;self.store.put('settings',settings)
        self.adapter.count=10;self.adapter.hold=True
        self.engine.submit('c','parts')
        self.pump(lambda:self.adapter.active==2 and any(role=='executor' for _,role in self.adapter.roles))
        for _ in range(4):self.engine.tick()
        self.assertLessEqual(self.adapter.peak,2)
    def test_multiple_main_chats_share_pool_without_crossed_results(self):
        self.worker('main2','main')
        self.store.put('chats',dict(id='c2',title='Second',main_worker='main2',folder_id='f2',project_path=self.temp.name))
        first=self.engine.submit('c','First goal');second=self.engine.submit('c2','Second goal')
        self.pump(lambda:all(self.store.get('runs',r['id'])['state']=='done' for r in [first,second]))
        self.assertEqual({m['chat_id'] for m in self.store.all('messages') if m['role']=='assistant'},{'c','c2'})
    def test_same_chat_queues_next_message(self):
        self.adapter.hold=True
        first=self.engine.submit('c','first');second=self.engine.submit('c','second')
        self.assertEqual(second['state'],'queued')
        self.pump(lambda:any(role=='executor' for _,role in self.adapter.roles))
        self.assertEqual(self.store.get('runs',second['id'])['state'],'queued')
        self.adapter.gate.set()
        self.pump(lambda:self.store.get('runs',second['id'])['state']=='done')
    def test_ambiguous_submit_is_not_resent(self):
        self.adapter.count=1;self.adapter.ambiguous=True
        run=self.engine.submit('c','one part')
        self.pump(lambda:any(t['state']=='uncertain' for t in self.engine.tasks(run['id'])))
        task=next(t for t in self.engine.tasks(run['id']) if t['state']=='uncertain')
        original_calls=len([r for _,r in self.adapter.roles if r=='executor'])
        task['reconcile_at']=0;self.store.put('tasks',task)
        self.adapter.gate.set()
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='done')
        self.assertEqual(len([r for _,r in self.adapter.roles if r=='executor']),original_calls)
        self.assertGreaterEqual(self.adapter.recoveries,1)
    def test_join_loop_is_bounded(self):
        self.adapter.bad_json=True
        run=self.engine.submit('c','no joins')
        def fast_retry():
            for t in self.engine.tasks(run['id']):
                if t['state']=='retry':t['next_at']=0;self.store.put('tasks',t)
            return self.store.get('runs',run['id'])['state']=='blocked'
        self.pump(fast_retry)
        self.assertTrue(all(t['attempts']<=3 for t in self.engine.tasks(run['id'])))
        self.assertFalse(any(m['role']=='assistant' for m in self.store.all('messages')))
    def test_failed_review_never_marks_done(self):
        self.adapter.review_passed=False
        run=self.engine.submit('c','verify carefully')
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='blocked')
        self.assertEqual(self.store.get('runs',run['id'])['fixes'],2)
        self.assertFalse(any(m['role']=='assistant' for m in self.store.all('messages')))
    def test_cancel_holds_ambiguous_thread(self):
        self.adapter.ambiguous=True;self.adapter.count=1
        run=self.engine.submit('c','work')
        self.pump(lambda:any(t['state']=='uncertain' for t in self.engine.tasks(run['id'])))
        self.engine.cancel(run['id'])
        self.assertTrue(any(w['state']=='recovering' for w in self.store.all('workers')))
        self.assertEqual(self.store.get('runs',run['id'])['state'],'cancelled')
    def test_nested_file_ownership_conflict(self):
        run=self.engine.submit('c','files')
        a=self.engine.add_task(run,'executor','a',files=['src'])
        a.update(state='running',lease='a');self.store.put('tasks',a)
        b=self.engine.add_task(run,'executor','b',files=['src/main.py'])
        self.assertTrue(self.engine._conflicts(b,run))
        b['files']=['tests/main.py'];self.assertFalse(self.engine._conflicts(b,run))
    def test_outside_project_rejected(self):
        run=self.engine.submit('c','files')
        with self.assertRaises(ContractError):self.engine._resource_keys(run,['../outside.txt'])
    def test_stale_attempt_report_cannot_complete_new_assignment(self):
        run=self.engine.submit('c','test')
        task=self.engine.tasks(run['id'])[0];task.update(state='running',lease='new');self.store.put('tasks',task)
        from concurrent.futures import Future
        future=Future();future.set_result('{"status":"done","summary":"stale"}')
        self.engine._complete(future,dict(task_id=task['id'],lease='old'))
        self.assertEqual(self.store.get('tasks',task['id'])['state'],'running')
    def test_restart_recovers_instead_of_resending(self):
        run=self.engine.submit('c','recover')
        task=self.engine.tasks(run['id'])[0];task.update(state='running',lease='saved',worker_id='w0');self.store.put('tasks',task)
        self.engine._restore()
        self.assertEqual(self.store.get('tasks',task['id'])['state'],'uncertain')
        self.assertEqual(self.store.get('workers','w0')['state'],'recovering')
    def test_verified_reset_fallback_resumes_parked_run(self):
        from concurrent.futures import Future
        from notiontool.engine import ResetNotAccepted
        self.adapter.count=1
        run=self.engine.submit('c','fallback')
        task=self.engine.tasks(run['id'])[0]
        task.update(state='blocked',remote_unknown=True,lease='saved',worker_id='main',started=time.time()-2000,sent_at=time.time()-1900,attempts=1)
        self.store.put('tasks',task)
        parked=self.store.get('runs',run['id']);parked.update(blocked_stage=parked['state'],state='blocked',error='unconfirmed')
        self.store.put('runs',parked)
        future=Future();future.set_exception(ResetNotAccepted())
        self.engine._complete(future,dict(task_id=task['id'],lease='saved',worker_id='main'))
        task=self.store.get('tasks',task['id'])
        self.assertEqual((task['state'],task['attempts'],task.get('remote_unknown')),('retry',0,None))
        self.assertNotEqual(self.store.get('runs',run['id'])['state'],'blocked')
        self.assertEqual(self.store.get('runs',run['id'])['error'],'')
        self.assertEqual(self.store.get('workers','main')['context_mode'],'soft')
        task['next_at']=0;self.store.put('tasks',task)
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='done')
    def test_confirmed_remote_error_retries_and_resumes_parked_run(self):
        from concurrent.futures import Future
        self.adapter.count=1
        run=self.engine.submit('c','remote error')
        task=self.engine.tasks(run['id'])[0]
        task.update(state='blocked',remote_unknown=True,lease='saved',worker_id='main',started=time.time()-2000,sent_at=time.time()-1900,attempts=1)
        self.store.put('tasks',task)
        parked=self.store.get('runs',run['id']);parked.update(blocked_stage=parked['state'],state='blocked',error='unconfirmed')
        self.store.put('runs',parked)
        future=Future();future.set_exception(RateLimited(1,'Notion ended this turn with an error',confirmed=True))
        self.engine._complete(future,dict(task_id=task['id'],lease='saved',worker_id='main'))
        task=self.store.get('tasks',task['id'])
        self.assertEqual((task['state'],task.get('remote_unknown')),('retry',None))
        self.assertIn('ended this turn',task['error'])
        self.assertNotEqual(self.store.get('runs',run['id'])['state'],'blocked')
        task['next_at']=0;self.store.put('tasks',task)
        account=self.store.get('accounts','a');account['cooldown_until']=0;self.store.put('accounts',account)
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='done')
    def test_waiting_for_busy_chat_does_not_use_attempts(self):
        from concurrent.futures import Future
        run=self.engine.submit('c','busy chat')
        task=self.engine.tasks(run['id'])[0]
        limit=self.store.settings()['max_attempts']
        task.update(state='running',lease='saved',worker_id='main',attempts=limit)
        self.store.put('tasks',task)
        future=Future();future.set_exception(RateLimited(1,'busy',free=True))
        self.engine._complete(future,dict(task_id=task['id'],lease='saved',worker_id='main'))
        task=self.store.get('tasks',task['id'])
        self.assertEqual((task['state'],task['attempts'],task['error']),('retry',limit-1,'busy'))
    def test_recovered_reply_resumes_parked_run(self):
        self.adapter.ambiguous=True;self.adapter.count=1
        run=self.engine.submit('c','work')
        self.pump(lambda:any(t['state']=='uncertain' for t in self.engine.tasks(run['id'])))
        task=next(t for t in self.engine.tasks(run['id']) if t['state']=='uncertain')
        task.update(state='blocked',remote_unknown=True,reconcile_at=0);self.store.put('tasks',task)
        parked=self.store.get('runs',run['id']);parked.update(blocked_stage=parked['state'],state='blocked',error=task['error'])
        self.store.put('runs',parked)
        self.adapter.gate.set()
        self.pump(lambda:self.store.get('runs',run['id'])['state']=='done')
        self.assertEqual(self.store.get('tasks',task['id'])['state'],'done')
    def test_expired_bridge_assignment_cannot_write(self):
        bridge=Bridge(self.store,self.engine)
        with self.assertRaises(ValueError):bridge.call('write_file',{'path':'x','text':'x'},'expired')
        self.assertFalse((Path(self.temp.name)/'x').exists())
    def test_full_reports_remain_available_for_context(self):
        run=self.engine.submit('c','context')
        task=self.engine.tasks(run['id'])[0];task.update(state='running',lease='secret',result='x'*70000);self.store.put('tasks',task)
        bridge=Bridge(self.store,self.engine)
        value=bridge.call('context',{},'secret')
        if 'text' in value:
            chunks=[value['text']]
            while value.get('next_offset') is not None:
                value=bridge.call('context',{'offset':value['next_offset']},'secret')
                chunks.append(value['text'])
            value=json.loads(''.join(chunks))
        self.assertEqual(len(value['reports'][0]['result']),70000)
    def test_plan_cycle_and_missing_dependency_rejected(self):
        def part(ident,deps):return dict(id=ident,title='title',prompt='prompt',acceptance='check',depends_on=deps,files=[])
        with self.assertRaises(ContractError):validate_plan({'tasks':[part('a',['b']),part('b',['a'])]})
        with self.assertRaises(ContractError):validate_plan({'tasks':[part('a',['missing'])]})


if __name__=='__main__':unittest.main()
