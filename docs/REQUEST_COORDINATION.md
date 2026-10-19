# Request-Koordination (Phase 3)

Alle Notion-Requests eines Prozesses laufen durch genau eine `NetworkBudget`-Instanz (`notiontool/network_budget.py`). Sie gilt **global für alle Konten und Transporte**. Ein Limit pro Konto gibt es nicht. Phase 3 ergänzt das I11-Fenster aus Phase 2 um sechs Bausteine: persistiertes Fenster, Prioritäten, adaptive Parallelität, Backoff mit Jitter, Circuit Breaker pro Endpunkt und eine globale Pause bei gesperrtem Konto.

## 1. Globales Limit (I11)

- `HARD_LIMIT_PER_MINUTE = 180` gilt als gleitendes Fenster über 60 s. `clamp_limit` begrenzt jeden Wert auf 1–180. Ein Setting kann die Grenze senken, aber nie anheben.
- Ein Budget für alle Konten, Host-API- und MCP-Durchreichungen. Prioritäten, Parallelität und Breaker ändern nur Reihenfolge und Ablehnung, nie das Limit.

## 2. Persistiertes Fenster

- Jeder genehmigte Start wird in `settings/network_window` (`{id, starts[]}`, Unix-Sekunden) gespeichert, bevor `acquire` zurückkehrt.
- Der Snapshot entsteht unter `lock`, geschrieben wird unter `save_lock`. Die Sperrreihenfolge ist `save_lock` → `lock`; unter `lock` wird nie auf den Store zugegriffen.
- Beim Start lädt `_load_window` alle Starts der letzten 60 s, höchstens die letzten 180.
- Ein nicht lesbares Dokument zählt als volles Fenster (`[now] * limit`). Das ist fail-safe: Im schlimmsten Fall wird 60 s gewartet, das Limit wird nie gerissen.
- Folge: Nach einem Neustart zählen die Starts von vorher weiter. Kosten: ein `store.put` pro Start (SQLite mit `synchronous=FULL`, geschätzt 10–30 ms, nicht einzeln gemessen).

## 3. Prioritäten

| Stufe | Wert | Verwendung |
|---|---|---|
| `critical` | 0 | Task-Ausführung: Die Engine startet `adapter.run` über `run_with_priority('critical', ...)` |
| `verify` | 1 | Abgleich im Recover-Pfad (`notion.py`): Transcript lesen und eigenen Turn prüfen |
| `rest` | 2 | Alles andere, z. B. Durchreichungen über Host-API und MCP |

- Wartende werden nach (Priorität, Ankunft) bedient. Die Stufe gilt pro Thread (`threading.local`). `request_priority(...)` stellt beim Verlassen die vorherige Stufe wieder her; ungültige Werte werden zu `rest`.

## 4. Adaptive Parallelität

- Die Engine nutzt `_parallel_cap(configured)` statt `settings['per_account']`. Der Wert ist `min(configured, budget.parallel)`.
- **Senken:** Ein Fehler (`failed`) oder ein 429 halbiert den Wert (`(n+1)//2`, Minimum 1). Steigt die Latenz an (aktuelle EWMA > 2 × Basis-EWMA und mindestens +1 s), sinkt der Wert um 1, höchstens alle 10 s.
- **Turn-Fehler im Stream:** Beendet Notion einen Turn bei HTTP 200 mit einem Fehler (z. B. `temporarily-unavailable`), zählt der HTTP-Request als `ok`. Deshalb meldet `NotionAdapter.turn_failed` den Fehler zusätzlich über `budget.turn_failed(source, sub_type)`, und die Parallelität wird ebenfalls halbiert. Diese Lücke wurde im Live-Lauf gefunden (Abschnitt 11).
- **Anheben:** Nach `max(3, aktueller Wert)` Erfolgen in Folge steigt er um 1, bis zum konfigurierten Maximum.
- Event `request_coordination_adjusted` mit `reason` = `transient_error`, `turn_error` (zusätzlich `source`, `sub_type`), `rate_limited`, `latency` oder `recovered`.

## 5. Intervall und Backoff

- Das Start-Intervall liegt zwischen 0,25 s (Basis) und 2 s. Ein 429 verdoppelt es. Nach 60 s ohne 429 sinkt es bei jedem Erfolg um den Faktor 0,9, bis 0,25 s.
- Gespeichert wird, sobald das Intervall wieder auf der Basis ist oder 30 s seit der letzten Speicherung vergangen sind (Event mit `reason = interval_recovered`).
- `backoff(attempt, base, cap, floor, jitter)`: exponentiell mit multiplikativem Jitter. Der Cap gilt nach dem Jitter.
  - 429-Sperre: `backoff(strikes-1, 30, 300, floor=Retry-After, jitter=0.25)`
  - Task-Retry in der Engine: `backoff(attempts, 1, 60)` statt `2**attempts`

## 6. Circuit Breaker pro Endpunkt

