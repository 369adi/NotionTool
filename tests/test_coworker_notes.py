"""Coworkers report NotionTool weaknesses/optimisations as 'NotionTool note:' lines; the engine keeps them."""
from notiontool import plain_protocol

RUN = {'goal': 'Research tools', 'project_path': 'C:/p', 'capacity': 2}
TASK = {'id': 't1', 'role': 'executor', 'prompt': 'Find repos', 'acceptance': 'ranked list', 'files': []}


def test_executor_prompt_asks_for_notes():
    assert plain_protocol.NOTES_HINT in plain_protocol._prompt_text(dict(TASK), dict(RUN), '', 'tok', True)


def test_notes_are_extracted_and_deduplicated():
    text = ('Result: done\n\n- NotionTool note: the delegate API should accept model_reason\n'
            'NotionTool note: the delegate API should accept model_reason\n'
            '**NotionTool note:** steps chip still shows x/450\nnothing else')
    notes = plain_protocol.notes_from(text)
    assert notes[0] == 'the delegate API should accept model_reason'
    assert len(notes) == 2 and 'x/450' in notes[1]


def test_no_notes_and_limit():
    assert plain_protocol.notes_from('All done, tests pass.') == []
    many = '\n'.join('NotionTool note: point %d' % i for i in range(9))
    assert len(plain_protocol.notes_from(many)) == 5
