# CAPABILITIES – Phase 1 (2026-10-16 ~12:00)

Testaccount: **nur hg@arisya.de** (`d84a51c7…`, Setting `harness_account_id`). Belege: [GEMOCKT] / [LOKAL GEPRÜFT] / [LIVE GEPRÜFT] / [NICHT GEPRÜFT]. "historisch" = Live-Daten aus 37 gespeicherten Tasks/7 Runs, nicht neu erzeugt.

## 1. Betriebsmodus
| Punkt | Befund | Beleg |
|---|---|---|
| Start | `pythonw main.py` aus dem Quellcode, PID 30952 seit 11:51:44; Ports 8790 + 53768 gehören dieser PID; `/api/health` 200; `run.err.log` leer | LOKAL GEPRÜFT |
| Umschaltung | `work\switch-to-source.ps1` stoppt nur `dist\NotionTool.exe` (exakter Pfad), danach `restart-notiontool.ps1`; Fallback: EXE wird neu gestartet, wenn 8790 nicht übernommen wird | LOKAL GEPRÜFT |
| Datenordner | EXE lief mit `--data-dir <root>\data` = derselbe Ordner wie der Quellstart → gleicher Bridge-Token, MCP über Funnel nach Neustart sofort wieder erreichbar | LIVE GEPRÜFT |
| Account | Health meldet `d84a…`/ready. Direkt nach dem Start einmal `971f…` gemeldet, Ursache UNBEKANNT (Setting war `d84a…`) | LOKAL GEPRÜFT |
| Pool d84a | 1 Main („Main Chat“) + 5 Coworker („Cowork joined“), alle enabled, alle Welcome → I1 erfüllt | LOKAL GEPRÜFT (DB) |
| Account 971f | 5 Cowork + 2 deaktivierte Welcome-Chats, kein Main; wird nicht verwendet, nicht angefasst | LOKAL GEPRÜFT (DB) |

## 2. Modelle
Katalog per `getAvailableModels` (read-only, d84a) [LIVE GEPRÜFT]:

| Router-Label | Katalog-ID | Gruppe | Aufwand (Default) |
|---|---|---|---|
| Opus 5.5 | `albuquerque-quinn` | intelligent | low–max (medium) |
| Sonnet 5.5 | `achira-donut` | intelligent | low–max (high) |
| Haiku 5.5 | `amla-jam` | fast | none–max (medium) |
| *(nicht im Router)* Sonnet 4.6 | `almond-croissant-low` | fast | low/medium/high/max (**low**) |

Tatsächlich ausgeführt (Evidenz: `agent-inference`-Steps des eigenen Turns, Felder `model`/`notionModelName`, nie die Config):

| Angefordert | Ergebnis | n | Status | Fallback |
|---|---|---|---|---|
| Opus 5.5 | ersetzt durch **Sonnet 4.6** (`almond-croissant-low`) | 26 Tasks / 29 Events | ersetzt (LIVE GEPRÜFT, historisch) | Ergebnis behalten, `model_fallback` anzeigen |
| Haiku 5.5 | ersetzt durch **Sonnet 4.6** | 11 | ersetzt (LIVE GEPRÜFT, historisch) | wie oben; kein Speed-Vorteil durch Haiku-Routing |
| Sonnet 5.5 | nie angefordert | 0 | NICHT GEPRÜFT | wie Opus behandeln (Ersetzung erwartet) |
| Modellwechsel im selben Thread | 2 Threads mit Haiku↔Opus: Wechsel akzeptiert, Ausführung bleibt Sonnet 4.6 | 2 Threads | ersetzt (LIVE GEPRÜFT, historisch) | – |
| Reasoning-Aufwand | Katalog kennt `supportedReasoningEfforts`; der Request-Body sendet **kein** Aufwand-Feld (`body()`: model, modelFromUser, enableComputer, agentMemorySettings). Feldname im Request UNBEKANNT | – | UNBEKANNT | Server-Default (bei Sonnet 4.6: low) |

Hypothese (UNBEKANNT): Welcome-/Onboarding-Threads (`welcome.json`: `modelFromUser:false`, `agentMessageKey: unboxing_m5`, surface `post_onboarding_agent`) sind serverseitig auf Sonnet 4.6 low fixiert; `ensure_thread_model` setzt `modelFromUser:true` in der gespeicherten Config, der Server ignoriert das. Nicht prüfbar ohne Nicht-Welcome-Chat (I1).

**Konsequenz:** Modell-Routing ist auf diesem Account wirkungslos. Qualität und Latenz entsprechen Sonnet 4.6 low. Router-IDs kommen live aus dem Katalog (Label-Match, `notion.py:142–150`), keine hartcodierten IDs (Default `achira-donut` in `template.json` wird in `body()` überschrieben) → nur belegte Katalog-IDs, aber keine wird wie angefordert ausgeführt.

