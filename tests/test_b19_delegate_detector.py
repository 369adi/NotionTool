import time

from notiontool import ai_disable_detector
from notiontool.operator_main import OPERATOR, delegating_chat, delegating_worker
from notiontool.store import Store


def _store(tmp_path):
    store = Store(tmp_path / 'data')
    store.put('accounts', dict(id='acc_off', email='off@x.de', state='ai_disabled'))
    store.put('accounts', dict(id='acc_on', email='on@x.de', state='ready'))
    store.put('workers', dict(id='acc_off:op', account_id='acc_off', purpose=OPERATOR, state='ready'))
    store.put('workers', dict(id='acc_on:main', account_id='acc_on', purpose='main', state='ready'))
    store.put('workers', dict(id='acc_on:w1', account_id='acc_on', purpose='worker', state='ready', enabled=True))
    store.put('chats', dict(id='c_op', main_worker='acc_off:op', operator=True, state='active'))
    store.put('chats', dict(id='c_main', main_worker='acc_on:main'))
    store.put('chats', dict(id='c_cw', main_worker='acc_on:w1'))
    return store


def test_every_main_may_delegate(tmp_path):
    store = _store(tmp_path)
    assert delegating_worker(store, store.get('chats', 'c_main'))['id'] == 'acc_on:main'
    assert delegating_worker(store, store.get('chats', 'c_cw')) is None


def test_default_chat_skips_ai_disabled_operator(tmp_path):
    store = _store(tmp_path)
    assert delegating_chat(store)['id'] == 'c_main'


def test_detector_inference_signal_and_log_in_data_dir(tmp_path):
    store = _store(tmp_path)
    store.put('accounts', dict(id='acc_off', email='off@x.de', state='ready'))
    ai_disable_detector.scan(store)
    store.event('notion_inference_error', worker_id='acc_off:op',
                message='Agent inference failed: aiNotEnabled (AiNotEnabledOnSpaceGetCompletionError)')
    ai_disable_detector.scan(store, time.time() + 1)
    reports = [r for r in ai_disable_detector.reports(store) if r['account_id'] == 'acc_off']
    assert len(reports) == 1 and 'aiNotEnabled' in reports[0]['signal']
    assert not [r for r in ai_disable_detector.reports(store) if r['account_id'] == 'acc_on']
    log = tmp_path / 'data' / 'ai_disable_detector.log'
    assert log.is_file() and 'ai_disabled_detected' in log.read_text(encoding='utf-8')
