"""Reload the NotionTool window ONLY while Notion shows its own crash page.

After a restart Notion sometimes shows 'Oops! Something went wrong ... Refresh' instead of the
workspace (user report 2026-10-21). That page holds no user input, so reloading it is the same as
the user pressing 'Refresh'. This is the single, narrow exception to the rule that the harness
never navigates or reloads the app window (test_desktop_no_lag allowlists only this module):
- only for about a minute after the window was attached,
- only when the page text is that short crash page (no chat, no editor),
- at most twice; after that an event is logged and nothing else happens.
"""

PROBE = ("(function(){var t=(document.body&&document.body.innerText)||'';"
         "return /Something went wrong/.test(t)&&/Refresh/.test(t)&&t.length<600;})()")


def is_error_page(session):
    result = session.send('Runtime.evaluate', {'expression': PROBE, 'returnByValue': True}, 10) or {}
    if not isinstance(result, dict):
        return False
    return bool((result.get('result') or {}).get('value'))


def heal(get_session, stop_event, store, checks=24, wait_s=2.5, max_reloads=2):
    """Check every wait_s seconds; reload the crash page at most max_reloads times. Returns reload count."""
    reloads = 0
    for _ in range(checks):
        if stop_event.wait(wait_s):
            return reloads
        session = get_session()
        if session is None:
            return reloads
        try:
            broken = is_error_page(session)
        except Exception:  # noqa: BLE001
            continue
        if not broken:
            continue
        if reloads >= max_reloads:
            store.event('desktop_error_page_stuck', message='Notion error page still shown after %d reloads' % reloads)
            return reloads
        try:
            session.send('Page.reload', {'ignoreCache': False}, 10)
        except Exception:  # noqa: BLE001
            return reloads
        reloads += 1
        store.event('desktop_error_page_reloaded',
                    message='Notion showed "Something went wrong"; reloaded (%d)' % reloads)
    return reloads
