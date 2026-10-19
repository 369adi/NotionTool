# Architektur-Audit (Phase 0)

Stand: 16.10.2026, ca. 11:50. Rein lesend erstellt, kein Code geändert.
Belegkategorien: LOKAL GEPRÜFT = Code, DB oder Tests gelesen bzw. ausgeführt; NICHT GEPRÜFT / UNBEKANNT = ohne Beleg.
Nachtrag 19.10.2026: Die Zeilennummern vom 16.10. waren veraltet und wurden durch Funktions-, Klassen- oder Abschnittsnamen ersetzt (Namen gegen den Code vom 19.10. geprüft). Die inhaltlichen Befunde selbst wurden nicht neu geprüft und gelten weiter als Stand 16.10.

## 1. Laufzeitumgebung (Ist)

- Laufende Instanz: `dist\NotionTool.exe`, PID 18960, Start 16.10.2026 10:23:21, lauscht auf 127.0.0.1:8790 und 127.0.0.1:53768. `/api/health` = 200, `account_state` ready, Transport edge. [LOKAL GEPRÜFT]
- Aktives Konto laut health: `971f379c…`. Das ist NICHT das in Prompt und Gedächtnis genannte the test account (`d84a51c7…`). Browser-Profil `notion-971f…` wurde am 16.10. 10:24 angelegt. Alle Runs/Tasks in der DB stammen vom d84a-Konto. Welcome-Pool (1 Main + 5 Coworker) des aktiven Kontos: UNBEKANNT.
- Die EXE nutzt ihren eigenen Ordner als Dev-Tools-Root: `nt_memory_read` liest `dist\project-memory\NotionTool.md` (leer). Das echte Gedächtnis liegt in `project-memory\NotionTool.md`. [LOKAL GEPRÜFT]
- `work\restart-notiontool.ps1` stoppt im Stopp-Block nur `python*.exe` mit `main.py` und startet danach `pythonw main.py`. Die laufende EXE wird nicht gestoppt; eine zweite Instanz bekäme Port 8790 nicht (Klasse `ExclusiveHTTPServer` in server.py) und würde das Edge-Profil teilen. [LOKAL GEPRÜFT, Folge abgeleitet]
- Testsuite: 192 Tests OK in 75,4 s (`work\tests.log`). [LOKAL GEPRÜFT]

## 2. Architekturkarte

Prompt → Run (Host-API, server.py `Application.action`) → `Engine.submit` → `allocation.allocation` / `allocation.planner_mode` (Regex-Heuristik direct/planned) → `Engine._approach` → Planner-Task auf dem Main → `Engine._import_plan` mit `model_routing.validate_models` → `Engine.tick` (Round-Robin) → `Engine._choose_worker` → `Engine._dispatch` → ThreadPool → `NotionAdapter.run` (notion.py) → `NotionAdapter.recover` / `NotionAdapter.verify_response_model` → `Engine._complete` → `Engine._advance`.

- Stufen: Planner → Executor → Reviewer → Integrator, stufenweise seriell.
- Rollen: planner/integrator immer auf dem Main (`Engine._choose_worker`); executor/reviewer auf Coworkern; Reviewer darf nicht eigener Executor sein (`Engine._choose_worker`); `context_compactor` für Checkpoints.
- Modellwahl: Planner wählt pro Task opus/sonnet/haiku mit Begründung (`model_routing.choice`, `validate_models`); der Main sendet immer opus (`NotionAdapter.run`, Stand 16.10.).
- Parallelität: `max_workers` (Deckel 40) und `per_account` (`Engine.tick`, `Engine._choose_worker`).
- Kontext an Tasks: Abhängigkeitsergebnisse je ≤ 6000 Zeichen, gesamt ≤ 60000 Zeichen (`Engine._dispatch`, Stand 16.10.). Chat-Kontext pro Worker mit Generation, Rotation im selben Thread (`ChatContext.rotate`), Checkpoint (`chat_context.semantic_checkpoint`).
- Remote-Pfad pro Auftrag (`NotionAdapter.run`): Transcript-Head lesen, `thread_busy`, `ChatContext.rotate`, ggf. erneut lesen, `ensure_thread_model`, Stream `runInferenceTranscript`, danach `recover` mit erneutem Transcript-Read.
- Request-Start-Budget global für alle Requests: network_budget.py (`NetworkBudget.acquire`), eingebunden beim Request-Start in vendor\transport.py und edge_transport.py.
- Persistenz: SQLite `objects(kind, id, doc JSON)` und `events(seq, ts, run_id, kind, doc)` (store.py).
- Schnittstellen: Host-API (server.py), SSE `/api/stream` (server.py, GET-Routing im Request-Handler), MCP (bridge.py), UI `notiontool\web` (app.js, damals 283 Zeilen).

