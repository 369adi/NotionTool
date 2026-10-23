# NotionTool Harness – Feature-Review (Stand 23.10.2026)

Dieses Dokument bewertet alle großen Features der Harness mit Noten von 0 bis 10.

- **Funktion**: Wie gut klappt das Feature wirklich (live, in der DB, in Tests)?
- **Code**: Wie gut ist der Code gebaut (Aufbau, Fehlerbehandlung, Tests, Doku)?
- **Gesamt**: Mittelwert, leicht nach Wichtigkeit gewichtet.

## Methode und Beweis-Arten

Jede Aussage hat eine Quelle:

- **LIVE** = echte App (`/api/health`, `/api/coordination`) oder Vorfälle von heute.
- **DB** = Lesezugriff auf `data/notiontool.db` (nur `mode=ro`).
- **TEST** = Testlauf von heute.
- **CODE** = Code-Statistik (Längen, `except`-Blöcke, Zeilenenden).
- **DOC** = Dokumente in `docs/`.

Wichtig: Ich habe den Code nicht Zeile für Zeile gelesen. Die Noten für Code stützen sich auf Statistik, Tests und Doku. Wo ich wenig Beweise habe, steht **(wenig geprüft)**.

### Harte Zahlen

| Punkt | Wert |
|---|---|
| Python-Module im Paket | 122 Module, 40.485 Zeilen |
| Testdateien | 290 Dateien, 48.102 Zeilen |
| unittest (voller Lauf) | **3192 Tests, 20 rot, 8 übersprungen, 740 s** |
| pytest `-x` | 1 rot, 1 Fehler, 179 grün (bricht beim 1. Fehler ab) |
| Runs gesamt | 202 (done 136, cancelled 62, blocked 3, awaiting_main 1) |
| Tasks gesamt | 1254 (done 1135 = 90,5 %, failed 24, blocked 18, cancelled 74) |
| Task-Dauer | Median 126 s, p90 641 s, Maximum 88.646 s (~24,6 h) |
| DB-Größe | **215 MB**, davon `thread_context` 173 MB |
| Patch-Modus heute | 17 gemergt, **14 Konflikte warten** |
| Netz-Budget live | 180/min Limit, 6 im Fenster, parallel 5/5, keine offenen Circuits |
| Git | Arbeitsbaum sauber, letzter Commit 23.10. 22:24 |

### Rote Tests im vollen unittest-Lauf (TEST)

- `test_backtest_limits`: test_1, test_1b, test_2, test_3a, test_3b, test_5 (6 Stück)
- Pool-Tests: eight_slots_five_workers, global_limit_twelve, one_disabled_slot, per_slot_cap_three, recovering_worker_not_reused, signed_out_slot_excluded, two_main_chats_fairly_share (7 Stück)
- `test_engine.test_failed_review_never_marks_done` ← **kritisch**
- `test_confirmed_creation_is_recovered_without_duplicate` ← **kritisch**
- `test_fix_rounds_limit_unchanged_and_main_decides`
- `test_failed_attempt_records_its_error_class`
- `test_long_calls.test_sync_limit_setting_is_clamped`
- `test_page_defaults.test_page_shows_the_server_defaults`
- `test_problems.test_server_route_and_install_keep_crlf`

Die Ursache habe ich nicht geprüft. Ein Teil kommt wohl daher, dass die echte App auf Port 8790 lief: Der Guard-Test meldet genau diesen Port und die echte DB. Das ist trotzdem ein Fehler, denn Tests dürfen nicht von der laufenden App abhängen.

---

## Übersicht

