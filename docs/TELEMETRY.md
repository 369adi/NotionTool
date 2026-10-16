# Telemetrie und Event-Schema (Phase 2)

Stand: 16.10.2026 · Schema-Version `v = 1` · Code: `notiontool/telemetry.py`

## 1. Event-Schema

Alle Events stehen in der SQLite-Tabelle `events(seq, ts, run_id, kind, doc)`. `seq` ist fortlaufend, `ts` ist Unix-Zeit, `doc` ist JSON.
Neu ab Phase 2 sind nur zusätzliche Felder. Bestehende Felder und Event-Namen bleiben unverändert (abwärtskompatibel).

| Feld | Bedeutung |
|---|---|
| `v` | Schema-Version (`1`) |
| `task_id` | Task, zu dem das Event gehört (leer bei Run-Events) |
| `worker_id` | `account:thread` des ausführenden Chats |
| `project_id` | `folder_id` des Runs (leer ohne Projekt) |

## 2. Event-Kette pro Task

Reihenfolge (`telemetry.TASK_CHAIN`):

`task_queued → task_ready → lease_acquired → context_built → remote_sent → first_output → remote_done → verify_started → verify_done → task_finished → task_attempt_finished`

Das bestehende `task_started` liegt weiterhin zwischen `context_built` und `remote_sent`.

| Event | Zusatzfelder |
|---|---|
| `first_output` | `seconds` (seit `remote_sent`) |
| `remote_done` | `outcome`, `seconds` (seit `remote_sent`), `error_class`, `lines` |
| `verify_done` | `outcome`, `seconds`, `messages_read` |
| `task_attempt_finished` | `state`, `error_class`, `duration` |

`telemetry.chain_gaps(store, task_id)` liefert die fehlenden Schritte. Eine leere Liste bedeutet: Kette vollständig.

## 3. Versuchsdaten am Task

Neue Task-Felder: `ready_at`, `first_output_at`, `remote_done_at`, `verify_messages_read` und `attempt_log[]`.
Jeder Versuch bekommt einen eigenen Eintrag mit `attempt, worker_id, started, finished, duration, state, error_class, sent, first_output, remote_done`. Die Dauer bleibt dadurch für jeden Versuch erhalten, nicht nur für den letzten.

## 4. Koordination und Fallbacks

- `request_coordination_adjusted` (aus `network_budget.py` bei einem Rate-Limit): `interval_before`, `interval`, `paused_seconds`.
- `circuit_open` / `circuit_closed`: reserviert, werden erst in Phase 3 geschrieben.
- Fallback-Events (`telemetry.FALLBACK_EVENTS`): `model_fallback`, `context_reset_fallback`, `approach_fallback`.

## 5. Weniger Rauschen

`context_scan_waiting` wird nur noch beim Wechsel in den Wartezustand geschrieben, mit `error` (Exception-Typ) und `error_class`. Die Erholung meldet `context_scan_resumed`.

## 6. Fehlerklassen

`TRANSIENT`, `INVALID_REQUEST`, `REMOTE_UNKNOWN`, `MODEL_MISMATCH`, `TASK_FAILED`, `ACCOUNT_BLOCKED`, `LOCAL`.

Die Zuordnung erfolgt über `telemetry.classify(exc)` für Exceptions und `telemetry.http_class(status)` für HTTP-Status. Beispiel aus den Tests: `HTTP 400` → `INVALID_REQUEST`. Jeder Fehler bekommt genau eine Klasse.
`MODEL_MISMATCH` existiert nur als Klasse. Welches Modell tatsächlich läuft, wird bewusst nicht weiter untersucht (Nutzerentscheidung vom 16.10.2026).

## 7. Retention und Aggregate

Bevor alte Events gelöscht werden, schreibt der Store Tageszählungen nach `event_rollup(day, kind, total)`. `telemetry.rollup(store)` liest sie aus.
Test: 2105 Events → 2001 behalten; behalten + aggregiert = 2105.

## 8. Abfragen

### Host-API

`GET /api/metrics?run_id=<run>` (Bearer) liefert `tasks[]` mit diesen Feldern:
`task_id, run_id, role, state, attempts, total, queue_wait, ready_wait, time_to_first_output, remote, after_stream, verify_messages_read, errors, fallbacks, attempt_log`.
Alle Zeiten in Sekunden:
- `time_to_first_output`: `remote_sent` → `first_output`
- `remote`: `remote_sent` → `remote_done`
- `after_stream`: `remote_done` → Abschluss

### SQL (nur lesend)

Verbindung: `sqlite3.connect('file:data/notiontool.db?mode=ro', uri=True)`

```sql
-- Zeit bis zur ersten Ausgabe je Task eines Runs
SELECT json_extract(doc,'$.task_id') AS task, json_extract(doc,'$.seconds') AS ttfo
FROM events WHERE run_id = ? AND kind = 'first_output';

-- Fehlgeschlagene Versuche nach Fehlerklasse
SELECT json_extract(doc,'$.error_class') AS cls, COUNT(*) AS n
FROM events WHERE kind = 'task_attempt_finished' AND json_extract(doc,'$.error_class') <> ''
GROUP BY cls;

-- Fallbacks
SELECT kind, COUNT(*) AS n FROM events
WHERE kind IN ('model_fallback','context_reset_fallback','approach_fallback') GROUP BY kind;

-- Tagesaggregate gelöschter Events
SELECT day, kind, total FROM event_rollup ORDER BY day, kind;
```

