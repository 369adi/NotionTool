"""End-to-end scenarios in the test app (port 8791) against the simulated Notion (fake_notion.py).

Each scenario starts a fresh fake-mode test app from this source tree, restarts it once and checks the
events and the simulated chats. The real NotionTool is never started, stopped or contacted.
"""
import json
import time

import pytest

from notiontool import test_app

RESULTS = {}


@pytest.fixture(scope='module', autouse=True)
def _stop_test_app_afterwards():
    yield
    test_app.stop()
    root, _, _, _ = test_app._paths()
    summary = {name: dict(verdict=r['verdict'], reasons=r['reasons'], seconds=r.get('seconds'),
                          events=[e['kind'] for e in r['events']], details=r.get('details'))
               for name, r in RESULTS.items()}
    (root / 'work' / 'test-app-scenarios.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')


@pytest.mark.parametrize('name', test_app.SCENARIOS)
def test_scenario_passes(name):
    result = test_app.scenario(name)
    RESULTS[name] = result
    assert result['scenario'] == name
    assert result['verdict'] == 'passed', json.dumps(dict(reasons=result['reasons'], events=result['events'],
                                                          details=result.get('details'),
                                                          log_tail=result.get('log_tail', '')[-1500:]), indent=1)
    assert result['events'], 'a passed scenario must report the events it saw'


def test_plain_test_instance_has_no_simulated_notion():
    started = test_app.start(fake=False)
    try:
        assert started['ok'], started
        assert started['fake_notion'] is False
        time.sleep(3)
        _, data, _, _ = test_app._paths()
        kinds = [e['kind'] for e in test_app._events(data)]
        assert 'test_instance_started' in kinds
        assert test_app.FAKE_READY_EVENT not in kinds
        assert not (data / 'fake_notion.json').exists()
        assert test_app._object(data, 'accounts', 'ready') is None
    finally:
        test_app.stop()


def test_unknown_scenario_is_refused():
    with pytest.raises(ValueError, match='scenario name'):
        test_app.scenario('explode')
