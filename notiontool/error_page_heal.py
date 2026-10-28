"""Keep the NotionTool window usable while Notion shows its own crash page or hangs on the grey loading page.

After a restart Notion sometimes shows 'Oops! Something went wrong ... Refresh' instead of the
workspace (user report 2026-10-21), or stays on the grey loading placeholders forever (user report
2026-10-24 18:03). If a plain reload does not help, the JS cache (service workers + CacheStorage)
can contain a broken bundle; clearing it before the next reload fixes that case.

Round 34 (user 2026-10-28 19:05: "ich sehe keine chats mehr ... jetzt dauerhaft"): the v6 mask hides
#notion-app in a chat, so document.body.innerText was empty. The crash page was never seen as 'error'; it
counted as 'loading', the same crashing chat was reloaded 4 times and the window stayed black. Live, Notion's
own chat list crashed on every load and also after an in-app switch ("Cannot read properties of undefined
(reading 'createChildStore')" in Notion's code); clearing the caches did not help; Notion's own
"Back to my content" button worked. Now:
- the probe reads textContent (hidden text counts too) and Notion's crash icon (svg.eyes),
- a finished chat (.layout-chat) or page (.notion-frame / .layout-content) is never reloaded,
- crash page: at most one reload per crash during the start; if it comes back, press Notion's own
  "Back to my content" button instead of reloading the same crashing chat again,
- after the start the crash page is still watched for the whole session (no reloads then, only the
  "Back to my content" press, at most max_presses in a row without a healthy page in between).

This is the single, narrow exception to the rule that the harness never navigates or reloads the app
window (test_desktop_no_lag allowlists only this module):
- reloads only during the start (checks x wait_s, about 2 minutes), max max_reloads in total,
- loading page: only after STUCK_CHECKS consecutive checks without a finished chat or page,
- clear JS cache once after the first failed plain reload, then reload again.
"""

# Login/onboarding pages are left alone. A finished chat or page with real text is healthy (''), even if its
# text talks about errors. 'Loading...' or empty placeholders count as 'loading'. Without a finished layout:
# Notion's crash page -> 'error', anything else -> 'loading'.
PROBE = ("(function(){"
         "if(/\\/login|\\/signup|onboarding/.test(location.pathname))return '';"
         "var a=document.getElementById('notion-app')||document.body;"
         "var l=document.querySelector('.layout-chat,.notion-frame,.layout-content');"
         "if(l){var lt=String(l.textContent||'').replace(/\\s+/g,'');"
         "return (lt.length<20||/^Loading/.test(lt))?'loading':'';}"
         "var t=String((a&&a.textContent)||'');"
         "if((a&&a.querySelector&&a.querySelector('svg.eyes'))"
         "||(/Something went wrong/.test(t)&&/Refresh/.test(t)&&t.length<600))return 'error';"
         "return 'loading';})()")

# Notion's own "Back to my content" button on its crash page (the same click a user would do).
PRESS_BACK = ("(function(){"
              "var a=document.getElementById('notion-app');"
              "if(!a||document.querySelector('.layout-chat'))return '';"
              "var bs=a.querySelectorAll('[role=\"button\"],button');var pick=null,other=null;"
              "for(var i=0;i<bs.length;i++){var x=String(bs[i].textContent||'');"
              "if(/Back to my content/i.test(x)){pick=bs[i];break;}"
              "if(!other&&x&&!/Refresh|Aktualisieren|Neu laden/i.test(x))other=bs[i];}"
              "if(!pick&&a.querySelector('svg.eyes'))pick=other;"
              "if(!pick)return '';pick.click();return 'pressed';})()")

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

STUCK_CHECKS = 8  # 8 x 2.5 s = 20 s without a finished chat or page
ERROR_CHECKS = 2  # 2 checks of the crash page (Notion shows it briefly while it loads on its own)
WATCH_WAIT_S = 4.0  # after the start: one cheap check every 4 s
MAX_PRESSES = 3  # "Back to my content" presses in a row without a healthy page in between