## 9. Latenz-Optimierungen in Phase 2

- **Modellkatalog-Cache:** `getAvailableModels` wird pro Workspace auf dem Transport des Kontos zwischengespeichert (`MODEL_CATALOG_TTL = 600` s, `notion.py`). Neu gelesen wird nur nach Ablauf oder wenn das gesuchte Label fehlt.
- **Chat-Konfiguration:** Eine bestätigte Konfiguration wird gemerkt, die erneute Prüfung entfällt.
- **Prüfung des eigenen Turns:** Gelesen wird nur das Thread-Ende, in Schritten von 20 Nachrichten, bis der eigene Schritt gefunden ist. Fehlt er, wird alles in Blöcken von höchstens 100 gelesen. Das Ergebnis ist identisch mit dem vollständigen Lesen (per Test belegt).

## 10. Nachweise

| Punkt | Status |
|---|---|
| Event-Kette, Fehlerklassen, Rollup, Cache, Turn-Prüfung, Koordinations-Event | LOKAL GEPRÜFT: 13 Tests in `tests/test_telemetry.py` |
| Bestehende Suite nach dem Patch | LOKAL GEPRÜFT: 193 Tests |
| Regression `test_harness_multi_main` (4 zusätzliche Commits pro Task bei `synchronous=FULL`) | LOKAL GEPRÜFT behoben: `task_queued`, `task_ready`, `lease_acquired`, `context_built` mit `commit=False`; sie werden mit dem nächsten `put` oder Event committet. Suite: 207 Tests OK (72,4 s) |
| I11-Sperre (180/min) | LOKAL GEPRÜFT: 8 Tests in `tests/test_rate_cap.py`; volle Suite 215 Tests OK (69,4 s, 12:48) |
| Neustart mit nur einer Instanz | LOKAL GEPRÜFT: `work\restart-harness.ps1` um 12:50:36 – alte PIDs 22160/17560 gestoppt, neue PID 22280 besitzt 8790, `instances=1` |
| Kette live nach dem Fix: Lauf `rb4fcbd450a684c5facd6f0e7a279668e` (3 Tasks, hg/d84a, 12:51) | LIVE GEPRÜFT: alle 11 Schritte je Task, Reihenfolge korrekt, `seq` und `ts` monoton, `messages_read` = 20. Vorlauf 3,05 s beim ersten Task nach dem Neustart, danach 0,82 s und 0,81 s |
| Kette live: 2 Läufe mit 6 Tasks auf hg@arisya.de (d84a), 16.10.2026 12:19 und 12:21 | LIVE GEPRÜFT: alle 11 Schritte vorhanden, Reihenfolge korrekt, keine Duplikate, `messages_read` = 20 |
| Vorlauf `task_started` → `remote_sent` | LIVE GEPRÜFT: 2,19 s beim ersten Task nach dem Neustart, danach 0,75–1,25 s, auch im zweiten Lauf. Cache und Konfig-Merker als Ursache sind plausibel, aber nicht einzeln gemessen, weil es keinen Zähler für `getAvailableModels` gibt. |
| `request_coordination_adjusted` live | NICHT GEPRÜFT: live trat kein Rate-Limit auf |
| `circuit_open` / `circuit_closed` | NICHT GEPRÜFT: erst Phase 3 |
| Ursache von `context_scan_waiting` | UNBEKANNT; seit dem Neustart kein solches Event |
| I11 live am Limit | NICHT GEPRÜFT (bewusst): Der Live-Lauf blieb weit unter 180/min, es gab kein `request_window_wait` |
| Fenster über einen Neustart hinweg | NICHT GEPRÜFT, bekannte Lücke: Das Fenster liegt nur im Speicher |

## 11. Rate-Limit I11 (180 Anfragen/Minute)

- `network_budget.py`: `HARD_LIMIT_PER_MINUTE = 180` mit gleitendem Fenster über 60 s (`WINDOW_SECONDS`). Jeder Start eines Notion-Requests läuft durch `NetworkBudget.acquire`.
- Pro Prozess gibt es genau eine `NetworkBudget`-Instanz (`NotionAdapter.__init__`). Sie gilt für alle Konten und Transporte, auch für Durchreichungen über Host-API und MCP (`nt_notion_call`, `nt_notion_stream`, `nt_api`).
- `clamp_limit` begrenzt jeden gespeicherten Wert auf 1–180. Ein Setting kann die Grenze senken, aber nie anheben.
- Überzählige Starts warten, bis im Fenster wieder Platz ist. Sie werden nie durchgelassen. Für jeden zurückgehaltenen Start entsteht genau ein Event `request_window_wait` mit `seconds` und `limit`.
- Wird während des Wartens abgebrochen, wird nichts gesendet.
- Vorher erlaubte das Start-Intervall von 0,25 s bis zu 240 Starts pro Minute. Das verletzte I11.
- Bekannte Lücke: Das Fenster liegt im Speicher und beginnt nach einem Neustart leer. Ein Neustart dauert mehrere Sekunden plus Edge-Vorbereitung, das Risiko ist daher gering. Abhilfe in Phase 3: das Fenster persistieren.
