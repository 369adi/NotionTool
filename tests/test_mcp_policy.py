from types import SimpleNamespace
import unittest
from notiontool.mcp_policy import ensure_mcp_allowed


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.space={'settings':{'allowed_mcp_server_urls':['https://existing.example/mcp']}}
        self.info={'space_id':'s','user_id':'u','records':{'space':{'s':self.space},'space_user':{
            'member':{'space_id':'s','user_id':'u','membership_type':'owner'}}}}
        self.tier='business';self.writes=[]
        self.client=SimpleNamespace(info=self.info,identity=lambda:self.info,
            call=lambda *args:{'subscriptionTier':self.tier})
        def transaction(client,label,ops):
            self.writes.extend(ops)
            for op in ops:self.space['settings'][op['path'][1]]=op['args']
        self.adapter=SimpleNamespace(transaction=transaction)
    def test_only_own_url_added_and_existing_allowlist_preserved(self):
        self.assertTrue(ensure_mcp_allowed(self.adapter,self.client,'https://own.example/mcp'))
        self.assertEqual(self.space['settings']['allowed_mcp_server_urls'],['https://existing.example/mcp','https://own.example/mcp'])
        self.assertTrue(self.space['settings']['restrict_mcp_urls_to_allowlist'])
        self.assertFalse(self.space['settings']['disallow_custom_mcp_servers'])
        self.assertFalse(ensure_mcp_allowed(self.adapter,self.client,'https://own.example/mcp'))
        self.assertEqual(len(self.writes),3)
    def test_plan_is_not_checked_locally(self):
        self.tier='free'
        self.assertTrue(ensure_mcp_allowed(self.adapter,self.client,'https://own.example/mcp'))
        self.assertEqual(len(self.writes),3)
    def test_nonowner_and_organization_cannot_be_overridden(self):
        self.info['records']['space_user']['member']['membership_type']='member'
        with self.assertRaisesRegex(ValueError,'owner'):ensure_mcp_allowed(self.adapter,self.client,'https://own.example/mcp')
        self.info['records']['space_user']['member']['membership_type']='owner';self.space['organization_id']='organization'
        with self.assertRaisesRegex(ValueError,'administrator'):ensure_mcp_allowed(self.adapter,self.client,'https://own.example/mcp')
        self.assertEqual(self.writes,[])
    def test_existing_unrestricted_permission_is_left_unchanged(self):
        self.space['settings']['disallow_custom_mcp_servers']=False
        self.assertFalse(ensure_mcp_allowed(self.adapter,self.client,'https://own.example/mcp'))
        self.assertEqual(self.writes,[])

if __name__=='__main__':unittest.main()
