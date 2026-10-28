"""Round 31: Notion's reset to Medium after a chat switch never replaces the user's pick."""
import json
from pathlib import Path

from notiontool.notion_shell import choose_effort, history_effort, request_effort

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / 'notiontool' / 'notion_shell.py').read_text(encoding='utf-8')
JS = (ROOT / 'notiontool' / 'web' / 'desktop' / 'v6.js').read_text(encoding='utf-8')
FAKE_THREAD = 'aaaa0000bbbb4ccc8ddd000000000031'


def cfg(**value):
    return {'type': 'updated-config', 'value': value}


def body(*steps):
    transcript = [{'type': 'config', 'value': {'model': 'albuquerque-quinn'}}] + list(steps)
    transcript.append({'type': 'user', 'value': [['hi']]})
    return json.dumps({'threadId': FAKE_THREAD, 'transcript': transcript})


def test_switch_reset_after_a_pick_keeps_the_pick():
    # same shape as the live request 15:51:32: [xhigh, medium, {}] -> the user's pick is xhigh
    post = body(cfg(reasoningEffort='xhigh'), cfg(reasoningEffort='medium'), cfg(enableComputer=True))
    assert request_effort(post) == 'medium'
    assert history_effort(post) == 'xhigh'
    assert choose_effort(request_effort(post), '', history_effort(post)) == ('xhigh', True)


def test_newest_non_medium_pick_counts():
    post = body(cfg(reasoningEffort='max'), cfg(reasoningEffort='high'), cfg(reasoningEffort='medium'))
    assert history_effort(post) == 'high'


def test_no_pick_in_history_invents_nothing():
    post = body(cfg(reasoningEffort='medium'), cfg())
    assert history_effort(post) == ''
    assert choose_effort('medium', '', history_effort(post)) == ('medium', False)
    assert history_effort('no json') == ''
    assert history_effort(None) == ''


def test_stored_pick_and_fresh_pick_still_win():
    assert choose_effort('medium', 'medium', 'xhigh') == ('medium', False)
    assert choose_effort('medium', 'high', 'xhigh') == ('high', False)
    assert choose_effort('max', '', 'xhigh') == ('max', True)
    assert choose_effort('medium', '') == ('medium', False)


def test_history_pick_only_when_it_can_be_stored():
    assert 'late, late_fresh = choose_effort(req_effort, stored, history_effort(post))' in SRC
    assert 'sink(thread, late)' in SRC


def test_v6_arms_the_chat_and_watches_notions_label():
    assert 'function effortWatch()' in JS
    assert 'try { effortWatch(); } catch (e) { /* ignore */ }' in JS
    assert 'effortArm = at ? { tid: at, from: effortLevel(nativeEffort()) } : null;' in JS
    assert 'if (!box) { effortArm = null; return; }' in JS
    assert JS.count('function onEffortPick(e)') == 1
    assert 'function armEffort()' in JS
    assert 'onEffortPick({ ntPick: { tid: tid, lv: lv } });' in JS