| # | Feature | Funktion | Code | Gesamt | Kurz-Grund |
|---|---|---|---|---|---|
| 1 | Edge-Transport, Tab-Wake, Session-Store | 7 | 7 | **7,0** | Läuft live stabil, hängt aber an Notion-Interna |
| 2 | Accounts, Slots, Failover | 7 | 6 | **6,5** | Live ok, aber 7 Pool-Tests rot |
| 3 | Chat-Pool, Welcome-Worker, Namen | 6 | 6 | **6,0** | Namen klappen, Chat-Liste ohne Titel seit Notion v23.13 |
| 4 | Main-Wechsel und Handoff | 4 | 6 | **5,0** | Ack klappt, Archiv-Umbenennung und Chat-Alter kaputt |
| 5 | Multi-Main und Host-API | 7 | 6 | **6,5** | Läuft, Sicherheitslücken G1/G2 offen oder ungeklärt |
| 6 | Delegation, Plan, DAG, Teams | 7 | 6 | **6,5** | 90 % der Tasks fertig, Funktionen sehr lang |
| 7 | Engine (Runs, Review, Bestätigung) | 6 | 5 | **5,5** | 30 % Abbrüche, `_complete` 329 Zeilen, Review-Test rot |
| 8 | Netz-Budget und Koordination (I11) | 8 | 7 | **7,5** | Live sauber, gute Idee, aber 6 Limit-Tests rot |
| 9 | Modell-Routing und Effort | 4 | 6 | **5,0** | Welcome-Chats nutzen das Wunsch-Modell oft nicht |
| 10 | Context-Erneuerung | 5 | 6 | **5,5** | Main 20 bei ~4,1 Mio Tokens, DB-Blobs wachsen |
| 11 | Coworker-Alter (23 h), Token-Limit, Upkeep | 5 | 6 | **5,5** | Coworker mit 27–41 h noch da |
| 12 | Recovery (kein blindes Neusenden) | 6 | 7 | **6,5** | Gutes Prinzip, aber 125 turn_stalled in 5 h |
| 13 | Verifier, Proof, Fix-Loop | 6 | 6 | **6,0** | Läuft (65 Prüfungen), Fix-Runden-Test rot |
| 14 | Patch-Modus, Leases, EOL-Guard | 5 | 6 | **5,5** | 14 Konflikte warten |
| 15 | MCP-Bridge, Gateway, Tools, Sicherheit | 8 | 5 | **6,0** | Tools top, Sicherheit schwach |
| 16 | Desktop-App (CDP 9333), Overlay, Web-UI | 6 | 5 | **5,5** | Viel Funktion, riesige JS-Dateien, alte .bak-Dateien |
| 17 | Screen-Capture | 6 | 6 | **6,0** | (wenig geprüft) |
| 18 | Telemetrie, Feed, Problems | 7 | 7 | **7,0** | Viele Events, aber nur ~5 h Verlauf |
| 19 | Backup, Updater, Restart, Startup | 6 | 6 | **6,0** | (wenig geprüft) |
| 20 | Tests und fake_notion | 7 | 7 | **7,0** | Sehr viele Tests, aber 20 rot und Fake ≠ echt |
| 21 | Doku und Sicherheit gesamt | 5 | 5 | **5,0** | Ehrliche Doku, README veraltet, Audit-Lücken offen |

**Gesamtnote der Harness: 6,1 / 10**

Kurz gesagt: Die Harness kann sehr viel und ist gut getestet. Der Kern läuft im Alltag (Transport, Netz-Budget, Delegation). Die Schwächen liegen bei den Rändern: Main-Wechsel, Context-Grenzen, Patch-Konflikte, Sicherheit und rote Tests.

---

## 1. Edge-Transport, Tab-Wake, Session-Store – 7,0

**Was es macht:** Schickt alle Notion-Aufrufe über einen echten Edge-Tab (same-origin fetch, R4). Weckt den Tab, wenn er schläft. Speichert die Session mit DPAPI. Hat einen nativen Notfall-Weg und `window_login`.

**Wo:** `edge_transport.py` (Tab-Wake Z. 34–66), `vendor/transport.py`, `window_login.py`.

**Beweis:** LIVE `/api/health` ok, transport `edge`, Konto bereit. DB: `push_slow` 242-mal in ~5 h.

**Stark:**
- Läuft im Alltag stabil.
- DPAPI für die Session ist sauber.

**Schwach:**
- `push_slow` kommt sehr oft. Das bremst jeden Turn.
- Die Datei hat CRLF und ist damit schwer zu patchen (R8).
- Laut Doku gibt `vendor/transport.py` das Budget-Ticket nie frei.

**Vorschlag:** Ursache für `push_slow` messen (Tab-Wake oder Netz?). Den Ticket-Release in `vendor/transport.py` nachziehen.

## 2. Accounts, Slots, Failover, Health – 6,5

**Was es macht:** Verwaltet mehrere Konten und Slots, wechselt bei Fehlern, hat Cooldowns und einen `ai_disable_detector`.

**Beweis:** LIVE Konto 06e0… (Slot 2) bereit. TEST: 7 Pool-Tests rot (z. B. `one_disabled_slot_does_not_stall_other_seven`, `signed_out_slot_is_excluded…`).

