"""I18: send the stable prompt parts only once per coworker chat.

A Notion chat remembers its earlier messages. The overall request, the run memory, the project line and
the fixed rules are therefore sent only in the first prompt of a chat (and again after a context rotation,
a new thread or a retry of the same task). Later prompts carry only the role and the actual task plus a
short note. worker['prompt_sent'] = {thread, base, hashes} records what this chat already received.
"""
import hashlib

from .agent_guard import GUARD_RULE, COWORKER_RULE

NOTE = 'Rules, project and overall request: as in my earlier messages in this chat (still valid).'
EXEC_HEAD = 'My overall request (for context; other parts are handled separately):\n'
MIN_CHARS = 40


def _hash(text):
    return hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:16]


def _blocks(run):
    """Stable texts that may repeat between tasks of one chat."""
    found = []
    project = run.get('project_path')
    if project:
        found.append('\nThe project folder is ' + project + '.\n')
    memory = str(run.get('context') or '')
    if memory.strip():
        found.append('\nFor reference, our completed earlier work:\n' + memory + '\n')
    found += [GUARD_RULE, COWORKER_RULE]
    try:
        from .plain_protocol import SCREEN_TOOLS_LINE, NOTES_HINT
        found.append(SCREEN_TOOLS_LINE)
        found.append(NOTES_HINT)  # asked once per chat; the NOTE line says earlier rules stay valid
    except Exception:
        pass
    return [b for b in found if b and len(b.strip()) >= MIN_CHARS]


def _state(worker):
    return dict(thread=worker.get('thread_id', ''), base=worker.get('context_base_seq', 0))


def compact(worker, task, run, prompt, enabled=True):
    """(prompt, new prompt_sent state). Never raises; returns the full prompt when unsure."""
    try:
        key = _state(worker)
        old = worker.get('prompt_sent') or {}
        same_chat = old.get('thread') == key['thread'] and old.get('base') == key['base'] and key['thread']
        sent = set(old.get('hashes') or []) if same_chat else set()
        # A retry may follow a turn Notion never received: send everything again.
        fresh = not enabled or task.get('attempts', 1) > 1 or worker.get('context_reset')
        if fresh:
            sent = set()
        out = prompt
        removed = False
        goal = str(run.get('goal') or '')
        if goal and len(goal) >= MIN_CHARS and _hash('goal:' + goal) in sent:
            if EXEC_HEAD + goal in out:
                out = out.replace(EXEC_HEAD + goal, 'My overall request: see my earlier message in this chat.', 1)
                removed = True
            elif goal in out:
                out = out.replace(goal, '(my request from the earlier message in this chat)', 1)
                removed = True
        for block in _blocks(run):
            if _hash(block) in sent and block in out:
                out = out.replace(block, '\n', 1)
                removed = True
        if removed:
            out = out.rstrip() + '\n' + NOTE
        hashes = set(sent)
        if goal and goal in prompt:
            hashes.add(_hash('goal:' + goal))
        for block in _blocks(run):
            if block in prompt:
                hashes.add(_hash(block))
        return out, dict(key, hashes=sorted(hashes)[-64:])
    except Exception:
        return prompt, worker.get('prompt_sent') or {}
