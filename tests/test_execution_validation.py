from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from notiontool.store import Store
from notiontool.notion import NotionAdapter
from notiontool.model_verification import ModelVerificationError


class ExecutionValidationTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).parent/'.test-data';base.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base);self.store=Store(self.temp.name)
        self.adapter=NotionAdapter(self.store)
        self.task={'id':'t','role':'planner','step_id':'own','assigned_model_id':'opus-id','before_seq':0}
        self.store.put('tasks',self.task)
        self.worker={'account_id':'a','thread_id':'thread'}
        self.model='sonnet-low'
        def call(path,body):
            self.assertEqual(path,'syncRecordValuesSpaceInitial')
            table=body['requests'][0]['pointer']['table']
            if table=='thread':
                return {'recordMap':{'thread':{'thread':{'value':{'space_id':'space','alive':True,'messages':['cfg','own','reply']}}}}}
            step={'type':'agent-inference','id':'reply','value':[{'notionModelName':self.model}]}
            if self.model is not None:step['model']=self.model
            return {'recordMap':{'thread_message':{
                'cfg':{'value':{'step':{'type':'config','value':{'model':'opus-id','modelFromUser':True}}}},
                'own':{'value':{'step':{'type':'user','id':'own'}}},'reply':{'value':{'step':step}}}}}
        self.client=SimpleNamespace(info={'space_id':'space'},call=call,transcript=lambda _: {
            'session':{'status':'idle'},'patches':[
                {'op':'put','entity':{'kind':'user_message','sequence':1,'client_message_id':'own'}},
                {'op':'put','entity':{'kind':'assistant_message','sequence':2,'content':[{'type':'text','text':'The answer is 837.'}]}}]})
        self.adapter.client=lambda _:self.client

    def tearDown(self):self.store.close();self.temp.cleanup()

    def test_other_reported_model_is_a_visible_non_blocking_fallback(self):
        self.adapter.recover(self.worker,self.task,threading.Event())
        saved=self.store.get('tasks','t')
        self.assertEqual(saved['model_verification'],'fallback')
        self.assertEqual(saved['reported_model_ids'],['sonnet-low'])

    def test_correct_actual_opus_response_is_accepted_and_recorded(self):
        self.model='opus-id'
        result=self.adapter.recover(self.worker,self.task,threading.Event())
        self.assertEqual(result,'The answer is 837.')
        self.assertEqual(self.store.get('tasks','t')['model_verification'],'verified')

    def test_missing_execution_model_stays_unverified(self):
        self.model=None
        with self.assertRaises(ModelVerificationError) as caught:self.adapter.verify_response_model(self.worker,self.task)
        self.assertEqual(caught.exception.reason,'missing')