**Stark:** Klare Regel, dass Turns nur auf erlaubten Konten laufen.

**Schwach:** Gerade die Tests für Ausfall-Fälle sind rot. Ob Failover heute noch richtig klappt, ist also nicht sicher belegt.

**Vorschlag:** Die 7 Pool-Tests einzeln und ohne laufende App ausführen und die Ursache finden.

## 3. Chat-Pool, Welcome-Worker, Coworker-Namen – 6,0

**Was es macht:** Hält einen Pool aus Welcome-Chats. Gibt Coworkern Namen nach dem Muster „Coworker N“ (I41). Merkt sich eigene und alte Chats.

**Wo:** `chat_pool.py`, `cowork_names.py`, `own_chats`, `retired_chats`.

**Beweis:** DB `coworker_renamed` 20, `coworker_old_chat_deleted` 21.

**Stark:** Umbenennen und Löschen alter Coworker-Chats klappt meistens.

**Schwach:**
- Seit Notion v23.13 fehlt `getInferenceTranscriptsForUser`. Die Chat-Liste hat keine Titel mehr. Darauf bauen andere Features auf (siehe 4).
- Pool-Tests rot (siehe 2).

**Vorschlag:** Chats nach eigener ID-Liste verwalten und nicht nach der Notion-Liste suchen.

## 4. Main-/Operator-Wechsel und Handoff – 5,0

**Was es macht:** Wechselt den Main-Chat. Schreibt eine Handoff-Datei mit Prüfwort (8 Hex-Zeichen). Benennt den alten Main in „Archive“ um. Richtet Wake, Refresh und Repoint neu aus.

**Wo:** `main_switch.py` (761 Z., `_advance` 92 Z.), `main_handoff_ops.py` (697 Z.), `main_wake.py`.

**Beweis:**
- DB: `handoff_file_written` 13, `handoff_acked` 9.
- LIVE: `main_handoff_rename_refused` 3-mal mit „chat not listed“. Der alte Main heißt nicht „Archive“.
- LIVE: Der neue Main zeigt ein falsches Alter von 53 h, weil `chat_age` auf `chat.created` zurückfällt.

**Stark:** Prüfwort-Ablauf ist klug und klappt. Rollback ist vorgesehen.

**Schwach:**
- Zwei sichtbare Fehler bei jedem Wechsel (Archiv, Alter).
- Die Tests sind grün, aber der Fake liefert Titel, die echte Notion-API nicht mehr. Die Tests prüfen also eine Welt, die es nicht mehr gibt.

**Vorschlag:** Wird vom echten Main behoben. Dazu einen Live-Backtest (I42) für „Wechsel → altes Chat heißt Archive → Alter neu“ anlegen.

## 5. Multi-Main und Host-API – 6,5

**Was es macht:** Mehrere Mains parallel. Zähler bleibt nach Neustart erhalten. Idempotentes Senden über `request_id`. Host-API mit Bearer-Token und SSE.

**Beweis:** DOC `HARNESS_MULTI_MAIN.md`, LIVE API antwortet nur auf 127.0.0.1.

**Stark:** `request_id` gegen Doppel-Senden ist richtig gedacht. Log-Redaction ist da.

**Schwach:** Laut Audit: kein Token-Wechsel (G3), kein Limit bei falschen Tokens (G4), Vergleich nicht konstant in der Zeit (G11). Ob das inzwischen behoben ist, habe ich nicht geprüft.

**Vorschlag:** Audit-Lücken G1–G14 als Liste mit Status (offen/zu) im Repo führen.

## 6. Delegation, Operator-Plan, DAG, Teams – 6,5

**Was es macht:** `/api/operator/plan` und `/api/operator/delegate`. Zerlegt Arbeit in einen DAG und verteilt sie auf Rollen und Teams (Größe, Wachstum, Schwierigkeit).

**Wo:** `operator_main.py` (`delegate` 149 Z.), `delegation.py` (`validate_delegation` 101 Z.), Task-Router, Team-Module.

**Beweis:** DB: 1254 Tasks, 90,5 % fertig. Rollen: executor 808, reviewer 288, planner 62, context_compactor 41, integrator 36, architect 19.

**Stark:** Hohe Abschlussquote. Review ist fest eingebaut (fast jeder 3. Task ist Review).

**Schwach:** Sehr lange Funktionen. Planner 62 gegen 808 Executor zeigt: wenig echte Planung, viel direktes Ausführen.

