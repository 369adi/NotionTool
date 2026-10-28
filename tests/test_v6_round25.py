# Round 25 (user 2026-10-28 07:10-07:12): effort pin, kept turns, view shift, own chat age.
import json
import os

from notiontool.notion_shell import choose_effort, request_effort

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, 'notiontool', 'web', 'desktop')


def read(name):
    with open(os.path.join(WEB, name), encoding='utf-8') as fh:
        return fh.read()


def body(*efforts):
    steps = [{'type': 'config', 'value': {}}] + [{'type': 'updated-config', 'value': ({'reasoningEffort': e} if e else {})} for e in efforts]
    return json.dumps({'threadId': 'a' * 32, 'transcript': steps})


def test_request_effort_newest_non_empty():
    assert request_effort(body('xhigh', '', '')) == 'xhigh'
    assert request_effort(body('high', 'low')) == 'low'
    assert request_effort(body()) == ''
    assert request_effort('not json') == ''


def test_fresh_pick_in_notion_menu_wins_and_is_stored():
    assert choose_effort('xhigh', 'high') == ('xhigh', True)
    assert choose_effort('low', 'high') == ('low', True)


def test_reset_or_missing_keeps_stored_pick():
    assert choose_effort('medium', 'xhigh') == ('xhigh', False)
    assert choose_effort('', 'high') == ('high', False)
    assert choose_effort('auto', 'high') == ('high', False)
    assert choose_effort('high', 'high') == ('high', False)


def test_kept_turns_survive_slow_load_and_big_chats():
    js = read('v6.js')
    assert 'fdCache.delete(th), feedLsDel(th)' not in js
    assert 'FD_LS_MAX = 400000' in js
    assert "while (parts.length > 1 && parts.join('').length > FD_LS_MAX) parts.shift();" in js


def test_no_sideways_shift():
    js = read('v6.js')
    assert "document.addEventListener('scroll', noSideScroll, true);" in js
    assert "ox === 'hidden' || ox === 'clip'" in js


def test_own_chat_age_from_thread():
    js = read('v6.js')
    assert "ctx.call('chat_thread_age', { thread_id: k, send: false })" in js
    assert 'hours = threadAge(w ? w.thread_id : currentThread());' in js


def test_model_button_hidden_by_css():
    css = read('v6.css')
    assert '[aria-label^="Change effort"] ~ [role="button"]:not([aria-label])' in css
