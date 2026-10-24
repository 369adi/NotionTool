import unittest
from notiontool.allocation import allocation, parallel_width, validate_allocation
from notiontool.protocol import ContractError
from notiontool.network_budget import NetworkBudget, retry_seconds


class AllocationTests(unittest.TestCase):
    def test_simple_message_does_not_fill_forty_workers(self):
        self.assertEqual(allocation('Hallo!',40)['mode'],'direct')
        # Requirement: a simple question is answered directly by the Main, no coworker.
        self.assertEqual(allocation('Calculate 17 * 23',40)['mode'],'direct')
        self.assertEqual(allocation('Calculate 17 * 23',40)['budget'],1)
        self.assertEqual(allocation('Calculate 17 * 23',40)['advisers'],0)

    def test_requested_independent_work_scales_to_pool(self):
        goal='Use exactly 40 independent contributions, one integer per contribution.'
        self.assertEqual(allocation(goal,15)['budget'],15)
        self.assertEqual(allocation(goal,40)['budget'],40)
        self.assertEqual(allocation('Use exactly ten independent calculations',40)['budget'],10)

    def test_dependencies_do_not_create_useless_concurrency(self):
        tasks=[dict(id='A',depends_on=[]),dict(id='B',depends_on=['A']),dict(id='C',depends_on=['B'])]
        self.assertEqual(parallel_width(tasks),1)
        self.assertEqual(parallel_width([dict(id=str(n),depends_on=[]) for n in range(40)]),40)

    def test_parallel_width_compares_ids_as_text_and_ignores_unknown(self):
        tasks=[dict(id=1,depends_on=[]),dict(id='2',depends_on=['1']),dict(id=3,depends_on=['zz'])]
        self.assertEqual(parallel_width(tasks),2)  # 1 and 3 start together; 2 waits for 1

    def test_t1_budget_one_coworker_per_independent_part(self):
        from notiontool.allocation import parts_budget
        self.assertEqual(parts_budget(4,4,5),4)   # 4 independent parts -> worker_budget 4
        self.assertEqual(parts_budget(4,4,3),3)   # at most the free slots
        self.assertEqual(parts_budget(3,1,5),2)   # serial plan with 2+ parts: review of A overlaps B
        self.assertEqual(parts_budget(1,1,5),1)   # only a real one-part request gets 1
        self.assertEqual(parts_budget(4,4,0),1)

    def test_normalize_deps_aliases_numbers_duplicates_and_self(self):
        from notiontool.allocation import normalize_deps
        tasks=[dict(id=1),dict(id='T1',deps='1'),dict(id=3,after=1,depends_on=['T1','T1']),dict(id='x',depends_on=['x','1'])]
        warnings=normalize_deps(tasks)
        self.assertEqual([t['id'] for t in tasks],['1','T1','3','x'])
        self.assertEqual([t['depends_on'] for t in tasks],[[],['1'],['T1','1'],['1']])
        self.assertFalse(any(k in t for t in tasks for k in ('deps','after')))
        self.assertTrue(any('3' in w and 'merged' in w for w in warnings),warnings)
        self.assertTrue(any('x' in w and 'itself' in w for w in warnings),warnings)
        from notiontool.protocol import validate_plan
        plan={'tasks':[dict(t,title='t',prompt='p',acceptance='a') for t in tasks]}
        validate_plan(plan)  # the cleaned plan passes; unknown ids/cycles are still validate_plan's job
        bad=[dict(id='a',deps='missing',title='t',prompt='p',acceptance='a')]
        normalize_deps(bad)
        with self.assertRaisesRegex(ContractError,'Unknown dependency'):validate_plan({'tasks':bad})

    def test_explicit_parallel_request_rejects_serial_plan_and_extra_review_task(self):
        policy=allocation('Use exactly 15 independent calculations',15)
        serial=[dict(id=str(n),depends_on=[str(n-1)] if n else []) for n in range(15)]
        with self.assertRaisesRegex(ContractError,'independent'):validate_allocation({'tasks':serial},policy)
        independent=[dict(id=str(n),depends_on=[]) for n in range(15)]
        validate_allocation({'tasks':independent},policy)
        with self.assertRaisesRegex(ContractError,'exactly 15'):validate_allocation({'tasks':independent+[dict(id='review',depends_on=[])]},policy)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.budget=NetworkBudget(clock=lambda:self.now,sleep=self.sleep,rng=lambda:0)
    def sleep(self,seconds):self.now+=seconds
    def test_all_accounts_share_start_pacing_but_streams_need_no_serial_completion(self):
        starts=[]
        for _ in range(40):
            self.budget.acquire();starts.append(self.now)
        self.assertEqual(starts[-1]-starts[0],9.75)
    def test_limit_stops_all_pending_accounts_and_respects_server_delay(self):
        self.budget.acquire()
        self.assertEqual(self.budget.limited('120'),120)
        self.budget.acquire()
        self.assertEqual(self.now,1120)
        self.assertEqual(self.budget.limited(None),60)
        self.budget.acquire()
        self.assertEqual(self.now,1180)
    def test_retry_after_http_date(self):
        self.assertEqual(retry_seconds('Thu, 01 Jan 1970 00:18:20 GMT',1000),100)
        self.assertEqual(retry_seconds('invalid',1000),0)

    def test_cancelled_wait_does_not_submit_later(self):
        import threading
        cancel=threading.Event();cancel.set()
        self.budget.limited('120')
        with self.assertRaisesRegex(ValueError,'cancelled'):
            self.budget.acquire(cancel)
        self.assertEqual(self.now,1000)

    def test_http_429_propagates_retry_after_and_blocks_other_slot(self):
        import io
        import urllib.error
        from notiontool.vendor.transport import NativeNotionTransport,NotionHttpError
        class Opener:
            def open(self,req,timeout):
                raise urllib.error.HTTPError(req.full_url,429,'Limited',{'Retry-After':'180'},io.BytesIO(b'{}'))
        first=NativeNotionTransport({},_opener=Opener(),budget=self.budget)
        status,_=first.request('getAvailableModels',{})
        self.assertEqual(status,429);self.assertEqual(first.retry_after,180)
        second=NativeNotionTransport({},_opener=Opener(),budget=self.budget)
        with self.assertRaises(NotionHttpError) as result:
            list(second.stream('runInferenceTranscript',{}))
        self.assertEqual(self.now,1180)
        self.assertEqual(result.exception.retry_after,180)
