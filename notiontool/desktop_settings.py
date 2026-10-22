"""Settings panel schema for the NotionTool desktop app overlay (docs/DESKTOP_APP.md).

HARNESS_SETTINGS lists the harness settings the overlay shows. Server keys mirror the
/api/settings route in server.py: int bounds are the server clamps, choice options are the
values the server validates. Keys with app_local=True are new desktop-app keys that the
server does not accept yet; the overlay keeps them locally until Main wires them in.
HIDDEN_NOTION_SETTINGS lists Notion settings sections the overlay hides.
settings_schema() is pure (no I/O) and never returns secret-like keys (rule I7).
"""
import re

try:
    from .effort_policy import REASONING_ORDER
except Exception:  # pragma: no cover - keeps the schema usable without the policy module
    REASONING_ORDER = ('low', 'medium', 'high', 'xhigh', 'max')

# Default team size (Coworkers per account); team_size.py and store.py use the same value.
DEFAULT_COWORKERS = 10


def _int(key, label, group, low, high, default, help_text):
    return dict(key=key, label=label, group=group, type='int', min=low, max=high, options=None,
                default=default, help=help_text, app_local=False)


def _bool(key, label, group, default, help_text, app_local=False):
    return dict(key=key, label=label, group=group, type='bool', min=None, max=None, options=None,
                default=default, help=help_text, app_local=app_local)


def _choice(key, label, group, options, default, help_text, app_local=False):
    return dict(key=key, label=label, group=group, type='choice', min=None, max=None, options=list(options),
                default=default, help=help_text, app_local=app_local)


# Items the overlay can hide in the Notion chrome (app_hide_<item>); default True = hidden.
APP_HIDE_ITEMS = (
    ('home', 'Home', True),
    ('meetings', 'Meetings', True),
    ('whats_new', "What's new", True),
    ('templates', 'Templates', True),
    ('marketplace', 'Marketplace', True),
    ('inbox', 'Inbox', False),
    ('get_started', 'Get started', False),
    ('invite', 'Invite members', False),
)