def _eval(session, expression, timeout=10):
    result = session.send('Runtime.evaluate', {'expression': expression, 'returnByValue': True}, timeout) or {}
    if not isinstance(result, dict):
        return ''
    return (result.get('result') or {}).get('value') or ''


def page_state(session):
    return _eval(session, PROBE)


def is_error_page(session):
    return page_state(session) == 'error'


def press_back(session):
    """Press Notion's own 'Back to my content' button on its crash page; True when pressed."""
    return _eval(session, PRESS_BACK) == 'pressed'


def _clear_cache(session):
    """Clear service workers and CacheStorage; ignore errors."""
    try:
        session.send('Runtime.evaluate',
                     {'expression': _CLEAR_JS, 'awaitPromise': True, 'returnByValue': True}, 20)
    except Exception:  # noqa: BLE001
        pass


def heal(get_session, stop_event, store, checks=48, wait_s=2.5, max_reloads=4,  # 48 x 2.5 s = 2 min
         watch=True, watch_wait_s=WATCH_WAIT_S, max_presses=MAX_PRESSES):
    """Keep the window out of Notion's crash page; returns the number of reloads.

    Start (checks x wait_s): a crash page gets one reload, a stuck loading page a reload after STUCK_CHECKS
    checks (JS cache cleared once after the first failed reload, max max_reloads in total). A crash page
    that comes back after its reload, or shows up later in the session, gets Notion's own
    'Back to my content' press instead of another reload. Runs until stop_event is set (watch=True).
    """
    reloads = 0
    loading = 0
    errors = 0
    presses = 0
    cache_cleared = False
    crash_reloaded = False
    stuck_logged = False
    n = 0
    while True:
        startup = n < checks
        if not startup and not watch:
            return reloads
        n += 1
        if stop_event.wait(wait_s if startup else watch_wait_s):
            return reloads
        session = get_session()
        if session is None:
            continue
        try:
            state = page_state(session)
        except Exception:  # noqa: BLE001
            continue
        if state == '':
            # Healthy chat or page: the next crash starts fresh.
            loading = errors = presses = 0
            crash_reloaded = False
            stuck_logged = False
            continue
        if state == 'error':
            loading = 0
            errors += 1
        else:
            errors = 0
            loading = loading + 1 if startup else 0  # loading pages are only reloaded during the start
        if errors < ERROR_CHECKS and loading < STUCK_CHECKS:
            continue
        if state == 'error' and (crash_reloaded or not startup or reloads >= max_reloads):
            # Reloading the same crashing chat again does not help (live 2026-10-28): leave the crash page
            # through Notion's own button so the window stays usable.
            errors = 0
            if presses >= max_presses:
                if not stuck_logged:
                    store.event('desktop_error_page_stuck',
                                message='Notion crash page stays after %d "Back to my content" presses' % presses)
                    stuck_logged = True
                continue
            try:
                pressed = press_back(session)
            except Exception:  # noqa: BLE001
                pressed = False
            presses += 1
            if pressed:
                store.event('desktop_error_page_back',
                            message='Notion showed "Something went wrong"; pressed "Back to my content" (%d)'
                            % presses)
            continue
        if reloads >= max_reloads:
            loading = 0
            if not stuck_logged:
                store.event('desktop_error_page_stuck',
                            message='Notion page still broken after %d reloads' % reloads)
                stuck_logged = True
            continue
        # After the first plain reload failed, clear the JS cache once before the next attempt.
        if reloads >= 1 and not cache_cleared:
            _clear_cache(session)
            cache_cleared = True
        try:
            session.send('Page.reload', {'ignoreCache': True}, 10)
        except Exception:  # noqa: BLE001
            continue
        reloads += 1
        loading = 0
        errors = 0
        if state == 'error':
            crash_reloaded = True
        what = 'showed "Something went wrong"' if state == 'error' else 'hung on the loading page'
        store.event('desktop_error_page_reloaded',
                    message='Notion %s; reloaded (%d)' % (what, reloads))
