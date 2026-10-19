"""Token-based context budget for a single task.

Not wired into engine.py yet. Token counts reuse chat_context.estimate_tokens,
the same estimate that ChatContext.usage() sums (chat_context.py:12-14, :52).
"""
import hashlib
import json

from .chat_context import estimate_tokens

ARTIFACT_KIND = 'artifacts'

# Fixed source order (contract):
#   1. Artifacts from the store kind 'artifacts' whose project_id equals task['project_id'],
#      in store order (Store.all orders by rowid, so the first stored artifact comes first).
# A task without a project_id gets no artifacts.
# Adding stops at the first source that would push total_tokens over budget_tokens;
# later sources are not tried, so the result stays predictable.
# Each source's sha256 and tokens are computed from the exact block that is added to the text,
# and the text is the plain concatenation of those blocks.


def artifact_body(doc):
    for key in ('text', 'content'):
        value = doc.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(doc, ensure_ascii=False, sort_keys=True)


def artifact_block(doc):
    """Exact text added for one artifact."""
    return f"[artifact {doc.get('id', '')}]\n{artifact_body(doc)}\n\n"


def project_artifacts(store, project_id):
    project_id = str(project_id or '').strip()
    if not project_id:
        return []
    return [doc for doc in store.all(ARTIFACT_KIND) if str(doc.get('project_id') or '').strip() == project_id]


def build_context(store, task, budget_tokens):
    """Return (text, context_manifest) for task within budget_tokens.

    context_manifest = {sources: [{kind, id, sha256, tokens}], total_tokens, budget}.
    """
    budget = max(0, int(budget_tokens or 0))
    blocks, sources, total = [], [], 0
    for doc in project_artifacts(store, (task or {}).get('project_id')):
        block = artifact_block(doc)
        tokens = estimate_tokens(block)
        if total + tokens > budget:
            break
        blocks.append(block)
        sources.append(dict(kind='artifact', id=str(doc.get('id', '')),
                            sha256=hashlib.sha256(block.encode('utf-8')).hexdigest(), tokens=tokens))
        total += tokens
    return ''.join(blocks), dict(sources=sources, total_tokens=total, budget=budget)
