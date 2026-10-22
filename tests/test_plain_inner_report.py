from notiontool.plain_protocol import normalize

TASK = dict(role='executor', files=[], id='t1')


def test_fenced_report_is_unwrapped():
    text = 'Reading both files.\n```json\n{"status": "done", "summary": "6 classes, 77 functions", "files": []}\n```'
    data = normalize(TASK, text)
    assert data['status'] == 'done' and data['summary'] == '6 classes, 77 functions'
    assert not data.get('partial')


def test_partial_report_is_done_but_marked_for_review():
    text = '```json\n{"status": "partial", "summary": "Covered 40 of 67 functions"}\n```'
    data = normalize(TASK, text)
    assert data['status'] == 'done' and data['partial'] is True and data['requires_review'] is True


def test_plain_answer_unchanged():
    data = normalize(TASK, 'DONE\nAll good, 3 risks found.')
    assert data['status'] == 'done' and 'partial' not in data
