# Architektur-Audit (Phase 0)

Stand: 16.10.2026, ca. 11:50. Rein lesend erstellt, kein Code geändert.
Belegkategorien: LOKAL GEPRÜFT = Code, DB oder Tests gelesen bzw. ausgeführt; NICHT GEPRÜFT / UNBEKANNT = ohne Beleg.

## 1. Laufzeitumgebung (Ist)

- Laufende Instanz: `dist\NotionTool.exe`, PID 18960, Start 16.10.2026 10:23:21, lauscht auf 127.0.0.1:8790 und 127.0.0.1:53768. `/api/health` = 200, `account_state` ready, Transport edge. [LOKAL GEPRÜFT]
- Aktives Konto laut health: `971f379c…`. Das ist NICHT das in Prompt und Gedächtnis genannte hg@arisya.de (`d84a51c7…`). Browser-Profil `notion-971f…` wurde am 16.10. 10:24 angelegt. Alle Runs/Tasks in der DB stammen vom d84a-Konto. Welcome-Pool (1 Main + 5 Coworker) des aktiven Kontos: UNBEKANNT.
- Die EXE nutzt ihren eigenen Ordner als Dev-Tools-Root: `nt_memory_read` liest `dist\project-memory\NotionTool.md` (leer). Das echte Gedächtnis liegt in `project-memory\NotionTool.md`. [LOKAL GEPRÜFT]
- `work\restart-notiontool.ps1` stoppt nur `python*.exe` mit `main.py` (Zeilen 5–11) und startet `pythonw main.py` (Zeile 18). Die laufende EXE wird nicht gestoppt; eine zweite Instanz bekäme Port 8790 nicht (`ExclusiveHTTPServer`, server.py:24) und würde das Edge-Profil teilen. [LOKAL GEPRÜFT, Folge abgeleitet]
- Testsuite: 192 Tests OK in 75,4 s (`work\tests.log`). [LOKAL GEPRÜFT]

## 2. Architekturkarte

Prompt → Run (Host-API, server.py `action` :462) → `Engine.submit` (engine.py:128) → `allocation` (allocation.py:6, Regex-Heuristik direct/planned) → `_approach` (engine.py:182) → Planner-Task auf dem Main → `_import_plan` (engine.py:622) mit `validate_models` (model_routing.py:13) → `tick` (engine.py:307, Round-Robin) → `_choose_worker` (engine.py:361) → `_dispatch` (engine.py:409) → ThreadPool → `NotionAdapter.run` (notion.py:571) → `recover` (notion.py:656) / `verify_response_model` (notion.py:749) → `_complete` (engine.py:476) → `_advance` (engine.py:633).

- Stufen: Planner → Executor → Reviewer → Integrator, stufenweise seriell.
- Rollen: planner/integrator immer auf dem Main (engine.py:367); executor/reviewer auf Coworkern; Reviewer darf nicht eigener Executor sein (engine.py:389–402); `context_compactor` für Checkpoints.
- Modellwahl: Planner wählt pro Task opus/sonnet/haiku mit Begründung (model_routing.py:4–20); der Main sendet immer opus (notion.py:604).
- Parallelität: `max_workers` (Deckel 40) und `per_account` (engine.py:336, :387).
- Kontext an Tasks: Abhängigkeitsergebnisse je ≤ 6000 Zeichen, gesamt ≤ 60000 Zeichen (engine.py:457–460). Chat-Kontext pro Worker mit Generation, Rotation im selben Thread (chat_context.py:203), Checkpoint (chat_context.py:91).
- Remote-Pfad pro Auftrag (notion.py:571–654): Transcript-Head lesen (:577), `thread_busy` (:584), `ChatContext.rotate` (:589), ggf. erneut lesen (:592/:596), `ensure_thread_model` (:606), Stream `runInferenceTranscript` (:633), danach `recover` mit erneutem Transcript-Read (:658).
- Request-Start-Budget global für alle Requests: network_budget.py, eingebunden in vendor\transport.py:159 und edge_transport.py:160.
- Persistenz: SQLite `objects(kind, id, doc JSON)` und `events(seq, ts, run_id, kind, doc)` (store.py).
- Schnittstellen: Host-API (server.py), SSE `/api/stream` (server.py:261), MCP (bridge.py), UI `notiontool\web` (app.js 283 Zeilen).

## 3. Funktionsmatrix

| Bereich | Status | Beleg |
|---|---|---|
| Welcome-Pool 1+5, Kontextwechsel im selben Chat | vorhanden | chat_context.py:203, Gedächtnis |
| Idempotenz / Reconcile | vorhanden | notion.py:615 (`request_fingerprint`), engine.py:599 |
| Turn-Fehlererkennung | vorhanden | model_verification.py:38 |
| Modell-Verifikation | vorhanden, live 100 % Fallback | DB: 37/37 Tasks `fallback` |
| Router mit Fallback-Kette | fehlt | model_routing.py kennt nur 3 Labels |
| Effort-Policy | fehlt | – |
| Planner-Modi | teilweise (direct/planned per Regex) | allocation.py:6–19 |
| DAG-Scheduler mit kritischem Pfad | fehlt (Listenreihenfolge) | engine.py:343–357 |
| Datei-/Ressourcensperren | vorhanden | engine.py:254–305 |
| Adaptive Parallelität / Circuit Breaker | fehlt; nur globaler 429-Backoff | network_budget.py:44–58 |
| Projekte / Kontextisolation | teilweise (`project_path`, `folder_id` am Run) | Run-Felder in der DB |
| Verifier-Kaskade | teilweise (ein Reviewer-Task) | engine.py:389 |
| Telemetrie | teilweise | Abschnitt 5 |
| Screen Capture | fehlt | – |
| Live-Dashboard | teilweise (SSE vorhanden, UI schlank) | server.py:261, app.js |