## 3. Funktionsmatrix

| Bereich | Status | Beleg |
|---|---|---|
| Welcome-Pool 1+5, Kontextwechsel im selben Chat | vorhanden | `ChatContext.rotate`, Gedächtnis |
| Idempotenz / Reconcile | vorhanden | `request_fingerprint` in `NotionAdapter.run`, Reconcile in `Engine._complete` (heute `Engine._reconcile`) |
| Turn-Fehlererkennung | vorhanden | `model_verification.turn_status` |
| Modell-Verifikation | vorhanden, live 100 % Fallback | DB: 37/37 Tasks `fallback` |
| Router mit Fallback-Kette | fehlt | model_routing.py kennt nur 3 Labels |
| Effort-Policy | fehlt | – |
| Planner-Modi | teilweise (direct/planned per Regex) | `allocation.planner_mode`, `allocation.allocation` |
| DAG-Scheduler mit kritischem Pfad | fehlt (Listenreihenfolge) | `Engine.tick` |
| Datei-/Ressourcensperren | vorhanden | Sperr-Helfer in engine.py zwischen `_approach` und `tick` (Stand 16.10.) |
| Adaptive Parallelität / Circuit Breaker | fehlt; nur globaler 429-Backoff | `network_budget.backoff` (Stand 16.10.; heute gibt es `CircuitOpen`, `_lower_parallel`, `parallel_limit`) |
| Projekte / Kontextisolation | teilweise (`project_path`, `folder_id` am Run) | Run-Felder in der DB |
| Verifier-Kaskade | teilweise (ein Reviewer-Task) | `Engine._choose_worker` (Reviewer-Wahl) |
| Telemetrie | teilweise | Abschnitt 5 |
| Screen Capture | fehlt | – |
| Live-Dashboard | teilweise (SSE vorhanden, UI schlank) | `/api/stream` in server.py, app.js |

## 4. Engpässe (nach erwarteter Wirkung)

1. **Orchestrierungs-Overhead bei kleinen Aufträgen.** `allocation.allocation` schickt alles außer Gruß/Kurzfrage in `planned` → mindestens Planner + Executor + Integrator als serielle Turns. Kleinste planned-Runs: 25–28 s, Median eines einzelnen Tasks: 10,5 s (≈ 2,5×). [LOKAL GEPRÜFT aus DB, Faktor geschätzt]
2. **Remote-Reads vor jedem Senden.** In `NotionAdapter.run`: Transcript-Head, `thread_busy`, ggf. erneuter Read nach `rotate`, danach Read in `recover` → mindestens 3 serielle Leserunden pro Turn. Dauer UNBEKANNT (keine `remote_sent`-Events).
3. **Modellwahl wirkungslos.** 37/37 Tasks liefen als Fallback auf `almond-croissant-low`; der Planner muss trotzdem `model_reason` liefern (`model_routing.validate_models`). Router-Arbeit lohnt erst nach Phase 1.
4. **Start-Intervall erholt sich nie.** Die Backoff-Logik in network_budget.py verdoppelte das Intervall bis 2 s, persistierte es und lud es beim Start; laut Codesuche vom 16.10. wurde es nirgends gesenkt → nach wenigen 429 dauerhaft höchstens 0,5 Request-Starts/s. [LOKAL GEPRÜFT am 16.10.; heute NICHT GEPRÜFT]
5. **Scheduler.** `Engine.tick` hält einen Lock über Complete, Reconcile, Advance und Dispatch; Auswahl in Listenreihenfolge ohne kritischen Pfad; Worker-Wahl nur nach `last_used` (`Engine._choose_worker`).
6. **Große JSON-Blobs in der DB.** `thread_context` = 53,7 MB in 16 Objekten (Ø 3,4 MB); jedes `put` schreibt das ganze Dokument neu. DB 87,5 MB, davon 33,1 MB Freelist (kein VACUUM). [LOKAL GEPRÜFT]
7. **Kontextbudget in Zeichen statt Tokens** (`Engine._dispatch`, Stand 16.10.), ohne Manifest-Hashes.