**Vorschlag:** `delegate` und `validate_delegation` in kleine Schritte zerlegen (prüfen, bauen, senden).

## 7. Engine: Runs, Tasks, Review, Bestätigung – 5,5

**Was es macht:** Führt Runs aus, plant Ticks, startet Reviews, hat Reviewer-Fallback, Status `awaiting_main` und `/api/run/confirm`.

**Wo:** `engine.py` (2327 Z., `_complete` 329 Z., 23 breite `except`).

**Beweis:**
- DB: 202 Runs, 62 abgebrochen (~30 %).
- TEST: `test_failed_review_never_marks_done` ist **rot**.
- Task mit 24,6 h Laufzeit.

**Stark:** Drei-Stufen-Abnahme (Maschine → Main → done) ist ein gutes Konzept.

**Schwach:**
- Wenn der rote Test einen echten Fehler zeigt, kann ein Task trotz schlechtem Review „done“ werden. Das ist das größte Risiko im Kern.
- `_complete` mit 329 Zeilen ist kaum zu prüfen.
- Die hohe Abbruch-Quote ist nicht erklärt.

**Vorschlag:** Den Review-Test sofort klären. `_complete` aufteilen. Abbrüche nach Grund zählen (Nutzer, Timeout, Limit).

## 8. Netz-Budget und Request-Koordination (I11) – 7,5

**Was es macht:** Ein globales Budget mit 180 Anfragen pro Minute (gleitendes Fenster). Prioritäten, anpassbare Parallelität, Backoff, Circuit-Breaker pro Endpoint, Pause bei 401/403.

**Beweis:** LIVE: Limit 180, 6 im Fenster, parallel 5/5, keine offenen Circuits. TEST: 6 Tests in `test_backtest_limits` rot.

**Stark:** Das am besten durchdachte Feature. Die Doku nennt offen ihre Lücken.

**Schwach:** Die Backtest-Tests sind rot, vermutlich wegen der laufenden App (Guard meldet Port 8790). Ticket-Release fehlt bei einem Fehler in `_start`.

**Vorschlag:** Backtests so bauen, dass sie auch bei laufender App sauber isoliert sind.

## 9. Modell-Routing, Verifikation, Effort, model_fit – 5,0

**Was es macht:** Wählt pro Task Modell und Effort (I35) und prüft, ob das Modell passt.

**Beweis:** DOC `CAPABILITIES.md`: Welcome-Chats laufen immer mit Sonnet 4.6 low. DOC `DESKTOP_APP.md`: `reasoningEffort` wird jetzt mitgeschickt. TEST-Ausgabe: „sonnet/medium want opus/high“.

**Stark:** Die Logik zur Auswahl ist da und getestet.

**Schwach:** Live nicht belegt, dass das gewählte Modell auch wirklich läuft. Die Doku widerspricht sich.

**Vorschlag:** Nach jedem Turn das echte Modell aus der Antwort lesen und als Event speichern. Erst dann gilt das Feature als fertig.

## 10. Context-Erneuerung – 5,5

**Was es macht:** `ChatContext.rotate` als einziger Einstieg. Handoff mit max. 24.000 Zeichen. Context-Archiv, Pack und Budget, `token_budget`.

**Wo:** `chat_context.py` (`rotate` 100 Z.), `token_budget.py` (18 breite `except`).

**Beweis:**
- LIVE: Main 20 bei ~4,1 Mio Tokens. Das Limit greift für ihn nicht.
- DB: `thread_context` wuchs von 53,7 MB (Audit) auf 173 MB.

**Stark:** Ein einziger Einstieg für die Rotation ist sauber.

**Schwach:**
- Tokens werden als „Bytes / 3“ geschätzt. Das ist grob.
- Große JSON-Blobs in der DB.
- Mains werden nur „angeraten“, nicht erzwungen.

**Vorschlag:** Alte Context-Blobs in Dateien auslagern oder kürzen. Ein hartes Limit auch für Mains, mit Warnung im UI.

## 11. Coworker-Alter (23 h), Token-Limit (2 Mio), Upkeep, worker_gc – 5,5

**Was es macht:** Kein Coworker-Chat älter als 23 h (I32), max. 2 Mio Tokens (I40). Upkeep räumt auf.

**Beweis:** LIVE: Coworker 15 (41 h), 7 (27,7 h), alter 3 (23,9 h) noch blockiert da. DB: `coworker_upkeep_failed` 1.