HARNESS_SETTINGS = [
    # Coworkers (server.py clamps and store.py / engine.py defaults)
    _int('max_workers', 'Max at once (all accounts)', 'Coworkers', 1, 40, 40,
         'Most helper chats working at the same time, over all accounts.'),
    _int('per_account', 'Coworkers', 'Coworkers', 1, 40, DEFAULT_COWORKERS,
         'Helper chats per Notion account (team size). 1 to 40.'),
    _int('coworker_max_age_hours', 'Replace chats after (hours)', 'Coworkers', 1, 72, 22,
         'A helper chat older than this gets a fresh chat. The old one is kept.'),
    _int('max_runs_per_chat', 'Runs per Main chat', 'Coworkers', 1, 6, 3,
         'How many runs one Main chat may have in progress.'),
    _int('context_token_limit', 'Coworker context limit (tokens)', 'Coworkers', 8000, 10000000, 1200000,
         'A coworker chat gets a fresh context when it reaches this size.'),
    _choice('coworker_fresh_context', 'Fresh coworker context', 'Coworkers', ('threshold', 'every_task'), 'threshold',
            'threshold: replace the context at the limit; every_task: start each task fresh.'),
    _bool('coworker_delete_old_chat', 'Delete retired coworker chats', 'Coworkers', True,
          'Off keeps a retired coworker chat (renamed) instead of deleting it in Notion.'),
    # Main chat
    _bool('main_auto_continue', 'Continue the Main automatically', 'Main chat', True,
          'Send the next step to the Main chat when coworker results arrive.'),
    _bool('turn_watch', 'Watch Main turns', 'Main chat', True,
          'Watch the Main chat for finished turns and stalls.'),
    _choice('main_context_policy', 'Main context policy', 'Main chat', ('hint', 'auto', 'ask'), 'hint',
            'hint: show a hint; auto: switch the Main automatically; ask: ask before switching.'),
    _int('main_context_ask_tokens', 'Ask to switch at (tokens)', 'Main chat', 50000, 10000000, 1300000,
         'With the ask policy, the Main asks about a switch at this context size.'),
    _int('main_context_ask_turns', 'Ask to switch at (turns)', 'Main chat', 20, 5000, 1000,
         'With the ask policy, the Main asks about a switch after this many turns.'),
    # Usage limits (token_budget.py)
    _int('token_budget_6h', 'Token limit per 6 hours', 'Coworkers', 1000000, 500000000, 45000000,
         'Main chats and coworkers together may use at most this many tokens in 6 hours.'),
    _int('burst_soft_15m', 'Slow coworkers at (tokens / 15 min)', 'Coworkers', 100000, 100000000, 5000000,
         'Above this, coworkers send at most one turn every few seconds. Main chats are never slowed.'),
    _int('burst_hard_15m', 'Pause coworkers at (tokens / 15 min)', 'Coworkers', 100000, 200000000, 8000000,
         'Above this, coworkers wait until usage drops again.'),
    _int('burst_hard_60m', 'Pause coworkers at (tokens / 60 min)', 'Coworkers', 100000, 500000000, 18000000,
         'Above this, coworkers wait until usage drops again.'),
    _int('burst_turns_5m', 'Slow at coworker turns (5 min)', 'Coworkers', 5, 1000, 40,
         'This many coworker turns in 5 minutes also slows coworkers down.'),
    _int('burst_spacing_s', 'Seconds between coworker turns when slowed', 'Coworkers', 1, 600, 20,
         'While slowed, a coworker turn starts at most this often.'),
    # App (app-local: new keys, not accepted by /api/settings yet)
    _choice('app_chat_filter', 'Sidebar chats', 'App', ('mains_only', 'hide_coworkers'), 'mains_only',
            'mains_only: show only Main chats; hide_coworkers: show all chats except coworker chats.',
            app_local=True),
    _bool('app_declutter', 'Hide Notion extras', 'App', True,
          'Hide feedback buttons, upgrade offers, tips, help and other Notion extras the app does not use.',
          app_local=True),
    _bool('app_done_sound', 'Done sound', 'App', True,
          'Play a soft chime when a Main chat, the Test chat or your own chat finishes. Coworker chats stay silent.',
          app_local=True),
    # Models & effort, App look, Notifications (app-local, Studio v3 Settings tab)
    _choice('app_effort_override', 'Effort', 'App', ('auto',) + tuple(REASONING_ORDER), 'auto',
            'Automatic is best for most people. Pick a level to use it for every task.', app_local=True),
    _choice('app_theme_intensity', 'Color strength', 'App', ('soft', 'normal', 'strong'), 'normal',
            'How strong the colors and glow are.', app_local=True),
    _bool('app_compact', 'Compact mode', 'App', False,
          'Smaller spacing so more fits on screen.', app_local=True),
    _bool('app_notify_desktop', 'Desktop pop-ups', 'App', True,
          'Show a pop-up from Windows.', app_local=True),
    _bool('app_notify_done', 'When a task is done', 'App', True,
          'Tell me when a task is finished.', app_local=True),
    _bool('app_notify_needs_you', 'When the app needs you', 'App', True,
          'Tell me when a chat waits for my answer.', app_local=True),
    _bool('app_notify_sound', 'Play a sound', 'App', False,
          'Play a short sound with each notice.', app_local=True),
] + [
    _bool('app_hide_' + item, 'Hide ' + label, 'App', default,
          'Hide ' + label + ' in the Notion window.', app_local=True)
    for item, label, default in APP_HIDE_ITEMS
]

