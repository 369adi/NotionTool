import unittest
from notiontool.setup_status import account_setup


class SetupStatusTests(unittest.TestCase):
    def setUp(self):
        self.account={'id':'a','state':'ready','bridge_connected':True}
        self.workers=[dict(account_id='a',purpose='worker',welcome=True) for _ in range(5)]
        self.workers.append(dict(account_id='a',purpose='main',welcome=True))
    def test_six_chats_on_existing_plan_are_complete(self):
        result=account_setup(self.account,self.workers)
        self.assertTrue(result['chat_ready']);self.assertTrue(result['complete'])
        self.assertEqual(result['stage'],'complete')
    def test_mcp_must_be_actually_connected(self):
        self.account['bridge_connected']=False
        result=account_setup(self.account,self.workers)
        self.assertFalse(result['complete']);self.assertEqual(result['stage'],'mcp_required')
    def test_other_accounts_workers_cannot_complete_missing_team(self):
        self.workers[0]['account_id']='other'
        result=account_setup(self.account,self.workers)
        self.assertFalse(result['complete']);self.assertEqual(result['stage'],'chats_required')
    def test_only_authenticated_team_and_mcp_are_complete(self):
        self.assertTrue(account_setup(self.account,self.workers)['complete'])
        self.account['state']='login'
        self.assertFalse(account_setup(self.account,self.workers)['complete'])

    def test_server_ai_disable_blocks_setup(self):
        self.account['ai_access']={'state':'disabled'}
        result=account_setup(self.account,self.workers)
        self.assertFalse(result['chat_ready'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['stage'],'ai_disabled')

if __name__=='__main__': unittest.main()