**Stark:** Der Alters-Wächter erkennt die Fälle richtig.

**Schwach:** Das Aufräumen hinkt hinterher. Blockierte Coworker werden nicht ersetzt.

**Vorschlag:** Upkeep auch für blockierte Coworker laufen lassen. Bei Fehler erneut versuchen und Alarm zeigen.

## 12. Recovery und Abgleich – 6,5

**Was es macht:** `PendingRemote`, kein blindes Neusenden, `turn_watch`, `stall_watch`, `stale_tasks`, `resolve_uncertain`. Nach Neustart werden laufende Tasks zu „uncertain“.

**Wo:** `turn_watch.py` (884 Z., 15 breite `except`), `step_guard.py`.

**Beweis:**
- DB: `turn_stalled` 125 und `turn_resume_skipped` 125 in ~5 h. `recover_timing` 207.
- LIVE: Task t81d10cc hängt als uncertain.
- TEST: `test_confirmed_creation_is_recovered_without_duplicate` ist **rot**.

**Stark:** Das Prinzip „nie blind neu senden“ schützt vor doppelten Turns.

**Schwach:** Sehr viele Stalls. Der rote Test betrifft genau die Doppel-Erkennung.

**Vorschlag:** Den roten Test klären. Für Stalls die Wartezeit und den Grund als Kennzahl führen.

## 13. Verifier, Proof, done_definition, Fix-Loop, Auto-Accept – 6,0

**Beweis:** DB `verify_started`/`verify_done` je 65. TEST `test_fix_rounds_limit_unchanged_and_main_decides` rot.

**Stark:** Maschinen-Prüfung vor der Abnahme ist eingebaut.

**Schwach:** Die Regel für Fix-Runden ist laut Test kaputt oder geändert.

**Vorschlag:** Klar festlegen, wie viele Fix-Runden erlaubt sind, und Test und Code angleichen.

## 14. Patch-Modus, Leases, Registry, EOL-Guard, Snapshots – 5,5

**Was es macht:** Jeder Task arbeitet in einer eigenen Kopie und wird danach gemergt.

**Wo:** `patch_mode.py` (1174 Z.).

**Beweis:** LIVE: 17 Merges heute, **14 Konflikte warten**, viele Tasks „Not merged“. DB: `patch_failed` 2.

**Stark:** Eigene Kopie pro Task verhindert, dass sich Coworker gegenseitig zerstören.

**Schwach:**
- Konflikte stauen sich.
- Der CRLF/LF-Mix im Repo macht Merges schwerer. Der EOL-Guard hilft, löst das Grundproblem aber nicht.
- Liste der Labels wächst ohne Ende (alte Tasks bleiben drin).

**Vorschlag:** Alle Dateien einheitlich auf ein Zeilenende umstellen (`.gitattributes`). Alte Merge-Labels aufräumen.

## 15. MCP-Bridge, Gateway, Tools, Sicherheit – 6,0

**Was es macht:** Gibt Notion-Chats Tools (`nt_api`, `nt_read_file`, `nt_shell`, `nt_job_result` …). Mit Gateway, Tailscale-Funnel und DevTools.

**Wo:** `bridge.py` (1158 Z., `_call` 134 Z.), `local_mcp`, `mcp_policy`, `devtools.py` (`call` 150 Z.).

**Beweis:** LIVE: Alle Tools in diesem Review liefen sauber. Lange Shell-Jobs laufen im Hintergrund. Der Daten-Ordner ist gegen Suche geschützt. DOC Audit: G1 (Operator-Key über öffentliche Funnel-Route) und G2 (Bind auf 0.0.0.0) sind „hoch“.

**Stark:** Sehr gute Werkzeuge. Hintergrund-Jobs und Datei-Hashes sind praktisch.

**Schwach:** `nt_shell` darf jede PowerShell mit Bypass bis 30 min (G5). `nt_write_file` darf fast überall schreiben (G6). Kein Größenlimit für Bodies (G9).

**Vorschlag:** Erst G1 und G2 schließen, dann Pfad-Whitelist für Schreiben und Rate-Limit.

## 16. Desktop-App (CDP 9333), desktop_shell, notion_shell, Web-UI – 5,5

**Was es macht:** Eigene Notion-Instanz mit Overlay über CDP (ohne asar-Patch). Studio-Tabs Chat/Live/Notes/Settings, Kurzbefehle, Not-Aus.

