"""Separate usable chats from a fully verified account configuration."""

def account_setup(account, workers):
    own=[w for w in workers if w.get('account_id')==account.get('id') and w.get('welcome') and w.get('enabled')]
    chats=(sum(w.get('purpose')=='worker' for w in own)>=5 and
           any(w.get('purpose')=='main' for w in own))
    authenticated=account.get('state')=='ready'
    ai_disabled=(account.get('state')=='ai_disabled' or account.get('ai_access',{}).get('state')=='disabled')
    complete=bool(authenticated and not ai_disabled and chats and account.get('bridge_connected'))
    stage=('ai_disabled' if ai_disabled else 'complete' if complete else 'sign_in_required' if not authenticated else
           'chats_required' if not chats else 'mcp_required')
    return dict(stage=stage,chat_ready=bool(authenticated and not ai_disabled and chats),
                complete=complete)
