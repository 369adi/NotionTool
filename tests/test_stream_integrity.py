import io
import json
import unittest

from notiontool.vendor.transport import NativeNotionTransport, NotionStreamError


class StreamIntegrityTests(unittest.TestCase):
    def transport(self, content):
        response = io.BytesIO(content)
        class Opener:
            def open(self, request, timeout):
                return response
        return NativeNotionTransport({}, _opener=Opener()), response

    def test_truncated_array_is_not_a_successful_event(self):
        transport, response = self.transport(b'[')
        with self.assertRaises(NotionStreamError):
            list(transport.stream('runInferenceTranscript', {}))
        self.assertTrue(response.closed)

    def test_empty_http_success_is_unconfirmed(self):
        transport, response = self.transport(b'\n\n')
        with self.assertRaises(NotionStreamError):
            list(transport.stream('runInferenceTranscript', {}))
        self.assertTrue(response.closed)

    def test_valid_frames_are_preserved(self):
        event = {'type': 'patch-start', 'data': {'s': []}}
        transport, _ = self.transport((json.dumps(event) + '\n').encode())
        self.assertEqual([json.loads(line) for line in transport.stream('runInferenceTranscript', {})], [event])

    def test_partial_stream_then_truncation_is_not_silently_completed(self):
        transport, response = self.transport(b'{"type":"patch-start","data":{"s":[]}}\n[')
        events = transport.stream('runInferenceTranscript', {})
        self.assertEqual(json.loads(next(events))['type'], 'patch-start')
        with self.assertRaises(NotionStreamError):
            next(events)
        self.assertTrue(response.closed)

    def test_raw_response_never_appears_in_error(self):
        transport, _ = self.transport(b'private-response-content')
        with self.assertRaises(NotionStreamError) as failure:
            list(transport.stream('runInferenceTranscript', {}))
        self.assertNotIn('private-response-content', str(failure.exception))

    def test_non_object_frame_is_rejected(self):
        transport, _ = self.transport(b'[]\n')
        with self.assertRaises(NotionStreamError):
            list(transport.stream('runInferenceTranscript', {}))