**Wo:** `desktop_shell.py` (1378 Z., 32 breite `except`, 14 leere), `notion_shell.py` (1048 Z.), `overlay.js` 108 KB, `settings.js` 95 KB, `live.js` 63 KB, `app.js` 46 KB.

**Stark:** Kein Eingriff in die Notion-Dateien. Doku nennt Risiken offen (ToS, DOM-Selektoren).

**Schwach:**
- Riesige JS-Dateien ohne Aufteilung.
- Alte Backups im Paket: `shell.js.bak-20261021-185207`, `studio.js.bak-20261021-185618`, `main.py.bak_quit`.
- Viele leere `except … pass` verstecken Fehler.
- Teile laut Doku „noch nicht im Live-Fenster geprüft“. `test_page_defaults` ist rot.

**Vorschlag:** .bak-Dateien löschen. `overlay.js` und `settings.js` in Module teilen. Leere `except` durch Log-Einträge ersetzen.

## 17. Screen-Capture – 6,0 (wenig geprüft)

**Was es macht:** Bilder vom Bildschirm oder Fenster mit einer Regel, was erlaubt ist (`capture_policy`).

**Schwach:** `capture_policy.py` hat keinen Docstring. Live nicht geprüft.

**Vorschlag:** Einen kurzen Live-Test in den Backtest aufnehmen.

## 18. Telemetrie, Fortschritts-Feed, Notes-Routen, Problems – 7,0

**Beweis:** DB: viele Event-Typen (`first_output`, `remote_sent/done`, `task_stalled` …). Die Lücken aus dem Audit sind geschlossen.

**Schwach:** Nur 2001 Events werden behalten, das sind ~5 h. Für Fehlersuche über Tage reicht das nicht. Es gibt keine `/api/events`-Route. `test_problems` (CRLF) ist rot.

**Vorschlag:** Events nach Typ zählen und als Tages-Summe dauerhaft speichern. Eine Lese-Route für Events anbieten.

## 19. Backup, Updater, restart_guard, Startup, test_app, Canary – 6,0 (wenig geprüft)

**Stark:** Neustart nur über `work\restart-harness.ps1 -Rebuild` (klar geregelt).

**Schwach:** Live nicht geprüft. `test_long_calls.test_sync_limit_setting_is_clamped` rot.

**Vorschlag:** Einen Restore-Test für Backups regelmäßig laufen lassen.

## 20. Tests und fake_notion – 7,0

**Beweis:** 3192 Tests, mehr Test- als Code-Zeilen. 20 rot. pytest scheitert an Reihenfolge (`test_0_guard…` muss als Erstes laufen).

**Stark:** Sehr hohe Abdeckung. Guard-Test, der echtes Netz und echte DB verbietet, ist eine gute Idee.

**Schwach:**
- 20 rote Tests im Haupt-Branch.
- Tests hängen von der Reihenfolge ab.
- `fake_notion` liefert Daten (z. B. Chat-Titel), die echte Notion nicht mehr liefert. Dadurch bleiben echte Fehler unentdeckt (siehe 4).

**Vorschlag:** Kein Merge, wenn Tests rot sind. Fake regelmäßig mit echten Antworten abgleichen. Guard als Fixture statt als „test_0“.

## 21. Doku und Sicherheit gesamt – 5,0

**Stark:** Viele ehrliche Docs, die Grenzen und Risiken nennen (Audit, Koordination, Desktop).

**Schwach:**
- `README.md` ist veraltet: nennt 60k Tokens / 80 Turns, der Code nutzt 1,2 Mio / 600.
- Docs widersprechen sich (Modell-Routing).
- Der Status der Sicherheits-Lücken G1–G14 ist nirgends gepflegt.

**Vorschlag:** README neu schreiben und in jede Doku ein „Stand“-Datum setzen.

---

## Querschnitts-Schwächen

1. **Riesige Funktionen:** `server.action` 526 Zeilen, `engine._complete` 329, `notion.run` 215, `operator_main.delegate` 149, `devtools.call` 150.
2. **Breite `except Exception`:** server.py 41, notion.py 34, desktop_shell.py 32, notion_shell.py 29. Dazu viele leere `except … pass`. Fehler werden so verschluckt.
3. **CRLF/LF-Mix:** 12 Module mit CRLF, der Rest LF. Das macht Patches und Merges schwer.
4. **DB wächst:** 215 MB, davon 173 MB `thread_context`.
5. **Alte Dateien:** `.bak`-Dateien im Paket und im Root.
6. **Rote Tests:** 20 im vollen Lauf, darunter zwei im Kern (Review, Doppel-Erkennung).
7. **Fake ≠ Live:** Tests grün, live kaputt (Main-Wechsel).
8. **Sicherheit:** Hohe Lücken aus dem Audit ohne geprüften Status.
9. **Veraltete README.**

