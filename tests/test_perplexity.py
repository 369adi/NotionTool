import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from notiontool import perplexity as pp
from notiontool.bridge import TOOLS


class PerplexityApiTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop('PPLX_API_KEY', None)
        os.environ.pop('PERPLEXITY_API_KEY', None)

    def test_tool_is_registered(self):
        self.assertIn('perplexity_search', TOOLS)
        self.assertIn('query', TOOLS['perplexity_search'][1])

    def test_api_key_from_settings_wins_over_env(self):
        os.environ['PPLX_API_KEY'] = 'env-key'
        self.assertEqual(pp.api_key({'perplexity_api_key': 'settings-key'}), 'settings-key')

    def test_api_key_from_env(self):
        os.environ['PPLX_API_KEY'] = 'env-key'
        self.assertEqual(pp.api_key({}), 'env-key')
        os.environ['PERPLEXITY_API_KEY'] = 'env2'
        self.assertEqual(pp.api_key({}), 'env-key')  # PPLX wins

    def test_api_key_empty_when_unset(self):
        self.assertEqual(pp.api_key({}), '')

    def test_search_posts_to_sonar_and_returns_answer(self):
        def fake_post(url, payload, key):
            self.assertEqual(url, pp.API_URL)
            self.assertEqual(key, 'abc')
            self.assertEqual(payload['model'], pp.DEFAULT_MODEL)
            self.assertEqual(payload['messages'][0]['content'], 'query text')
            return {'model': 'sonar', 'choices': [{'message': {'content': 'the answer'}}],
                    'citations': ['https://a', 'https://b']}

        result = pp.search('query text', 'abc', http_post=fake_post)
        self.assertEqual(result['answer'], 'the answer')
        self.assertEqual(result['citations'], ['https://a', 'https://b'])
        self.assertEqual(result['model'], 'sonar')

    def test_search_requires_non_empty_query(self):
        with self.assertRaises(ValueError):
            pp.search('   ', 'abc', http_post=lambda *a: {})


class BridgeKeyTests(unittest.TestCase):
    def test_bridge_reads_key_from_settings(self):
        store = SimpleNamespace(settings=lambda: {'perplexity_api_key': 'k1'})
        from notiontool.bridge import Bridge
        bridge = Bridge.__new__(Bridge)
        bridge.store = store
        self.assertEqual(bridge._perplexity_key(), 'k1')
