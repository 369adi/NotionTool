# Git in NotionTool

NotionTool is a git repository (branch `main`). Only source, tests and docs are committed.

## Never committed (see `.gitignore`)

| Path / pattern | Why |
| --- | --- |
| `data/` | Local database, Notion sessions (`data/sessions`: Notion cookies, the host API key), browser profiles with Notion cookies, handoffs, snapshots, `desktop_app.json`. |
| `browser-profile(s)/`, `sessions/`, `profile/` anywhere | Browser and login data. |
| `work/`, `dist/`, `build/`, `bench/out/`, `bench/results/`, `bench/*.json`, `bench/*.csv` | Work outputs, backups, captures and builds. |
| `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.test-data/` | Caches and test output. |
| `*.db`, `*.sqlite*`, `*.log`, `*.out` | Databases, logs, process output. |
| `.env*`, `*.key`, `*.pem`, `*.pfx`, `*.p12`, `*secret*`, `*token*.json/txt`, `*cookie*`, `*credential*`, `host_api*.json` | Anything that may hold a token, key or cookie. |

The app token and the host API key are created at start and kept in `data/sessions` (ignored). The Notion desktop profile lives in `%LOCALAPPDATA%\NotionTool-App`, outside the repo.

## Line endings (see `.gitattributes`)

- Default: LF (`* text=auto eol=lf`).
- CRLF on purpose: `notiontool/server.py`, `notiontool/devtools.py`, `notiontool/chat_context.py`.
- A few older files are still CRLF; they are listed in `.gitattributes` so git keeps their bytes. Convert one to LF only on purpose and then remove its line.
- `README.md` has mixed endings and is stored as is (`-text`).
- Images, fonts, archives and executables are binary.
- `core.autocrlf` is `false` in this repo. Check with `git ls-files --eol`.

## How to commit

1. `git status` - check that only your files changed and nothing from `data/` or `work/` shows up.
2. `git add <your files>` (avoid `git add -A` while other runs are editing).
3. Check for secrets: `git diff --cached` must not contain tokens, cookies, `token_v2`, `Bearer` keys or personal emails.
4. `python -m unittest discover -s tests` (or the tests for your part).
5. `git commit -m "Short plain message"`.

Do not push without a separate decision. If a secret was committed by mistake, do not push; remove it from the commit and rotate the secret.