## 3. Fehlerformen (Code + historische Events)
| Form | Behandlung | Stelle | Beleg |
|---|---|---|---|
| HTTP 429 | `RateLimited(retry_after)` | notion.py:650 | LOKAL GEPRÜFT (Code) |
| anderes HTTP | `ValueError('Notion inference: HTTP n')` | :652 | LOKAL GEPRÜFT (Code) |
| Timeout/OSError/JSON | `PendingRemote` → Abgleich über Transkript, kein Neusenden (I5) | :653–654, :656–699 | LOKAL GEPRÜFT (Code) |
| Turn endet mit Error-Step ohne Output | `remote_turn_failed` (sub_type), `RateLimited(20, confirmed=True)` → neuer Turn nach 20 s | :701–712, model_verification.turn_status | LIVE GEPRÜFT (historisch) |
| `temporarily-unavailable` | 3× gesehen; 2 Tasks danach im 2. Versuch fertig (3. = Checkpoint). `isRetryable` wird im Code nicht ausgewertet; trotz `isRetryable:false` war die bestätigte Wiederholung 2/2 erfolgreich | – | LIVE GEPRÜFT (historisch, n=2) |
| Modell abweichend | `fallback`, Run läuft weiter | notion.py:765–780 | LIVE GEPRÜFT (historisch) |
| Modell fehlt | Turn läuft noch → `PendingRemote`; sonst `unverified` | :781–790 | LIVE GEPRÜFT (historisch, 2×) |

## 4. Stream & Abschluss
- `runInferenceTranscript` liefert NDJSON: `patch-start`/`patch-sync` (`data.s` = alle Steps), `patch` (`v` → `apply_patch`); UI-Update alle 0,6 s (notion.py:633–643). [LOKAL GEPRÜFT]
- Die Antwort wird **nicht** aus dem Stream übernommen, sondern über `recover()` aus `getThreadTranscript` (committed answer), danach `verify_response_model`. [LOKAL GEPRÜFT]
- Zusatz-Roundtrips pro Task (Code, Latenz NICHT GEPRÜFT): vorher `getAvailableModels` (jeder Task, :605→145) + `syncRecordValues` Thread + **alle** Thread-Messages (`ensure_thread_model`), ggf. Transaction + Re-Read; nachher `getThreadTranscript` + `syncRecordValues` Thread + alle Messages in 100er-Blöcken → Verifikationskosten wachsen mit der Thread-Länge.

## 5. Offen
| Fähigkeit | Status | Fallback |
|---|---|---|
| Multimodal (Bilder/Dateien im Turn) | UNBEKANNT | nur Text; Dateien über MCP-Tools |
| Latenz vs. Kontextgröße | UNBEKANNT | Limits 60 000 Tokens / 80 Turns, Checkpoint im selben Chat |
| Aufwand-Feld im Request | UNBEKANNT | Server-Default |
| Sonnet 5.5 Ausführung | NICHT GEPRÜFT | als ersetzt behandeln |

## 6. Folgerungen für Phase 2 (nur lokal)
1. Modellkatalog pro Account cachen (TTL), statt pro Task `getAvailableModels` aufzurufen.
2. Verifikation auf die Messages des eigenen Turns begrenzen statt alle Thread-Messages zu laden.
3. Router/UI ehrlich machen: effektives Modell (Sonnet 4.6 low) anzeigen; Haiku-Routing bringt keinen Speed-Gewinn.
4. Health: Account-Anzeige nach dem Start beobachten (einmal 971f).

## 7. Phase 4 - Fix K und Fix L (Code-Stand 16.10.2026)
| Fähigkeit | Befund | Stelle | Beleg |
|---|---|---|---|
| Edge-Tab-Wake (Fix K) | Der versteckte Transport-Tab wird ohne Navigation und ohne Notion-Request aus dem Edge-Zustand frozen geholt: `browser.wake(target)`, sonst DevTools `Page.setWebLifecycleState` mit `{state: active}`. Normal höchstens alle 15 s; erzwungen, wenn ein Stream länger als `stall_wake_after` keine neuen Bytes liefert, dann vorher Test mit `setTimeout(0)` (2 s). Liegt außerhalb des Request-Budgets (I11). Event `edge_tab_woken` mit `frozen=true` höchstens einmal pro 60 s | edge_transport.py:34-66, :219-222 | LOKAL GEPRÜFT (Code) |
| Reviewer-Fallback bei belegtem Main (Fix L) | Wurde der Main kürzlich als belegt erkannt (`main_busy_at` innerhalb von `MAIN_BUSY_FALLBACK_S`) und haben alle freien Coworker an diesem Run mitgearbeitet, wartet das Review nicht bis `BUSY_WAIT_LIMIT`: Der Coworker mit dem kleinsten eigenen Executor-Anteil prüft. Event `reviewer_fallback` einmal pro Task | engine.py:475-495 | LOKAL GEPRÜFT (Code) |

Live-Belege: siehe docs/REQUEST_COORDINATION.md (Abschnitt Nachweise); in diesem Dokument NICHT GEPRÜFT.