## 5. Telemetrie-Lücken

- 374 Events in ca. 17 h; 136 davon (36 %) sind `context_scan_waiting` ohne Informationswert.
- Es fehlen: task_queued/ready, lease_acquired, remote_sent, first_output, remote_done, verify_*, Budget-/Breaker-Wechsel, context_built.
- Task-Dauer nur aus `objects.tasks.started/finished`, bei Retry überschrieben (nur letzter Versuch messbar).
- Keine Schema-Version in Event-Dokumenten, keine einheitlichen Fehlerklassen.
- Tokenwerte in `context_rotated` noch `[redacted]`: Das Event stammt von vor dem COUNT_KEYS-Fix; die Log-Redaktion in store.py (COUNT_KEYS) lässt `estimated_tokens` heute per Teilstring durch. Live NICHT GEPRÜFT.

## 6. Risiken

- Doppelinstanz bzw. fehlgeschlagener Restart (EXE vs. `pythonw main.py`), siehe Abschnitt 1.
- Kontowechsel: Prompt- und Gedächtnisdaten passen nicht zum aktiven Konto.
- Öffentliche Funnel-Route zum MCP mit `nt_shell`/`nt_write_file`: Auth im Funnel-Pfad in bridge.py im Detail NICHT GEPRÜFT – hohe Priorität.
- Interne, undokumentierte Notion-API: Fehlerformen und Modell-IDs können sich ohne Ankündigung ändern.
- Große Einzelmodule (am 16.10.: notion.py 846, engine.py 695, server.py 668 Zeilen) erhöhen das Änderungsrisiko; die Engine ist gut getestet (test_engine.py damals 388 Zeilen).

## 7. Abhängigkeiten (belegt)

- engine → harness_account (in `Engine.tick`), plain_protocol (in `Engine._dispatch`), store, Adapter (notion)
- notion.run → chat_context (in `NotionAdapter.run`), model_verification
- vendor\transport → network_budget (Modul-Import); edge_transport → Budget (beim Request-Start)
- model_routing → protocol (Modul-Import); allocation → protocol (Modul-Import); mcp_policy → notion.unwrap (Modul-Import)
- Vollständige Import-Grafik: NICHT GEPRÜFT

## 8. Priorisierter Plan

1. **Vor Phase 1:** Betriebsmodus klären (EXE stoppen und aus Quelle starten oder Restart-Skript EXE-fähig machen), aktives Konto und dessen Welcome-Pool prüfen, Gedächtnispfad korrigieren.
2. **Phase 2 Telemetrie** (`remote_sent`, `first_output`, `remote_done`) – ohne sie sind Engpass 2 und 5 nicht messbar.
3. **DIRECT-Pfad schärfen** (Engpass 1) – größter Zeitgewinn bei kleinen Aufträgen.
4. **Erholung des Start-Intervalls + adaptive Parallelität** (Engpass 4, Phase 3).
5. **Router erst nach Phase 1** (Engpass 3).
6. **`thread_context` auslagern/aufteilen, VACUUM** (Engpass 6).
7. **DAG-Scheduler** (Engpass 5, Phase 4).
