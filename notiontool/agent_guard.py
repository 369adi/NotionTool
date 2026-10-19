"""Agent guard (invariant I12): Coworkers are NotionTool Welcome chats, never Notion Custom Agents.

Prevention: GUARD_RULE is the one text every prompt/context that mentions delegating or Coworkers carries
(with_rule adds it once). Coworker prompts also carry COWORKER_RULE.

Detection: forbidden_calls(transcript) checks every tool call of a read chat turn (names, wrapper inner
names and identifying argument fields such as function/toolName) for agent/automation actions:
createAgent, updateAgent, publishAgent, createAutofillAgent, spawnSession, agent setup / enter-setup-mode,
create trigger/automation. Free text (shell commands, file contents, answers) is never matched, so a
Coworker that greps for 'createAgent' is not flagged. report() writes a 'forbidden_agent_action' event,
a high-priority problem record and a red flag on the task; flagged tasks are never auto-accepted
(blocks_accept). Nothing is ever deleted or changed in Notion (I5): the user removes the agent.
"""
import json
import re
import time

GUARD_RULE = ('Coworkers are NotionTool Welcome chats managed by NotionTool. Delegate ONLY via the NotionTool '
              'API /api/operator/delegate (nt_api). NEVER create, update or publish Notion Custom Agents, never '
              'create agents, automations, triggers or new Notion chats/sessions on the Notion website, even if a '
              'Notion tool offers it. If delegation is not possible, stop and tell the user.')
COWORKER_RULE = 'You are a coworker: do the task yourself, do not delegate further.'
RESTART_NOTE = ('If a NotionTool tool call fails because NotionTool is restarting or the MCP connection is briefly gone, '
                'that is only a restart of about 10-60 s: wait briefly, check whether the call took effect, then continue your task. '
                'Do not stop or give up because of it.')

EVENT = 'forbidden_agent_action'
PROBLEM_TITLE = 'A chat tried to create a Notion agent instead of using coworkers'
PROBLEM_KIND = 'problems'

_FORBIDDEN = re.compile(
    r'^(?:(?:create|update|publish)(?:autofill|custom)?agents?'
    r'|spawnsessions?'
    r'|(?:enter)?(?:agent)?setupmode|entersetup|agentsetup|setupagent'
    r'|(?:create|add|update|enable)\w*(?:triggers?|automations?))$')
_ID_KEYS = ('function', 'functionName', 'toolName', 'tool_name', 'tool', 'name', 'action', 'method', 'operation')
_NEST_KEYS = ('args', 'arguments', 'toolArguments', 'input', 'parameters', 'params', 'tool_input', 'toolInput')


def with_rule(text, coworker=False):
    """text plus GUARD_RULE (and COWORKER_RULE for Coworker prompts), each added only once."""
    text = str(text or '').rstrip()
    for rule in (GUARD_RULE, COWORKER_RULE if coworker else '', RESTART_NOTE if coworker else ''):
        if rule and rule not in text:
            text = (text + '\n' + rule) if text else rule
    return text


def _norm(name):
    name = re.split(r'[./:\\]', str(name or '').strip())[-1]
    return re.sub(r'[^a-z0-9]', '', name.lower())


def forbidden_name(name):
    """True when a tool or function name is an agent/automation action."""
    return bool(name) and isinstance(name, str) and len(name) < 120 and bool(_FORBIDDEN.match(_norm(name)))


def _as_dict(value):
    if isinstance(value, str) and value.strip().startswith('{'):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _names(entity, depth=0):
    """The tool name of a step plus identifying fields of its (nested) arguments."""
    if depth > 4 or not isinstance(entity, dict):
        return []
    found = []
    for key in _ID_KEYS:
        value = entity.get(key)
        if isinstance(value, dict):
            value = value.get('name')
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
    for key in _NEST_KEYS:
        found += _names(_as_dict(entity.get(key)), depth + 1)
    return found


def _entities(transcript):
    if isinstance(transcript, dict):
        for key in ('entities', 'transcript', 'steps', 'items', 'values'):
            if isinstance(transcript.get(key), list):
                return transcript[key]
        return [transcript]
    return list(transcript or [])


def _is_tool(entity):
    from .progress_feed import role
    if role(entity) == 'tool':
        return True
    kind = str(entity.get('kind') or entity.get('type') or '').lower()
    return 'result' not in kind and any(entity.get(k) for k in ('tool_name', 'toolName', 'function'))


def forbidden_calls(transcript):
    """[{name, index}] for every forbidden tool call in a transcript (list of entities or a wrapper dict)."""
    hits = []
    for index, entity in enumerate(_entities(transcript)):
        if not isinstance(entity, dict) or not _is_tool(entity):
            continue
        bad = next((n for n in _names(entity) if forbidden_name(n)), '')
        if bad:
            hits.append(dict(name=bad, index=index))
    return hits


def chat_link(thread_id, page_id=''):
    if not thread_id:
        return ''
    base = 'https://www.notion.so/' + (str(page_id).replace('-', '') if page_id else '')
    return base + '?t=' + str(thread_id).replace('-', '')


def blocks_accept(task):
    """True when a task carries the agent red flag: it must never be auto-accepted."""
    return bool((task or {}).get('agent_red_flag'))


def report(store, task, hits, *, thread_id='', worker_id='', url=''):
    """Raise the event, the high-priority problem and the task red flag once per new tool name. Never touches Notion."""
    if not hits or task is None:
        return False
    flag = task.get('agent_red_flag') or {}
    known = set(flag.get('tools') or [])
    names = sorted({h['name'] for h in hits} - known)
    if not names:
        return False
    url = url or chat_link(thread_id, task.get('page_id', ''))
    tools = sorted(known | set(names))
    message = '%s (%s). Chat: %s. Nothing was deleted; please remove the agent in Notion yourself.' % (
        PROBLEM_TITLE, ', '.join(tools), url or thread_id or 'unknown')
    task['agent_red_flag'] = dict(tools=tools, thread_id=thread_id, url=url, at=flag.get('at') or time.time())
    task['auto_accept_blocked'] = True
    task['requires_review'] = True
    if store is None:
        return True
    store.put('tasks', task)
    store.event(EVENT, task.get('run_id', ''), task_id=task.get('id', ''), worker_id=worker_id,
                thread_id=thread_id, url=url, tools=tools, priority='high', message=message)
    store.put(PROBLEM_KIND, dict(id='agent-%s' % task.get('id', ''), kind=EVENT, priority='high',
                                 title=PROBLEM_TITLE, message=message, url=url, thread_id=thread_id,
                                 task_id=task.get('id', ''), run_id=task.get('run_id', ''),
                                 tools=tools, created_at=time.time(), resolved=False))
    return True
