# Security audit: public MCP route and Host API auth

Phase 10 (Hardening), batch 1, Part B. Docs only; no code was changed.
Evidence was read from the source tree on 16.10.2026. Line numbers refer to the
files as they were at that time (server.py and bridge.py are being edited by other
runs, so lines may shift). Secrets are named only by variable or setting name.
Points that could not be confirmed from code are marked UNKNOWN.

Credentials referenced in this document (names only):

- `host_api_key`: operator key, created once and persisted in the session store (server.py:68-71).
- `bridge_token`: MCP key handed to the Notion MCP connection, persisted in the session store (server.py:65-67, server.py:498).
- `app.token`: per-start UI token sent as header `X-NotionTool-Token`, saved to the runtime session (server.py:64, server.py:222).
- `assignment_token`: the lease of a running coworker task (bridge.py:204-215).
- `gateway.control`: random control value of the gateway process (gateway.py:64), written to `runtime/gateway.json` (gateway.py:230-231).

## 1. Host API on 127.0.0.1:8790

- Default port and bind: settings defaults `host_api_port=8790`, `host_api_allow_network=False`, `host_api_bind='127.0.0.1'` (store.py:193).
- Bind address: `127.0.0.1` unless `host_api_allow_network` is true; then the value of `host_api_bind` is used without validation (server.py:207-210). The listener is created at server.py:214. If the port is busy, a random port is used on the same bind address (server.py:215-217).
- Both settings can be changed through the authenticated settings route (server.py:633-638). `host_api_bind` is only trimmed to 64 characters (server.py:638); a value such as `0.0.0.0` is accepted. UNKNOWN: whether the UI warns before saving.
- Bearer check: every `/api/` request needs either an Authorization header equal to the word Bearer plus `host_api_key`, or `X-NotionTool-Token` equal to `app.token`. Both use `hmac.compare_digest` (server.py:129-132). Missing or wrong values get 401 (server.py:132).
- Origin check: applied only when the request did not use the bearer and an `Origin` header is present (server.py:133-135). Allowed origins are `http://127.0.0.1:<port>` and `http://localhost:<port>` (server.py:241-242). Requests without an `Origin` header that carry the UI token are accepted (server.py:134).
- The same handler also serves `/mcp` (server.py:110-127), so the MCP route with operator tools is reachable on this port too. See section 2 for its checks.
- Security headers: `X-Content-Type-Options` and a strict `Content-Security-Policy` are sent on responses (server.py:95-96).
- Health reports `bind` and `network_access` (server.py:261-262).

## 2. MCP listener (observed on port 53768)

- Port: there is no fixed 53768 in the code. The listener uses setting `public_mcp_port` (default 8873) and falls back to any free port, which is then saved back into `public_mcp_port` (server.py:775-776). 53768 is the saved fallback port observed in docs/CAPABILITIES.md:8 and docs/ARCHITECTURE_AUDIT.md:8.
- Bind address: always `127.0.0.1` (server.py:775-776), with exclusive address use (server.py:25-32).
- Path restriction: the listener is flagged `mcp_only` (server.py:777), so every path except `/mcp` returns 404 (server.py:108-109). The Funnel prefix `/nt-harness/` is stripped first (server.py:106).
- Bearer check: the Authorization header must equal Bearer plus `host_api_key` (operator scope) or Bearer plus `bridge_token` (normal scope), compared with `hmac.compare_digest` (server.py:111-114). Anything else gets 401 (server.py:114).
- Scope: only the operator key passes an operator object into the bridge (server.py:121). Operator tools are listed and callable only in that case (bridge.py:536-537, bridge.py:541-542).
- Origin check: none on `/mcp` (server.py:110-127).
- Methods: GET gets 405, DELETE gets 200 (server.py:115-118). Errors return 400 or 500 without dropping the connection (server.py:123-127).
- Its port is published to the gateway in `runtime/core.json` (server.py:780-781, gateway.py:50-52).

## 3. MCP gateway on 127.0.0.1:8792

