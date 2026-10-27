"""Standalone window: Notion API client helpers and the window switch (small, fast tests only)."""
import json

import main
from notiontool import notion_api as na

SPACE = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
OTHER = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
T1 = '11111111-1111-4111-8111-111111111111'
T2 = '22222222-2222-4222-8222-222222222222'
T3 = '33333333-3333-4333-8333-333333333333'
T4 = '44444444-4444-4444-8444-444444444444'
T5 = '55555555-5555-4555-8555-555555555555'


def wrapped(rec):
    return {'spaceId': rec.get('space_id', SPACE), 'value': {'value': rec, 'role': 'editor'}}


def test_thread_key_and_millis():
    assert na.thread_key(T1.replace('-', '').upper()) == T1
    assert na.thread_key('not a thread') == ''
    assert na.millis(1700000000) == 1700000000000
    assert na.millis(1700000000000) == 1700000000000
    assert na.millis('2024-01-01T00:00:00Z') == 1704067200000
    assert na.millis('') == 0 and na.millis(True) == 0 and na.millis('soon') == 0


def test_model_key_is_always_explicit():
    assert na.model_key('opus-5.5') == 'opus'
    assert na.model_key('Sonnet 4.6') == 'sonnet'
    assert na.model_key('auto') == 'opus'
    assert na.model_key('', default='haiku') == 'haiku'


def test_chat_rows_lists_only_live_chats_of_the_workspace_newest_first():
    listing = {
        'transcripts': [{'id': T1, 'title': 'First', 'updatedAt': 2000}],
        'threadIds': [T2.replace('-', '')],
        'unreadThreadIds': [T1],
        'recordMap': {'thread': {T2: wrapped({'id': T2, 'space_id': SPACE, 'title': 'Second', 'updated_time': 3000})}},
    }
    records = {
        T3: wrapped({'id': T3, 'space_id': SPACE, 'title': 'Main', 'created_time': 1000}),
        T4: wrapped({'id': T4, 'space_id': OTHER, 'title': 'Other space', 'created_time': 9000}),
        T5: wrapped({'id': T5, 'space_id': SPACE, 'title': 'Trashed', 'alive': False, 'created_time': 9000}),
    }
    rows = na.chat_rows(listing, records, SPACE)
    assert [r['id'] for r in rows] == [T2, T1, T3]
    assert [r['title'] for r in rows] == ['Second', 'First', 'Main']
    assert [r['unread'] for r in rows] == [False, True, False]


def test_chat_rows_without_a_title_say_new_chat():
    rows = na.chat_rows({'threadIds': [T1]}, {}, SPACE)
    assert rows == [{'id': T1, 'title': 'New chat', 'updated': 0, 'unread': False}]


def test_standalone_url_opens_the_own_ui_with_the_token():
    assert main.standalone_url('http://127.0.0.1:8790/#tok123') == 'http://127.0.0.1:8790/app/index.html#tok123'


def test_app_ui_mode_defaults_to_the_overlay_fallback(tmp_path):
    assert main.app_ui_mode(str(tmp_path)) == 'overlay'
    (tmp_path / 'desktop_app.json').write_text(json.dumps({'app_ui_mode': 'standalone'}), encoding='utf-8')
    assert main.app_ui_mode(str(tmp_path)) == 'standalone'
    (tmp_path / 'desktop_app.json').write_text(json.dumps({'app_ui_mode': 'overlay'}), encoding='utf-8')
    assert main.app_ui_mode(str(tmp_path)) == 'overlay'
