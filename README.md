# NotionTool — single account harness

Run **Start NotionTool.cmd** or **NotionTool.exe**. Source: `python main.py`.
The existing `data` folder is retained.

## Connection

Notion AI still uses its internal API. By default requests are same-origin
`fetch` calls from an owned Microsoft Edge tab at
`https://app.notion.com/robots.txt`, matching SecretScript's transport approach.
Edge supplies its normal cookies and browser network stack; responses stream
incrementally. There are no chat-UI clicks or page scraping in the inference path.
The app uses a dedicated persistent profile and a loopback-only debugging port.
Session cookies are imported locally and refreshed cookies remain DPAPI-encrypted
in the session store. Existing browser profiles are not imported or modified.

Login before a verified account session exists still uses the native API.
Settings also offers an explicit native HTTP transport. An Edge failure does not
silently switch transports or resend inference.

## One account and one main

Open **Account & workers**. Connect an existing account, then use
**Prepare main & workers** to provision one dedicated main and five real
Welcome to Notion workers. **Welcome worker** adds a chat in that same account.
No eight-account bootstrap is started. New installations have one account slot.

When upgrading an existing multi-account installation, choose one saved account
in the selector. Other accounts, chats and sessions are preserved, but only the
selected account is scheduled or connected to harness tools. Existing uncertain
requests in that account are reconciled before their chat can be reused. Changing
accounts while a selected harness is active is blocked. Old extra main chats stay
in storage; the account's dedicated main is shown in this harness.

## Context renewal

Main and worker chats have independent context estimates and generations.
Defaults: renew near **60,000 estimated tokens** or **80 submitted turns**.
These are configurable local heuristics, not Notion's context limit or a
measurement of Edge RAM. A background read collects native transcript snapshots;
the complete saved workflow message list is archived before compaction.

Renewal happens between assignments. It never cancels a running inference, drops
an unresolved tool action, or moves an ambiguous request into a new chat.

1. Build source-linked notes from the current goal, user corrections, durable
   memory, dependencies, file paths, task status, evidence and observed history.
2. Ask the old chat for a structured semantic checkpoint, including constraints,
   decisions, completed work, open work, evidence, files and next steps.
3. Recover and verify that exact committed summary turn and its explicit model.
   Invalid or unconfirmed summaries keep the original chat intact. Unknown
   submissions are read back, never blindly posted again; restart retains this
   ledger.
4. Reserve an ID and create a real Welcome to Notion replacement. An uncertain
   creation is reconciled against that ID rather than duplicated.
5. Atomically update the generation, account ledger and logical worker. The main
   chat, assignment leases and coordinator references keep their logical IDs.
   Inject the checkpoint into the first assignment of the new generation.

Complete local reports, memory entries and archived native messages remain
available through the **context** MCP tool, including `thread_id` and paginated
`offset` reads. Existing working memory is preserved as a baseline. Native Notion
saved memories are enabled for reading; harness conversations remain excluded
from automatic memory writes. Local memory entries retain the full original text.
Summaries can still omit details, so source records remain authoritative.

The local UI initially renders only the latest 100 messages; older messages can
be loaded explicitly. It does not open heavy native Notion chat pages for work.

## Development and verification

```powershell
python -m unittest discover -s tests -q
node --check notiontool/web/app.js
python -m PyInstaller --noconfirm NotionTool.spec
```

`tests/smoke_edge_readonly.py` checks the Edge tab, identity and chat list using an
isolated copy of one saved session. It never submits inference or creates chats.
`tests/preview_harness.py` serves a separate UI fixture without live credentials.

The ordinary model availability checks, account cooldowns, request reconciliation,
resource locks and project-scoped MCP capabilities remain enforced. Account limits
still determine usable concurrency. Paid plans or trials are not activated by
context renewal.


## Harness (Multi-Main / Host API)
See docs/HARNESS_MULTI_MAIN.md and docs/HOST_API.md.
