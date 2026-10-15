"""Enable only the requested MCP URL through the owner's supported settings."""
from .notion import unwrap


def ensure_mcp_allowed(adapter,client,url):
    info=client.info;sid=info['space_id']
    space=unwrap(info.get('records',{}).get('space',{}).get(sid,{}))
    if not space:raise ValueError('Workspace policy metadata is unavailable.')
    settings=space.get('settings') or {};allowed=list(settings.get('allowed_mcp_server_urls') or [])
    if settings.get('disallow_custom_mcp_servers') is False and (
            not settings.get('restrict_mcp_urls_to_allowlist') or url in allowed):return False
    if space.get('organization_id'):raise ValueError('Organization policy needs its administrator; it was not overridden.')
    owner=any(unwrap(v).get('user_id')==info['user_id'] and unwrap(v).get('space_id')==sid and
        unwrap(v).get('membership_type')=='owner' for v in info.get('records',{}).get('space_user',{}).values())
    if not owner:raise ValueError('Only the workspace owner may approve the requested MCP URL.')
    if url not in allowed:allowed.append(url)
    values={'disallow_custom_mcp_servers':False,'restrict_mcp_urls_to_allowlist':True,'allowed_mcp_server_urls':allowed}
    adapter.transaction(client,'mcpSettings.approveNotionToolServer',[
        {'pointer':{'table':'space','id':sid,'spaceId':sid},'path':['settings',key],'command':'set','args':value}
        for key,value in values.items()])
    fresh=client.identity();verified=(unwrap(fresh.get('records',{}).get('space',{}).get(sid,{})).get('settings') or {})
    if verified.get('disallow_custom_mcp_servers') is not False or url not in verified.get('allowed_mcp_server_urls',[]):
        raise ValueError('Notion did not confirm the owner MCP policy update.')
    return True