# Coworker switch rule (chat_context.switch_decision). Server keys: bounds are the /api/settings clamps in
# server.py; defaults rest on the step-gap measurement (docs/CONTEXT_SWITCH_BENCH.md, 'Defaults 2026-10-22').
SWITCH_SETTINGS = [
    _choice('coworker_switch_policy', 'Coworker switch rule', 'Coworkers', ('threshold', 'measured'), 'measured',
            'measured (default): switch between tasks when the estimated extra time beats the switch cost; '
            'threshold: switch only at the context limit. The token and turn limits stay hard caps in both.'),
    _int('switch_min_tokens', 'Measured: never switch below (tokens)', 'Coworkers', 0, 10000000, 400000,
         'Measured rule floor: step times are flat up to about 400k, so no switch for speed below this.'),
    _int('switch_base_tokens', 'Measured: base context T0 (tokens)', 'Coworkers', 0, 1000000, 12000,
         'Context size of a fresh chat after the handoff.'),
    _int('switch_cost_s', 'Measured: switch cost (s)', 'Coworkers', 0, 3600, 76,
         'Time one switch costs: new Welcome chat plus the slower first turn after the handoff.'),
    _int('switch_slope_ms_per_100k', 'Measured: extra time per task per 100k tokens (ms)', 'Coworkers', 0, 600000, 3514,
         'Extra time per task per 100k tokens of context, in ms (3514 = 0.251 s per step x 14 steps).'),
    _int('switch_error_permille_per_100k', 'Measured: extra errors per 100k (per mille)', 'Coworkers', 0, 1000, 0,
         'Rise of the error share per 100k tokens, in per mille (off by default: only an upper bound so far).'),
    _int('switch_retry_s', 'Measured: cost of one error (s)', 'Coworkers', 0, 3600, 60,
         'Time one failed turn costs (retry).'),
    _int('switch_remaining_tasks', 'Measured: tasks ahead (fallback)', 'Coworkers', 1, 50, 3,
         'Used only when the queue cannot be read; normally the queued tasks of the worker or run are counted.'),
]

# Notion settings sections the overlay hides. Account, security, billing, Notion AI and
# connections are never hidden. German labels are for the German Notion UI.
HIDDEN_NOTION_SETTINGS = [
    dict(id='meeting_notes', label='Meeting notes', label_de='Besprechungsnotizen'),
    dict(id='notion_calendar', label='Notion Calendar', label_de='Notion Calendar'),
    dict(id='notion_mail', label='Notion Mail', label_de='Notion Mail'),
    dict(id='sites', label='Sites', label_de='Websites'),
    dict(id='public_pages', label='Public pages', label_de='Öffentliche Seiten'),
    dict(id='emoji', label='Emoji', label_de='Emoji'),
    dict(id='import', label='Import', label_de='Importieren'),
]

# Never shown: anything that looks like a credential. Token-count limits (*_tokens,
# *_token_limit) are sizes, not secrets, and stay.
_SECRET = re.compile(r'token|secret|password|key_|bearer', re.I)
_TOKEN_LIMIT = re.compile(r'(?:_tokens|_token_limit)$')


def is_secret_key(key):
    key = str(key)
    return bool(_SECRET.search(key)) and not _TOKEN_LIMIT.search(key)


def settings_schema(current):
    """{'harness': entries with 'value' (from current or the default), 'switch': the Coworker switch
    rule entries (same shape), 'hidden_notion': sections}.

    The switch entries stay a separate list until web/desktop/settings.js maps them to a section
    (SECTION_OF_KEY); they are set through /api/settings."""
    current = current if isinstance(current, dict) else {}

    def items(entries):
        out = []
        for entry in entries:
            if is_secret_key(entry['key']):
                continue
            item = dict(entry)
            if item['options'] is not None:
                item['options'] = list(item['options'])
            item['value'] = current.get(entry['key'], entry['default'])
            out.append(item)
        return out

    out = {'harness': items(HARNESS_SETTINGS), 'switch': items(SWITCH_SETTINGS),
           'hidden_notion': [dict(section) for section in HIDDEN_NOTION_SETTINGS],
           'efforts': list(REASONING_ORDER)}
    # Optional: the supported efforts of the current model (cached model catalog, see
    # effort_policy.catalog_efforts). The Settings tab offers only these when present.
    catalog = current.get('model_efforts')
    if isinstance(catalog, (list, tuple)):
        out['model_efforts'] = [level for level in REASONING_ORDER if level in catalog]
    return out