- Port: `DEFAULT_PORT = 8792` (gateway.py:22), overridable by setting `mcp_gateway_port` (server.py:808). It can be switched off with `mcp_gateway=False` (server.py:807).
- Bind address: always `127.0.0.1`, exclusive address use (gateway.py:216-225).
- Bearer check: the gateway does not check credentials itself. It forwards the `authorization` header and a fixed set of other headers (gateway.py:26, gateway.py:115) to the core MCP listener on 127.0.0.1 (gateway.py:78), which applies the check from section 2.
- Backup mode (core down): only callers whose sha256 of the Authorization value was previously accepted by the core get cached answers (gateway.py:114, gateway.py:127-129, gateway.py:145-146). An empty Authorization is never added (gateway.py:127). The cache is keyed by RPC method only, not by caller (gateway.py:130-131, gateway.py:152-157).
- Tool calls are never resent once they may have reached the core (gateway.py:121-122, gateway.py:135-141).
- Origin check: none (gateway.py:191-205).
- `/__gateway` (health) needs no credentials and returns pid, target port and cached method names (gateway.py:167-169, gateway.py:195-196).
- `/__gateway/stop` requires client address 127.0.0.1 and header `X-Gateway-Control` equal to `gateway.control`, compared with `!=` (gateway.py:198-199).
- Request bodies are read in full according to `Content-Length` with no upper limit (gateway.py:192-193).

## 4. Tailscale bridge (Funnel)

- Exposure: Tailscale Funnel publishes a local target to the public internet on port 443, 8443 or 10000 (tailscale_bridge.py:33-36). The target is `http://127.0.0.1:<port>` (tailscale_bridge.py:31), where the port is the gateway port, or the MCP listener port if no gateway runs (server.py:849-851).
- When all Funnel ports are taken, the route is mounted under `/nt-harness` (tailscale_bridge.py:44-54); the core strips that prefix (server.py:106).
- Bearer check: none in the bridge itself. Authentication relies on the core MCP check (server.py:111-114), reached through the gateway (section 3).
- Origin check: none (no code found in tailscale_bridge.py or on the `/mcp` path, server.py:110-127).
- The public URL is stored in setting `public_mcp_url` (tailscale_bridge.py:64-65) and handed to Notion together with `bridge_token` (server.py:498, server.py:889).
- Both `host_api_key` and `bridge_token` are accepted on the public route (server.py:112-113), so the operator scope, including nt_shell, is reachable from the internet with the operator key.
- Rate limiting or lockout after failed attempts to authenticate: none found in server.py:110-114 or gateway.py:110-133. UNKNOWN whether Tailscale applies any.
- The gateway sees Funnel traffic from 127.0.0.1 (tailscaled proxies locally, target at tailscale_bridge.py:31). So the client-address part of the stop check (gateway.py:198) does not separate local from public callers. UNKNOWN: whether Tailscale adds a header that could identify Funnel requests; no code reads one.

## 5. Tool nt_shell

- Defined as an operator dev tool (devtools.py:31) and merged into `OPERATOR_TOOLS` (bridge.py:116-120).
- Who can call it: only an MCP caller with the operator key (server.py:112, server.py:121, bridge.py:541-542). `bridge_token` callers do not see or reach it (bridge.py:536-537).
- Behaviour: runs any PowerShell command with `-ExecutionPolicy Bypass` (devtools.py:103-105), timeout up to 1800 s (devtools.py:101), cwd from `_path` which accepts absolute paths (devtools.py:102, devtools.py:45-47). There is no command allowlist and no confirmation step.
- Environment: PyInstaller variables are removed before start (devtools.py:109-110). Output is capped (devtools.py:115-116).
- Related coworker tool: `run_shell` is reachable with `bridge_token` plus a valid `assignment_token` (bridge.py:556-559, bridge.py:245-262). Its folder is the run's project folder, or the home folder with absolute paths when setting `mcp_full_access` is on (bridge.py:129-130, bridge.py:223-242).
- UNKNOWN: whether `shell_gate` (bridge.py:125) limits nt_shell; it was not traced to the dev call (bridge.py:455-460).

## 6. Tool nt_write_file

- Defined as an operator dev tool (devtools.py:33), operator scope only (bridge.py:455-460, bridge.py:541-542).
- Before the write, the target is registered as an operator edit (bridge.py:456-459).
- Path: `_path` expands environment variables and accepts absolute paths, so any file the app user can write is reachable (devtools.py:45-47, devtools.py:128). There is no root containment check.
- Safety: optional `expected_sha256` stops overwriting newer content (devtools.py:129-131); a backup is kept (devtools.py:132). It writes with `newline=''`, so line endings are kept (devtools.py:135).
- Related coworker tool `write_file` uses the bridge path check (bridge.py:234-242): limited to the project folder unless `mcp_full_access` is on.

## 7. Tool nt_notion_call

