from notiontool import desktop_shell, plain_protocol


def test_task_label_falls_back_to_prompt():
    assert desktop_shell._task_label({}) == ''
    assert desktop_shell._task_label({'title': 'Fix login'}) == 'Fix login'
    prompt = 'Count lines of parallel_plan.py\n\nEnd your answer with a final line RESULT: DONE.'
    assert desktop_shell._task_label({'prompt': prompt}) == 'Count lines of parallel_plan.py'
    assert desktop_shell._task_label({'state': 'running'}) == 'Working'


def test_busy_states_cover_sent_and_claimed():
    for state in ('running', 'sending', 'sent', 'claimed', 'tool_pending'):
        assert state in desktop_shell.BUSY_TASK_STATES
    for state in ('done', 'failed', 'cancelled', 'queued'):
        assert state not in desktop_shell.BUSY_TASK_STATES


def _parts(project_path=''):
    task = dict(id='t1', role='executor', prompt='Read a file and report.', acceptance='the count is given')
    run = dict(id='r1', goal='Test', capacity=1)
    if project_path:
        run['project_path'] = project_path
    return task, run


def test_no_result_file_hint_without_token():
    task, run = _parts()
    text = plain_protocol._prompt_text(task, run, '', 'TOKEN123', True)
    assert 'TOKEN123' not in text
    assert 'nt_write_file' not in text


def test_result_file_hint_comes_with_token():
    task, run = _parts(project_path='C:/proj')
    text = plain_protocol._prompt_text(task, run, '', 'TOKEN123', True)
    assert 'TOKEN123' in text
    assert 'nt_write_file' in text
