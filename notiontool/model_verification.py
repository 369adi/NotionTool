"""Execution evidence, never inferred from selection, prompt text or style."""


class ModelVerificationError(ValueError):
    def __init__(self,expected,reported,reason='mismatch'):
        self.expected=expected
        self.reported=list(reported)
        self.reason=reason
        super().__init__('Notion execution model '+('is unverified' if reason=='missing' else 'does not match the requested model')+
                         ': expected '+str(expected)+', reported '+(', '.join(self.reported) or 'none')+'. No automatic repetition.')


def verify_execution(steps,submitted_id,expected):
    """Match the submitted user step and only inference steps in its own turn."""
    if not expected:
        raise ModelVerificationError(expected,[],'missing')
    own=False;found=[]
    for step in steps:
        if step.get('type')=='user':
            if own:break
            own=step.get('id')==submitted_id
            continue
        if not own or step.get('type')!='agent-inference':continue
        reported=step.get('model')
        secondary=[item.get('notionModelName') for item in step.get('value',[]) if isinstance(item,dict) and item.get('notionModelName')]
        # The server's dedicated inference fields must agree; never scan quoted
        # content or accept config.model as execution evidence.
        if isinstance(reported,str):found.append(reported)
        found.extend(secondary)
    unique=sorted(set(found))
    if not own or not unique:
        raise ModelVerificationError(expected,unique,'missing')
    if any(model!=expected for model in unique):
        raise ModelVerificationError(expected,unique)
    return unique


def turn_status(steps,submitted_id,outcomes):
    """Server state of our own turn: missing, running, error (failed before any output) or completed."""
    own=False;ids=[submitted_id];errors=[];produced=False
    for step in steps:
        if step.get('type')=='user':
            if own:break
            own=step.get('id')==submitted_id
            continue
        if own:
            ids.append(step.get('id'))
            if step.get('type')=='error':errors.append(step)
            if step.get('type')=='agent-inference':produced=True
    if not own:
        return 'missing',None
    if produced:
        # Output exists in this turn: never treat it as a clean failure that may be sent again.
        return 'completed',None
    if errors:
        return 'error',errors[-1]
    for outcome in outcomes:
        if isinstance(outcome,dict) and outcome.get('final_step_id') in ids:
            return ('error' if outcome.get('status')=='error' else 'completed'),None
    return 'running',None
