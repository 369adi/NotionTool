"""Eight realistic slots; capacity, fairness and degraded pool behavior."""
import threading
import unittest
import time
import test_engine as fixtures


class MeteredAdapter(fixtures.FakeAdapter):
    def __init__(self):
        super().__init__(count=40)
        self.by_account={}; self.account_peaks={}; self.execution=[]
        self.executing=0; self.execution_peak=0; self.wait_execution=True
    def run(self,worker,task,prompt,cancel,update):
        execution=task['role']=='executor'
        if execution:
            with self.lock:
                account=worker['account_id']
                self.by_account[account]=self.by_account.get(account,0)+1
                self.account_peaks[account]=max(self.account_peaks.get(account,0),self.by_account[account])
                self.executing+=1; self.execution_peak=max(self.execution_peak,self.executing)
                self.execution.append((task['run_id'],worker['id'],account,prompt))
            if self.wait_execution:self.gate.wait(12)
        try:return super().run(worker,task,prompt,cancel,update)
        finally:
            if execution:
                with self.lock:
                    self.by_account[worker['account_id']]-=1;self.executing-=1


class EightSlotTests(unittest.TestCase):
    worker=fixtures.EngineTests.worker
    pump=fixtures.EngineTests.pump
    tearDown=fixtures.EngineTests.tearDown
    def setUp(self):
        fixtures.EngineTests.setUp(self)
        self.adapter=MeteredAdapter();self.engine.adapter=self.adapter
        for slot in range(8):
            ident='a' if slot==0 else f'a{slot}'
            self.store.put('accounts',dict(id=ident,slot=slot+1,state='ready',cooldown_until=0))
            for n in range(slot*5,slot*5+5):
                worker=self.store.get('workers',f'w{n}');worker['account_id']=ident;self.store.put('workers',worker)
            if slot:
                self.worker(f'main{slot}','main')
                worker=self.store.get('workers',f'main{slot}');worker['account_id']=ident;self.store.put('workers',worker)
        settings=self.store.settings();settings['per_account']=5;self.store.put('settings',settings)
    def test_eight_slots_five_workers_each_forty_inflight(self):
        run=self.engine.submit('c','forty independent contributions')
        self.pump(lambda:self.adapter.executing==40,timeout=9)
        self.assertEqual(self.adapter.by_account,{('a' if n==0 else f'a{n}'):5 for n in range(8)})
        self.assertEqual(len({w for _,w,_,_ in self.adapter.execution}),40)
        self.assertFalse(any(self.store.get('workers',w)['purpose']=='main' for _,w,_,_ in self.adapter.execution))
        self.adapter.gate.set();self.pump(lambda:self.store.get('runs',run['id'])['state']=='done',timeout=9)
    def test_per_slot_cap_three_queues_remaining_work(self):
        settings=self.store.settings();settings['per_account']=3;self.store.put('settings',settings)
        run=self.engine.submit('c','capacity constrained')
        self.pump(lambda:self.adapter.executing==24,timeout=9)
        self.assertTrue(all(n==3 for n in self.adapter.by_account.values()))
        self.assertEqual(sum(t['state']=='queued' for t in self.engine.tasks(run['id'])),16)
        self.adapter.gate.set();self.pump(lambda:self.store.get('runs',run['id'])['state']=='done',timeout=9)
        self.assertTrue(all(n<=3 for n in self.adapter.account_peaks.values()))
    def test_two_main_chats_fairly_share_full_pool(self):
        self.store.put('chats',dict(id='c2',title='Second',main_worker='main1',folder_id='f2',project_path=self.temp.name))
        first=self.engine.submit('c','ALPHA_ONLY');second=self.engine.submit('c2','BETA_ONLY')
        self.pump(lambda:all(self.store.get('runs',r['id'])['state']=='executing' for r in (first,second)),timeout=9)
        self.pump(lambda:self.adapter.executing==40,timeout=9)
        # A second run may finish planning after the first fills the pool. Release
        # one batch; round-robin must allow the other run to progress thereafter.
        self.adapter.gate.set();self.pump(lambda:all(self.store.get('runs',r['id'])['state']=='done' for r in (first,second)),timeout=9)
        for ident,_,_,prompt in self.adapter.execution:
            self.assertIn('ALPHA_ONLY' if ident==first['id'] else 'BETA_ONLY',prompt)
            self.assertNotIn('BETA_ONLY' if ident==first['id'] else 'ALPHA_ONLY',prompt)
        self.assertLessEqual(self.adapter.execution_peak,40)
        self.assertEqual({m['chat_id'] for m in self.store.all('messages') if m['role']=='assistant'},{'c','c2'})
    def test_one_disabled_slot_does_not_stall_other_seven(self):
        for worker in self.store.all('workers'):
            if worker['account_id']=='a7':worker['enabled']=False;self.store.put('workers',worker)
        run=self.engine.submit('c','degraded pool')
        self.pump(lambda:self.adapter.executing==35,timeout=9)
        self.assertNotIn('a7',self.adapter.by_account)
        self.adapter.gate.set();self.pump(lambda:self.store.get('runs',run['id'])['state']=='done',timeout=9)
    def test_reviewer_fallback_respects_account_cooldown(self):
        run=self.engine.submit('c','review during cooldown')
        for worker in self.store.all('workers'):
            if worker['purpose']=='worker':
                task=self.engine.add_task(run,'executor','completed contribution')
                task.update(state='done',worker_id=worker['id']);self.store.put('tasks',task)
        account=self.store.get('accounts','a');account['cooldown_until']=time.time()+60;self.store.put('accounts',account)
        reviewer=self.engine.add_task(run,'reviewer','check results')
        self.assertIsNone(self.engine._choose_worker(reviewer,run,self.store.settings()))
    def test_recovering_worker_is_not_reused_and_pool_continues(self):
        worker=self.store.get('workers','w0');worker['state']='recovering';self.store.put('workers',worker)
        run=self.engine.submit('c','one unavailable uncertain thread')
        self.pump(lambda:self.adapter.executing==39,timeout=9)
        self.assertNotIn('w0',{w for _,w,_,_ in self.adapter.execution})
        self.adapter.gate.set();self.pump(lambda:self.store.get('runs',run['id'])['state']=='done',timeout=9)
    def test_global_limit_twelve_across_eight_slots(self):
        settings=self.store.settings();settings['max_workers']=12;self.store.put('settings',settings)
        run=self.engine.submit('c','global backpressure')
        self.pump(lambda:self.adapter.executing==12,timeout=9)
        self.assertEqual(sum(t['state']=='queued' for t in self.engine.tasks(run['id'])),28)
        self.adapter.gate.set();self.pump(lambda:self.store.get('runs',run['id'])['state']=='done',timeout=9)
        self.assertLessEqual(self.adapter.execution_peak,12)
    def test_signed_out_slot_is_excluded_even_with_cached_ready_workers(self):
        account=self.store.get('accounts','a7');account['state']='login';self.store.put('accounts',account)
        run=self.engine.submit('c','one expired account')
        self.assertEqual(run['capacity'],35)
        self.pump(lambda:self.adapter.executing==35,timeout=9)
        self.assertNotIn('a7',self.adapter.by_account)
        self.adapter.gate.set();self.pump(lambda:self.store.get('runs',run['id'])['state']=='done',timeout=9)

if __name__=='__main__':unittest.main()
