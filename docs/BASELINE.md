# Baseline (Phase 0)

Quelle: `data\notiontool.db`, nur lesend ausgewertet (16.10.2026). Daten stammen vom Konto `d84a51c7…` (15./16.10.2026). Es wurden keine neuen Live-Runs gestartet. Alle Werte: LOKAL GEPRÜFT (aus vorhandenen Daten).

## Runs

| Kennzahl | Wert |
|---|---|
| Runs gesamt | 7 (6 done, 1 cancelled) |
| Dauer done-Runs (created → updated) | 25,2 s · 28,0 s · 94,3 s · 135,1 s · 1503,6 s · 1540,2 s |
| Median | 114,7 s |
| Tasks pro Run | 3 · 3 · 3 · 5 · 6 · 9 |

Hinweis: Die beiden ~25-min-Runs enthalten laut Gedächtnis manuelle Retries während der Fehlersuche; sie sind keine normale Laufzeit.

## Tasks

| Kennzahl | Wert |
|---|---|
| Tasks gesamt | 37 (36 done, 1 blocked) |
| Rollen | executor 14 · planner 7 · context_compactor 7 · integrator 6 · reviewer 3 |
| Dauer (letzter Versuch, n = 29) | Median 10,5 s · p90 46,1 s · Max 48,1 s |
| Versuche | 25× 1 · 4× 2 · 1× 3 (17 % mit Wiederholung) |
| Angefordertes Modell | opus 19 · haiku 11 · ohne 7 |
| Modell-Verifikation | 37/37 `fallback` (ausgeführt: almond-croissant-low) |

## Events

| Kennzahl | Wert |
|---|---|
| Events gesamt | 374 in ca. 17,3 h |
| context_scan_waiting | 136 (36 %) |
| task_attempt_finished / task_started / task_finished | 56 / 41 / 29 |
| model_fallback | 40 |
| remote_turn_failed (temporarily-unavailable) | 3 |
| model_verification_failed | 3 |
| context_rotated | 7 |

## Speicher

| Kennzahl | Wert |
|---|---|
| DB-Größe | 87,5 MB |
| davon Freelist | 33,1 MB |
| thread_context | 53,7 MB in 16 Objekten |
| tasks | 0,28 MB in 37 Objekten |
| events | 0,05 MB |

## Lokal

| Kennzahl | Wert |
|---|---|
| Unit-Tests | 192 OK in 75,4 s |

## Was fehlt (nicht messbar ohne Phase 2)

- Zeit bis zum ersten Token und reine Remote-Dauer pro Turn (keine `remote_sent`/`first_output`-Events).
- Wartezeit in der Queue und Dauer der Vorab-Reads (notion.py:577–597).
- Zeitanteil Planung vs. Ausführung vs. Review pro Run.
- Vergleich mit einem einzelnen Welcome-Chat ohne Orchestrierung (B0): erst in Phase 9, schlank (6 Aufgaben × 3 Läufe).
