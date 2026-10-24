"""Reload the NotionTool window ONLY while Notion shows its own crash page or hangs on the grey loading page.

After a restart Notion sometimes shows 'Oops! Something went wrong ... Refresh' instead of the
workspace (user report 2026-10-21), or stays on the grey loading placeholders forever (user report
2026-10-24 18:03). If a plain reload does not help, the JS cache (service workers + CacheStorage)
can contain a broken bundle; clearing it before the next reload fixes that case.

This is the single, narrow exception to the rule that the harness never navigates or reloads the app
window (test_desktop_no_lag allowlists only this module):
- only for about a minute after the window was attached,
- crash page: reload right away; loading page: only after STUCK_CHECKS consecutive grey-only checks,
- clear JS cache once after the first failed plain reload, then reload again (max max_reloads total).
"""

PROBE = ("(function(){var t=(document.body&&document.body.innerText)||'';"
         "if(/Something went wrong/.test(t)&&/Refresh/.test(t)&&t.length<600)return 'error';"
         "if(t.length<200&&!document.querySelector('.notion-sidebar')&&"
         "document.querySelectorAll('[class*=skeleton],[class*=Skeleton],[class*=shimmer]').length>10)return 'loading';"
         "return '';})()")

# Clear service workers and CacheStorage (program files only; user data stays).
_CLEAR_JS = (
    "(async function(){"
    "var n=0,c=0;"
    "if(navigator.serviceWorker){"
    "  var rs=await navigator.serviceWorker.getRegistrations();"
    "  for(var r of rs){await r.unregister();n++;}"
    "}"
    "if(window.caches){"
    "  var ks=await caches.keys();for(var k of ks){await caches.delete(k);c++;}"
    "}"
    "return 'workers='+n+' caches='+c;"  
    "})()"
)

STUCK_CHECKS = 8  # 8 x 2.5 s = 20 s of nothing but grey placeholders


def page_state(session):
    result = session.send('Runtime.evaluate', {'expression': PROBE, 'returnByValue': True}, 10) or {}
    if not isinstance(result, dict):
        return ''
    return (result.get('result') or {}).get('value') or ''


def is_error_page(session):
    return page_state(session) == 'error'


def _clear_cache(session):
    """Clear service workers and CacheStorage; ignore errors."""
    try:
        session.send('Runtime.evaluate',
                     {'expression': _CLEAR_JS, 'awaitPromise': True, 'returnByValue': True}, 20)
    except Exception:  # noqa: BLE001
        pass


def heal(get_session, stop_event, store, checks=24, wait_s=2.5, max_reloads=4):
    """Check every wait_s seconds; reload a crash or stuck loading page at most max_reloads times.

    After the first failed plain reload the JS cache is cleared before the next reload attempt.
    """
    reloads = 0
    loading = 0
    cache_cleared = False
    for _ in range(checks):
        if stop_event.wait(wait_s):
            return reloads
        session = get_session()
        if session is None:
            return reloads
        try:
            state = page_state(session)
        except Exception:  # noqa: BLE001
            continue
        loading = loading + 1 if state == 'loading' else 0
        if state != 'error' and loading < STUCK_CHECKS:
            continue
        if reloads >= max_reloads:
            store.event('desktop_error_page_stuck',
                        message='Notion page still broken after %d reloads' % reloads)
            return reloads
        # After the first plain reload failed, clear the JS cache once before the next attempt.
        if reloads >= 1 and not cache_cleared:
            _clear_cache(session)
            cache_cleared = True
        try:
            session.send('Page.reload', {'ignoreCache': True}, 10)
        except Exception:  # noqa: BLE001
            return reloads
        reloads += 1
        loading = 0
        what = 'showed "Something went wrong"' if state == 'error' else 'hung on the loading page'
        store.event('desktop_error_page_reloaded',
                    message='Notion %s; reloaded (%d)' % (what, reloads))
    return reloads
