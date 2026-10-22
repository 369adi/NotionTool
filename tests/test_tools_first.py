"""Notion AI skips the MCP tool load when the request comes first: the tool load must open the message."""
from notiontool import plain_protocol, protocol

RUN = {'goal': 'Fix the pool', 'project_path': 'C:/p', 'capacity': 2}
TASK = {'id': 't1', 'role': 'executor', 'prompt': 'Edit x.py', 'acceptance': 'tests pass', 'files': ['x.py']}


def test_plain_prompt_starts_with_tool_load():
    text = plain_protocol._prompt_text(dict(TASK), dict(RUN), '', 'tok', True)
    assert text.startswith(plain_protocol.TOOLS_FIRST)
    assert 'Fix the pool' in text and text.index('NotionTool tools') < text.index('Fix the pool')


def test_json_prompt_starts_with_tool_load():
    text = protocol.prompt_for(dict(TASK), dict(RUN), '', 'tok', tools_available=True)
    assert text.startswith(plain_protocol.TOOLS_FIRST)


def test_no_tool_line_without_tools_or_project():
    assert not plain_protocol._prompt_text(dict(TASK), dict(RUN), '', 'tok', False).startswith('Before anything')
    run = {'goal': 'Say hi', 'capacity': 1}
    task = dict(TASK, files=[])
    assert not plain_protocol.tools_first(task, run, True)
    assert not plain_protocol.tools_first(TASK, dict(RUN, allocation={'mode': 'direct'}), True)