- Quelle ist der Request-Pfad.
- **closed → open:** nach 5 aufeinanderfolgenden `failed` (Netzwerkfehler oder HTTP ≥ 500). 429 und `invalid` zählen nicht, ein Erfolg setzt den Zähler zurück.
- **open:** `acquire` wirft sofort `CircuitOpen`, gesendet wird nichts. Die Dauer ist `backoff(trips-1, 30, 300, jitter=0.25)`: 30 s, dann 60 s, höchstens 300 s.
- **half_open:** Genau ein Probe-Request ist erlaubt, weitere Aufrufer erhalten `CircuitOpen(5)`.
  - Probe ok → closed (`circuit_closed`).
  - Probe failed → erneut open mit längerer Dauer.
  - Hängt die Probe länger als 120 s, wird die nächste Probe freigegeben.
- Events: `circuit_open` (source, failures, seconds, trips, status) und `circuit_closed` (source, open_seconds).

## 7. Globale Pause bei gesperrtem Konto

- Ein 401/403 (`blocked`) oder die Fehlerklasse `ACCOUNT_BLOCKED` ruft `pause(...)` auf. Danach werden **alle** Remote-Calls aller Konten mit `RemotePaused` abgelehnt, auch bereits wartende.
- Der Zustand wird in `settings/network_budget.paused` gespeichert und übersteht Neustarts.
- Es gibt kein automatisches Fortsetzen. Fortgesetzt wird manuell über `POST /api/coordination/resume` oder über Retry auf dem blockierten Lauf (`POST /api/run/retry`).
- Events: `remote_paused` (`error_class` = `ACCOUNT_BLOCKED`) und `remote_resumed` (paused_seconds).

## 8. Engine-Verhalten und I5

- **Nach `submission_phase == 'inference'`:** `CircuitOpen` oder `RemotePaused` wird wie `PendingRemote` behandelt. Es wird nie neu gesendet. Der Task wird `uncertain`, bei Überfälligkeit `blocked` mit `remote_unknown`. Der Abgleich startet frühestens nach `exc.seconds`.
- **Davor (nichts gesendet):**
  - `CircuitOpen` → Status `retry` ohne Versuchsverbrauch, `next_at` = jetzt + `seconds`.
  - `RemotePaused` → Status `blocked` mit `paused_by = 'account_blocked'`, ohne Versuchsverbrauch.
- `resume_remote()` stellt pausierte Tasks wieder ein. Betroffene Läufe werden fortgesetzt, sofern kein Task `failed`, `blocked` oder `remote_unknown` ist.
- Fehlerklassen: `CircuitOpen` → `TRANSIENT`, `RemotePaused` → `ACCOUNT_BLOCKED`.

## 9. Host-API

- `GET /api/coordination` liefert paused, pause, limit_per_minute, window_count, interval, cooldown_seconds, parallel, parallel_max und circuits.
- `POST /api/coordination/resume` hebt die Pause auf und stellt betroffene Tasks und Läufe wieder ein.
- `/api/limits` enthält zusätzlich `coordination`, `pause` und `paused`.

## 10. Bekannte Lücken

- `vendor/transport.py` (Altpfad) nutzt `SHARED_BUDGET` mit `acquire` und `limited`, ruft aber nie `release` auf. Für diesen Pfad gelten Fenster und Limit, Breaker und Parallelität dagegen nicht. Die Datei bleibt bewusst unverändert.
- Scheitert `_start` nach `acquire`, wird das Ticket nicht freigegeben. Eine hängende Probe gibt sich nach 120 s selbst frei.
- `remote_sent` kann geloggt werden, obwohl `CircuitOpen` erst beim Stream-Acquire greift. Das ist kosmetisch, gesendet wird nichts.
- Turn-Fehler im Stream zählen nicht für den Breaker, weil der Endpunkt geantwortet hat. Sie senken nur die Parallelität. Die Engine kühlt den Task über `RateLimited(20)` ab (live beobachtet: etwa 20 s zwischen den Versuchen). Ein eigener Breaker für gehäufte `temporarily-unavailable`-Fehler ist nicht umgesetzt.

## 11. Nachweise

Stand: 16.10.2026. Die Punkte stammen aus dem Live-Betrieb des Operators vom 16.10.2026.

- Eingefrorener Tab, vor Fix K: `setTimeout(0)` im versteckten Transport-Tab bekam keine Antwort. 5 bereits beendete Streams blieben offen, bis der Timeout von 900 s griff.
- Nach dem Wecken über `Page.setWebLifecycleState` (`notiontool/edge_transport.py:52-56`): `setTimeout(0)` antwortete in 0 ms.
- Nach dem Neustart mit dem Fix: 3 Tasks im Zustand `uncertain` wurden in jeweils etwa 2 s geprüft.
- Fix L: Der Review ging vom busy Main an einen Coworker (Event `reviewer_fallback`, `notiontool/engine.py:494`).
