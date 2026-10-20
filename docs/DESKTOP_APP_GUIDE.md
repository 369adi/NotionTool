# NotionTool App - user guide

## What it is
The NotionTool App is a second Notion desktop window. It runs the Notion desktop app you
already have installed, but with its own profile (`%LOCALAPPDATA%\NotionTool-App\profile`)
and the NotionTool layer on top: a sidebar with only your Main chats, a Live tab, harness
settings and a few helper banners.

Your normal Notion app and its profile stay untouched. Both windows can run side by side.
Everything you click in the NotionTool App still runs in the real Notion client against
your real Notion account.

## Starting the app
The NotionTool harness must be running first. Then start the app with either of these:

- Double-click the desktop shortcut **NotionTool App**.
- Or send `POST /api/app/start` to the local harness server (127.0.0.1, with your usual
  Bearer token).

The harness opens a separate Notion window and connects to it. Wait a few seconds until
the NotionTool sidebar and the Live button appear.

## First start: log in once
The app uses its own profile, so on the first start Notion shows its login screen.
Log in once in that window with the **same Notion account** you use for the harness.
The login is stored in the app profile and is not needed again on later starts.
The NotionTool layer only becomes active after you are logged in.

## Look and feel
The NotionTool layer gives the window its own dark design: a deep background (#0b0d12),
glass panels, rounded corners (12 px) and a violet-to-cyan accent gradient
(#7c5cff to #22d3ee) for the logo, primary buttons and the active chat. The tab title starts
with "NotionTool". Notion's upgrade and AI-credit banners are hidden.

## Layout
- **Sidebar:** starts with the NotionTool header. Your Main chats are listed under
  **Mains** as cards; the open chat has an accent bar and the operator Main an
  **Operator** pill. The "New agent" tile and the date headings (Today, Yesterday, ...) are
  hidden. The sidebar shows only your Main chats. Coworker chats are hidden (see the setting
  *Sidebar chats* if you want all chats except coworkers).
- **New chat** (bottom left): creates a Welcome chat for the harness, never a plain Notion
  chat. The Welcome chat becomes a Main chat once you start working in it.
- **Hidden items:** Home, Meetings, What's new, Templates, Marketplace and Inbox are hidden.
  The account switcher (top left) and the real Notion settings stay available.

- **Action bar** (bottom left): the **Live** button, a red badge with the number of items
  that need attention and a status dot (green while the harness sends updates).
- **Composer:** the chat input is a floating rounded card that glows when focused.

## Live tab
Open it with **Ctrl+Shift+L** or the **Live** button. It slides in from the right as a
dashboard with tiles for Running, Ready, Recovering and Blocked, and shows:

- harness status and the request budget (requests per minute),
- runs with their state, goal and number of open tasks,
- one card per coworker: current task, time since the last tool step and a context meter
  (green below 70 %, amber below 90 %, red above),
- items that need attention (for example runs waiting for the Main),
- Main switches and the latest 50 events.

Close it with **Esc**, the **Close** button or Ctrl+Shift+L again.

## NotionTool settings
The harness settings live inside the normal Notion settings window, in their own
**NotionTool** section. You can also open them directly with **Ctrl+Shift+,**.
The window has a section list on the left (General, Mains & Coworkers, Desktop app,
Advanced). The footer counts unsaved changes; **Save changes** or **Ctrl+S** saves them.
Settings marked **This device** are stored only in the app.
There you set harness options (limits, auto-continue and similar) and the app options
(which chats the sidebar shows, which Notion items are hidden). A few Notion settings
sections the harness does not use (for example Meeting notes, Sites, Import) are hidden;
account, security, billing, Notion AI and connections are never hidden.

## Main switch banner
When a Main chat grows very large (many tokens or user turns), the harness asks before
switching to a fresh Main. A banner appears at the top of the window. Confirm to start the
switch, or dismiss it to keep working in the current Main. Nothing switches without you.

## Command palette
Press **Ctrl+K** to open the command palette, type a few letters and press **Enter**.
Commands: Open Live, Open NotionTool settings, New Welcome chat, Switch to a fresh Main
chat, Emergency stop on/off, Refresh harness state, Refresh MCP tools, and Pause or resume
for each run. **Esc** or a click outside closes it. When text is selected in an editable
field, Ctrl+K keeps Notion's "add link" function.

## Hotkeys

| Keys | Action |
|---|---|
| Ctrl+K | Open the command palette |
| Ctrl+Shift+L | Open or close the Live tab |
| Ctrl+Shift+, | Open or close NotionTool settings |
| Ctrl+S | Save changes (inside NotionTool settings) |
| Esc | Close the palette, the Live tab or the settings |
| Ctrl+Alt+Shift+N | Emergency stop: turn the NotionTool layer off or on |

## Emergency stop
**Ctrl+Alt+Shift+N** removes the NotionTool layer from the window at once. You get the
plain Notion UI back (all chats, all menu items), without closing the app or logging out.
Press it again to turn the layer back on. Use it whenever something looks wrong.

## Troubleshooting

**"Harness not connected" banner**
The window cannot reach the harness. Check that the harness is running, then wait a
moment; the banner disappears by itself when the connection is back. If it stays,
close the app window and start it again from the shortcut.

**Buttons moved or missing after a Notion update**
Notion updates can change its layout, so the layer may hide the wrong item or miss one.
Press the emergency stop (Ctrl+Alt+Shift+N) to get the plain Notion UI back and keep working.
Then report what you saw (Notion version and a screenshot) so the layer can be fixed.

**Port 9333 is busy**
The app talks to its Notion window through the local port 9333. If another program or an
old NotionTool App window still uses it, the start fails with a timeout. Close the old
NotionTool App window (not your normal Notion) and start again. If another program needs
that port, ask for the app port to be changed in the harness setup.

**The app shows the login screen again**
The app profile was reset or deleted. Log in once more with the same account.

## Safety notes
- The DevTools port is only opened on **127.0.0.1**; other computers cannot reach it.
  The harness refuses connections to any other host.
- No cookies, passwords or tokens are copied out of Notion. The layer only reads what is
  on screen and calls the local harness.
- The normal Notion app and its profile are never changed, patched or closed.
- For private use on your own PC only. Do not share or distribute the app; changing the
  Notion client is against Notion's terms, so there is an account risk.
- Hidden coworker chats still exist in your Notion account. They are only hidden in this
  window and stay visible in the normal Notion app and on other devices.