- Operator scope only (bridge.py:100, bridge.py:541-542).
- Method name must match a 3-64 letter pattern (bridge.py:487-488); any internal API method that fits is allowed, including write methods.
- Account: `account_id` argument or the selected harness account (bridge.py:483-485).
- `save_to` is checked before the Notion call (bridge.py:489-491): no absolute paths, no `..`, and the result must stay below `work/` (bridge.py:419-437). The write is atomic (bridge.py:443-446).
- Body content is passed as given (bridge.py:491). UNKNOWN: whether network_budget limits apply on this path.

## 8. Gaps

| ID | Severity | Evidence file:line | Impact | Proposed fix |
| --- | --- | --- | --- | --- |
| G1 | High | server.py:112-113, server.py:121, bridge.py:541-542, tailscale_bridge.py:31 | The operator key is accepted on the public Funnel route, so anyone with `host_api_key` gets remote nt_shell, nt_write_file and nt_notion_call from the internet. | Accept only `bridge_token` on the `mcp_only` listener by default; make operator scope over Funnel an explicit setting, and use a separate operator MCP key. |
| G2 | High | server.py:207-210, server.py:636-638 | `host_api_bind` accepts any address (for example all interfaces). The same listener serves `/mcp` with operator tools (server.py:110-121), exposing shell access to the LAN. | Allow only loopback or the Tailscale interface address; reject wildcard binds or require a second confirmation; never serve operator `/mcp` on a non-loopback bind. |
| G3 | Medium | server.py:65-67, server.py:68-71 | `bridge_token` and `host_api_key` are long-lived and never rotated; a leak stays valid until manual reset. | Add rotation (UI action and on account switch) and reconnect Notion with the new value. |
| G4 | Medium | server.py:113-114, gateway.py:110-133 | No rate limit or lockout on failed attempts to authenticate on a public endpoint. | Count 401s per source and back off; log failures as events. |
| G5 | Medium | devtools.py:101-105, devtools.py:45-47 | nt_shell runs any PowerShell with execution policy bypass and up to 30 min; with G1 this is remote code execution. | Log every command as an event, optional allowlist or operator confirmation for destructive commands, lower default timeout. |
| G6 | Medium | devtools.py:45-47, devtools.py:128 | nt_write_file can write anywhere (absolute paths, environment variables). | Restrict to the project root plus an explicit allowlist; reject paths outside unless a full-access setting is on. |
| G7 | Medium | bridge.py:228-241, plain_protocol.py:204 | With `mcp_full_access`, a `bridge_token` caller with a lease gets shell and file access to the whole home folder; leases travel in prompt text. | Keep the default off, warn in Settings, shorten lease lifetime, bind leases to the worker thread. |
| G8 | Medium | gateway.py:130-131, gateway.py:152-157 | The gateway cache is keyed by method only; during an outage a `bridge_token` caller can receive the operator `tools/list` cached from an operator call (information disclosure, no execution). | Key the cache by the sha256 of the Authorization value. |
| G9 | Medium | gateway.py:192-193 | No cap on request body size; a large Content-Length can exhaust memory. | Reject bodies above a fixed limit (for example 8 MB) with 413. |
| G10 | Low | gateway.py:195-196, gateway.py:167-169 | `/__gateway` health is public through Funnel and reveals pid and internal port. | Answer only without a Funnel path, or require `X-Gateway-Control`; return minimal data. |
| G11 | Low | gateway.py:198, gateway.py:230-231 | Stop check compares with `!=` (not constant time) and the client-address check cannot tell Funnel traffic from local traffic. | Use `hmac.compare_digest`; reject stop requests that arrive via Funnel. |
| G12 | Low | server.py:110-127 | No Origin validation on `/mcp` (DNS rebinding protection relies on the bearer only). | Reject requests whose Origin is set and not in an allowlist. |
| G13 | Low | bridge.py:487-491 | nt_notion_call allows any matching method, including destructive internal API writes. | Allowlist read methods; require an explicit flag for write methods. |
| G14 | Low | server.py:222, gateway.py:230-231 | UI token and gateway control value are stored in runtime files; encryption at rest is UNKNOWN (DPAPI is only stated for MCP server entries, mcp_servers.py:1). | Store via the encrypted session store or restrict file ACLs to the current user. |

## 9. Open points (UNKNOWN)

- Whether the Settings UI warns before saving a non-loopback `host_api_bind` (server.py:636-638).
- Whether `shell_gate` (bridge.py:125) applies to nt_shell.
- Whether network_budget limits nt_notion_call (bridge.py:491).
- Whether Tailscale marks Funnel requests with a header that the app could check.
- Encryption at rest of the session store used at server.py:67 and server.py:71.
