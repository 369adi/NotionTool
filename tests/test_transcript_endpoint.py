"""transcript() reads the committed transcript; REMOTE_UNKNOWN keeps its cause. Fakes only, no network."""
import inspect
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_wiring_server import client  # noqa: E402
from notiontool import notion  # noqa: E402


class TranscriptEndpointTests(unittest.TestCase):
    def test_transcript_calls_get_thread_transcript(self):
        c = client(200, '{"patches": []}')
        self.assertEqual(c.transcript('t1', limit=7), {'patches': []})
        path, body = c.transport.calls[-1]
        if isinstance(body, (str, bytes)):
            body = json.loads(body)
        self.assertTrue(str(path).endswith('getThreadTranscript'))
        self.assertEqual((body['threadId'], body['direction'], body['limit']), ('t1', 'backward', 7))

    def test_transcript_errors_are_not_swallowed(self):
        with self.assertRaises(ValueError):
            client(400, 'Transcript debugging is not enabled').transcript('t1')

    def test_debug_endpoint_is_not_used_for_transcripts(self):
        source = inspect.getsource(notion.NotionClient.transcript)
        self.assertNotIn("self.call('getInferenceTranscriptInferences'", source)
        self.assertIn("self.call('getThreadTranscript'", source)
        self.assertIn('getThreadTranscript', notion.THREAD_PATHS)

    def test_remote_unknown_keeps_the_cause(self):
        source = inspect.getsource(notion)
        self.assertIn("remote_done('unknown', 'REMOTE_UNKNOWN', f'{type(exc).__name__}: {exc}')", source)
        self.assertIn("extra = dict(detail=str(detail)[:200]) if detail else {}", source)


if __name__ == '__main__':
    unittest.main()
