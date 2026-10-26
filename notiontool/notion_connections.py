"""Read the selected account's actual Notion MCP connections and manage their tool permissions."""
from urllib.parse import urlsplit, urlunsplit
from .harness_account import selected_account
from .notion import unwrap


def connections(adapter, account_id):
    client = adapter.client(account_id)
    client.identity()
    modules = []
    for wrapped in client.info.get('records', {}).get('space_view', {}).values():
        view = unwrap(wrapped)
        if view.get('space_id') == client.info['space_id']:
            modules.extend((view.get('settings') or {}).get('agent_chat_modules') or [])
    enabled = {(m.get('pointer') or {}).get('id'): m.get('defaultEnabled') is not False for m in modules
               if (m.get('pointer') or {}).get('table') == 'workflow_module'}
    if not enabled:
        return client, []
    result = client.call('syncRecordValuesSpaceInitial', {'requests': [
        {'pointer': {'table': 'workflow_module', 'id': ident, 'spaceId': client.info['space_id']}, 'version': -1}
        for ident in enabled]})
    rows = []
    for ident, wrapped in result.get('recordMap', {}).get('workflow_module', {}).items():
        record = unwrap(wrapped)
        data = record.get('data') or {}
        url = data.get('serverUrl') or record.get('server_url')
        if ident not in enabled or not record.get('alive', True) or not url:
            continue
        rows.append(dict(id=ident, name=data.get('name') or record.get('name') or 'MCP server',
                         url=url, enabled=enabled[ident], read_auto=data.get('runReadToolsAutomatically') is True,
                         write_auto=data.get('runWriteToolsAutomatically') is True))
    return client, rows


def public(rows):
    result = []
    for row in rows:
        value = dict(row)
        parsed = urlsplit(value['url'])
        value['url'] = urlunsplit((parsed.scheme, parsed.netloc.rsplit('@', 1)[-1], parsed.path, '', ''))
        result.append(value)
    return result


def ensure_notiontool(server, account_id):
    import hashlib
    owner_token = server.host_api_key
    fingerprint = hashlib.sha256(owner_token.encode()).hexdigest()
    url = server.store.settings().get('public_mcp_url')
    if not url:
        server.start_tunnel()
        url = server.store.settings().get('public_mcp_url')
    if not url:
        raise ValueError('No public MCP URL for NotionTool yet')
    _, rows = connections(server.adapter, account_id)
    current = next((row for row in rows if row['url'] == url), None)
    account = server.store.get('accounts', account_id) or {}
    refresh_auth = account.get('bridge_owner_fingerprint') != fingerprint
    if not current or not current['enabled'] or not current['read_auto'] or not current['write_auto'] or refresh_auth:
        account = dict(server.store.get('accounts', account_id))
        account['bridge_connected'] = False
        server.store.put('accounts', account)
        server.adapter.connect_bridge(account_id, url, owner_token, read_auto=True, write_auto=True, refresh_auth=refresh_auth)
        _, rows = connections(server.adapter, account_id)
        current = next((row for row in rows if row['url'] == url), None)
    if not current or not all(current[key] for key in ('enabled', 'read_auto', 'write_auto')):
        account = dict(server.store.get('accounts', account_id))
        account['bridge_connected'] = False
        server.store.put('accounts', account)
        raise ValueError('NotionTool MCP permissions were not confirmed by Notion')
    account = dict(server.store.get('accounts', account_id))
    account.update(bridge_connected=True, bridge_url=url, bridge_integration=current['id'], bridge_error='',
                   bridge_owner_fingerprint=fingerprint)
    server.store.put('accounts', account)
    return dict(connected=True, read_auto=True, write_auto=True)


def desk_handlers(server):
    def active():
        ident = selected_account(server.store)
        if not ident:
            raise ValueError('Sign in to load Notion connections.')
        return connections(server.adapter, ident)

    def listing(args):
        _, rows = active()
        return dict(connections=public(rows))

    def permissions(args):
        client, rows = active()
        ident = str(args.get('id') or '')
        if not any(row['id'] == ident for row in rows):
            raise ValueError('This connection does not belong to the active account.')
        if any(type(args.get(key)) is not bool for key in ('read_auto', 'write_auto')):
            raise ValueError('Choose on or off for read and write tools.')
        server.adapter.transaction(client, 'mcpServerSettings.updateToolPermissions', [
            {'pointer': {'table': 'workflow_module', 'id': ident, 'spaceId': client.info['space_id']},
             'path': ['data', remote], 'command': 'set', 'args': args[local]}
            for local, remote in (('read_auto', 'runReadToolsAutomatically'), ('write_auto', 'runWriteToolsAutomatically'))])
        fresh = listing({})
        saved = next((row for row in fresh['connections'] if row['id'] == ident), {})
        if any(saved.get(key) != args[key] for key in ('read_auto', 'write_auto')):
            raise ValueError('Notion did not save these tool permissions.')
        return fresh

    return dict(notion_connections=listing, notion_connection_permissions=permissions)
