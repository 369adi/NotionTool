"""Round 30: a pick in Notion's menu is stored even when the chat had no stored pick yet."""
import json
from pathlib import Path

from notiontool.notion_shell import choose_effort, pin_config_body

SRC = (Path(__file__).resolve().parent.parent / 'notiontool' / 'notion_shell.py').read_text(encoding='utf-8')
FAKE_THREAD = 'aaaa0000bbbb4ccc8ddd000000000030'


def body(effort, thread=FAKE_THREAD):
    steps = [{'type': 'config', 'value': {'model': 'albuquerque-quinn'}},
             {'type': 'updated-config', 'value': {'reasoningEffort': effort} if effort else {}},
             {'type': 'user', 'value': [['hi']]}]
    return json.dumps({'threadId': thread, 'transcript': steps})


def test_fresh_pick_without_record_is_stored():
    assert choose_effort('xhigh', '') == ('xhigh', True)
    assert choose_effort('max', None) == ('max', True)


def test_medium_without_record_is_no_pick():
    assert choose_effort('medium', '') == ('medium', False)
    assert choose_effort('', '') == ('', False)


def test_stored_pick_beats_notions_medium_reset():
    assert choose_effort('medium', 'xhigh') == ('xhigh', False)
    out = json.loads(pin_config_body(body('medium'), {}, picks={FAKE_THREAD: 'xhigh'}))
    assert out['transcript'][1]['value']['reasoningEffort'] == 'xhigh'


def test_pin_request_runs_choose_effort_without_a_record():
    assert "stored = rec.get('effort') if isinstance(rec, dict) else ''" in SRC
    assert "if isinstance(rec, dict) and rec.get('effort'):" not in SRC