## 4. Engpässe (nach erwarteter Wirkung)

1. **Orchestrierungs-Overhead bei kleinen Aufträgen.** allocation.py:15–19 schickt alles außer Gruß/Kurzfrage in `planned` → mindestens Planner + Executor + Integrator als serielle Turns. Kleinste planned-Runs: 25–28 s, Median eines einzelnen Tasks: 10,5 s (≈ 2,5×). [LOKAL GEPRÜFT aus DB, Faktor geschätzt]
2. **Remote-Reads vor jedem Senden.** notion.py:577, :584, ggf. :592/:596 und danach :658 → mindestens 3 serielle Leserunden pro Turn. Dauer UNBEKANNT (keine `remote_sent`-Events).
3. **Modellwahl wirkungslos.** 37/37 Tasks liefen als Fallback auf `almond-croissant-low`; der Planner muss trotzdem `model_reason` liefern (model_routing.py:17). Router-Arbeit lohnt erst nach Phase 1.
4. **Start-Intervall erholt sich nie.** network_budget.py:53 verdoppelt das Intervall bis 2 s, :55 persistiert es, :25 lädt es beim Start; laut Codesuche wird es nirgends gesenkt → nach wenigen 429 dauerhaft höchstens 0,5 Request-Starts/s. [LOKAL GEPRÜFT]
5. **Scheduler.** `tick` hält einen Lock über Complete, Reconcile, Advance und Dispatch (engine.py:308–359); Auswahl in Listenreihenfolge ohne kritischen Pfad; Worker-Wahl nur nach `last_used` (engine.py:406).
6. **Große JSON-Blobs in der DB.** `thread_context` = 53,7 MB in 16 Objekten (Ø 3,4 MB); jedes `put` schreibt das ganze Dokument neu. DB 87,5 MB, davon 33,1 MB Freelist (kein VACUUM). [LOKAL GEPRÜFT]
7. **Kontextbudget in Zeichen statt Tokens** (engine.py:457–460), ohne Manifest-Hashes.

## 5. Telemetrie-Lücken

- 374 Events in ca. 17 h; 136 davon (36 %) sind `context_scan_waiting` ohne Informationswert.
- Es fehlen: task_queued/ready, lease_acquired, remote_sent, first_output, remote_done, verify_*, Budget-/Breaker-Wechsel, context_built.
- Task-Dauer nur aus `objects.tasks.started/finished`, bei Retry überschrieben (nur letzter Versuch messbar).
- Keine Schema-Version in Event-Dokumenten, keine einheitlichen Fehlerklassen.
- Tokenwerte in `context_rotated` noch `[redacted]`: Das Event stammt von vor dem COUNT_KEYS-Fix; store.py:17/:25 lässt `estimated_tokens` heute per Teilstring durch. Live NICHT GEPRÜFT.

## 6. Risiken

- Doppelinstanz bzw. fehlgeschlagener Restart (EXE vs. `pythonw main.py`), siehe Abschnitt 1.
- Kontowechsel: Prompt- und Gedächtnisdaten passen nicht zum aktiven Konto.
- Öffentliche Funnel-Route zum MCP mit `nt_shell`/`nt_write_file`: Auth im Funnel-Pfad in bridge.py im Detail NICHT GEPRÜFT – hohe Priorität.
- Interne, undokumentierte Notion-API: Fehlerformen und Modell-IDs können sich ohne Ankündigung ändern.
- Große Einzelmodule (notion.py 846, engine.py 695, server.py 668 Zeilen) erhöhen das Änderungsrisiko; die Engine ist gut getestet (test_engine.py 388 Zeilen).

## 7. Abhängigkeiten (belegt)

- engine → harness_account (engine.py:316), plain_protocol (:434), store, Adapter (notion)
- notion.run → chat_context (notion.py:586), model_verification
- vendor\transport → network_budget (transport.py:21); edge_transport → Budget (edge_transport.py:160)
- model_routing → protocol (:2); allocation → protocol (:3); mcp_policy → notion.unwrap (:2)
- Vollständige Import-Grafik: NICHT GEPRÜFT

## 8. Priorisierter Plan

1. **Vor Phase 1:** Betriebsmodus klären (EXE stoppen und aus Quelle starten oder Restart-Skript EXE-fähig machen), aktives Konto und dessen Welcome-Pool prüfen, Gedächtnispfad korrigieren.
2. **Phase 2 Telemetrie** (`remote_sent`, `first_output`, `remote_done`) – ohne sie sind Engpass 2 und 5 nicht messbar.
3. **DIRECT-Pfad schärfen** (Engpass 1) – größter Zeitgewinn bei kleinen Aufträgen.
4. **Erholung des Start-Intervalls + adaptive Parallelität** (Engpass 4, Phase 3).
5. **Router erst nach Phase 1** (Engpass 3).
6. **`thread_context` auslagern/aufteilen, VACUUM** (Engpass 6).
7. **DAG-Scheduler** (Engpass 5, Phase 4).