## Top-10 Fix-Liste

| Rang | Fix | Warum |
|---|---|---|
| 1 | `test_failed_review_never_marks_done` klären | Sonst kann schlechte Arbeit als fertig gelten |
| 2 | `test_confirmed_creation_is_recovered_without_duplicate` klären | Schutz vor doppelten Turns |
| 3 | Main-Wechsel: Archiv-Umbenennung und Chat-Alter (echter Main) | Fehler bei jedem Wechsel |
| 4 | Sicherheits-Lücken G1 und G2 schließen | Fernzugriff auf die Shell möglich |
| 5 | Tests isolieren (Guard als Fixture, keine Abhängigkeit von Port 8790) | 13+ rote Tests vermutlich nur deswegen |
| 6 | Context-Limit auch für Mains erzwingen, `thread_context` kürzen | 4,1 Mio Tokens, 173 MB DB |
| 7 | 14 Patch-Konflikte abbauen und `.gitattributes` für Zeilenenden | Stau und Merge-Fehler |
| 8 | Upkeep für blockierte Coworker (> 23 h) | Regel I32 wird verletzt |
| 9 | Echtes Modell pro Turn messen | Routing ist sonst nur Theorie |
| 10 | `server.action` und `engine._complete` aufteilen, leere `except` loggen | Wartbarkeit |

## Weitere Vorschläge

- Eine „Gesundheits-Seite“ im Desktop-UI: rote Tests, offene Konflikte, alte Coworker, Context-Größe der Mains auf einen Blick.
- Event-Tages-Summen dauerhaft speichern, damit Trends über Wochen sichtbar sind.
- Vor jedem Merge: voller unittest-Lauf ohne laufende App, und ein Live-Backtest (I42) für die betroffenen Features.
- `fake_notion` einmal pro Woche gegen echte Antworten prüfen (Vertrags-Test).

---

## Nachprüfung der roten Tests (23.10., 23:15)

Ich habe 8 der roten Tests einzeln laufen lassen (27 s). **Alle 8 sind auch allein rot.** Die Reihenfolge der Tests ist also nicht die Ursache. Die laufende App erklärt höchstens die 6 Backtest-Limit-Tests. Die anderen Fehler sind echt: Code und Test passen nicht mehr zusammen.

| Test | Meldung | Einschätzung |
|---|---|---|
| `test_engine.test_failed_review_never_marks_done` | `1 != 2` | Test erwartet 2 Runden, Code macht 1. Entweder wurde die Review-Logik bewusst geändert (Review-Sparen) und der Test nicht nachgezogen, oder es ist ein echter Fehler. **Muss entschieden werden.** |
| `test_review_economy.test_fix_rounds_limit_unchanged_and_main_decides` | `1 != 2` | Gleiche Ursache wie oben (Zahl der Fix-Runden). |
| `test_native.test_confirmed_creation_is_recovered_without_duplicate` | `'Welcome to Notion' != 'Main'` | Neuer Chat behält den Standard-Titel. Umbenennen nach dem Anlegen klappt nicht. Gleiche Gegend wie der Archiv-Fehler beim Main-Wechsel (echter Main). |
| `test_telemetry.test_failed_attempt_records_its_error_class` | Task bleibt `retry` statt `blocked` | Neue Wiederhol-Logik (`presend_retries`) bei `INVALID_REQUEST`. Test nicht angepasst. |
| `test_long_calls.test_sync_limit_setting_is_clamped` | `50.0 != 30.0` | Code wurde geändert (Standard 45 s, erlaubt 10–55 s), der Test ist älter. Test veraltet. |
| `test_page_defaults.test_page_shows_the_server_defaults` | `idle_mode` fehlt | Die Einstellungs-Seite zeigt den Server-Wert `idle_mode` nicht. |
| `test_problems.test_server_route_and_install_keep_crlf` | `3 != 2` | Zeilenenden in `server.py`/Installer haben sich geändert. Folge des CRLF/LF-Mix. |
| `test_limits.test_one_disabled_slot_does_not_stall_other_seven` | Koordinator bleibt `executing` | Ausfall eines Slots blockiert noch die anderen. Echter Fehler im Pool oder Test veraltet. |

**Folgen für die Noten:**

- Feature 20 (Tests) sinkt von 7,0 auf **6,0**. Die Tests sind gut, werden aber bei Code-Änderungen nicht mehr mitgepflegt. Merges laufen offenbar auch bei roten Tests durch.
- Die Gesamtnote bleibt bei rund **6,0 / 10**.

**Neuer Top-Fix (vor Rang 1):** Eine Merge-Sperre bei roten Tests. Der Patch-Modus darf nur mergen, wenn die betroffenen Testdateien grün sind.

## Nachprüfung Teil 2: die restlichen 12 Tests (23.10., 23:20)

**Backtest-Limits (6 Tests):** Allein gestartet laufen alle 8 Tests in `test_backtest_limits` **grün** (14 s). Im vollen Lauf waren sie rot. Ursache: Andere Tests vorher lassen echte Netz-, Socket- oder DB-Spuren zurück. Der Guard-Test merkt das und schlägt an. Das ist **kein Fehler im Netz-Budget**, sondern eine **fehlende Isolation zwischen Tests**.

- Folge für Feature 8 (Netz-Budget): Die Note bleibt 7,5. Die Funktion ist belegt.
- Fix: Herausfinden, welcher Test vorher echte Verbindungen öffnet. Den Guard als Fixture für jeden Test nutzen und nicht nur als `test_0`.

**Pool-Tests `test_limits.EightSlotTests` (7 Tests inkl. `one_disabled_slot`):** Auch allein **alle rot** (66 s), alle mit der gleichen Meldung: „Coordinator did not reach expected state: executing“. Die Tasks werden also nie fertig.

- Eine gemeinsame Ursache ist sehr wahrscheinlich. Die Klasse nutzt `fixtures.EngineTests.worker/pump/tearDown`, dieselbe Grundlage wie `test_engine` und `test_telemetry`, die auch rot sind.
- **Vermutung (nicht bewiesen):** Im Telemetrie-Test bekommt der Fake-Worker `INVALID_REQUEST` und der Task bleibt bei `retry`. Wenn das Fake-Notion eine neue Angabe im Request (z. B. Modell `opus` oder `reasoningEffort`) ablehnt, hängen alle Engine-Tests auf diese Art.
- Folge für Feature 2 (Slots/Failover): Die Note bleibt 6,5, aber die Ausfall-Fälle sind im Moment **gar nicht durch Tests abgesichert**.

### Gesamtbild der 20 roten Tests

| Gruppe | Anzahl | Art |
|---|---|---|
| Backtest-Limits | 6 | Isolation zwischen Tests, kein Code-Fehler |
| Pool / EightSlotTests | 7 | Vermutlich ein gemeinsamer Fehler in der Engine-Testbasis oder im Fake |
| Review- und Fix-Runden | 2 | Entscheidung nötig: 1 oder 2 Runden |
| Chat-Titel nach dem Anlegen | 1 | Echter Fehler (Gegend Main-Wechsel) |
| Retry statt blocked | 1 | Test nicht an neue Logik angepasst |
| Wartezeit lange Aufrufe | 1 | Test veraltet |
| Einstellungs-Seite `idle_mode` | 1 | UI zeigt Server-Wert nicht |
| Zeilenenden server.py | 1 | Folge des CRLF/LF-Mix |

**Bester nächster Hebel:** Die `INVALID_REQUEST`-Ursache im Fake beziehungsweise in der Engine-Testbasis finden. Wenn die Vermutung stimmt, werden damit bis zu 9 Tests auf einmal grün.

## Korrektur (23.10., 23:30)

Die Vermutung oben (`INVALID_REQUEST` im Fake) stimmt für die Pool-Tests **nicht**. In `test_per_slot_cap_three_queues_remaining_work` bleibt der Run ohne Fehler bei `executing`. Die Tasks erreichen den Fake-Adapter gar nicht. `INVALID_REQUEST` kommt nur im Telemetrie-Test vor, und dort ist er absichtlich ausgelöst. Die Ursache liegt also vor dem Senden (Planung, Queue, Budget oder Slot-Auswahl). Die Fix-Aufträge stehen in `docs/FIX_AUFTRAEGE.md`.
