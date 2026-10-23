# Fix-Aufträge aus dem Feature-Review (23.10.2026)

Quelle: `docs\FEATURE_REVIEW.md`. Diese Liste ist für den **echten Main**. Er verteilt sie über `/api/operator/plan` und danach `/api/operator/delegate` (I12). Ich habe nichts delegiert und nichts am Code geändert.

Jeder Auftrag hat: Ziel, Fakten, Dateien, Modell/Effort (I35), DIRECT oder TEAM (I19/I44), Abnahme (I42: Test + Verdrahtung + Live-Backtest, wo es passt).

## Reihenfolge (DAG)

```
Welle 1 (parallel):  A1  A2  A4  A5  A6        A9  A10  A11  A12  A13
Entscheidung Owner:  A3 (1 oder 2 Review-Runden)
Welle 2:             A3 (nach Entscheidung)
Welle 3:             A7 Merge-Sperre (wenn A1–A6 grün sind)
Welle 4:             A8 Zeilenenden (wenn A7 aktiv und Konflikte abgebaut)
Zuletzt:             A14 Aufteilen großer Funktionen
```

A4 und der Archiv-Fix des echten Mains hängen zusammen. Bitte zusammen planen.

---

## A1 – Pool-Tests hängen (7 rote Tests) – TEAM, Opus 5.5 / high

**Ziel:** Alle 7 Tests in `tests/test_limits.py` (Klasse `EightSlotTests`) wieder grün.

**Fakten:**
- Auch allein rot. Beispiel `test_per_slot_cap_three_queues_remaining_work`: Wartet in Zeile 56 auf `adapter.executing == 24`. Das wird in 9 s nie erreicht. Der Run bleibt `executing` ohne Fehler.
- Die Tasks kommen also gar nicht beim Fake-Adapter an. Sie hängen vorher (Planung, Queue, Budget oder Slot-Auswahl).
- Meine frühere Vermutung (`INVALID_REQUEST` im Fake) stimmt hier **nicht**. Es gibt keinen Fehler im Run.
- Verdächtige Commits (bitte mit `git bisect` prüfen): `9d528b2` (22.10., Design-Task überspringen), `91f369a` (23.10., est_s pro Plan-Teil), `3b6e907` (23.10., Zustands-Tabelle).

**Dateien:** `tests/test_limits.py`, `tests/test_engine.py` (`pump`, Z. 123), `notiontool/engine.py`, `notiontool/operator_main.py`, Netz-Budget/Koordination.

**Hinweis zum Starten:** `test_limits.py` importiert `test_engine`. Einzeln nur so starten: `python -m unittest discover -s tests -p test_limits.py -k <name>`.

**Abnahme:** Alle 7 Tests grün, allein und im vollen Lauf. Ursache in einem Satz im Commit.

## A2 – Tests stören sich gegenseitig (6 rote Tests) – DIRECT, Sonnet 5.5 / medium

**Ziel:** `tests/test_backtest_limits.py` ist auch im vollen Lauf grün.

**Fakten:** Allein laufen alle 8 Tests grün (14 s). Im vollen Lauf meldet der Guard echte Spuren: urllib, Socket auf `127.0.0.1:8790` (App-Port) und die echte `data\notiontool.db`. Ein anderer Test öffnet also echte Verbindungen.

**Weg:**
1. Den Test finden, der echte Verbindungen öffnet (Guard in jeden Test einbauen und loggen).
2. Den Guard als Fixture für alle Tests nutzen, nicht nur als `test_0`.
3. pytest-Fehler gleich mit beheben (Reihenfolge darf egal sein).

**Abnahme:** Voller unittest-Lauf und `pytest` grün für diese Datei, auch während die App läuft.

## A3 – Review- und Fix-Runden (2 rote Tests) – erst Entscheidung, dann DIRECT, Opus 5.5 / high

**Owner muss entscheiden:** Sind 1 oder 2 Review-/Fix-Runden richtig?

**Fakten:** `test_engine.test_failed_review_never_marks_done` und `test_review_economy.test_fix_rounds_limit_unchanged_and_main_decides` erwarten beide 2, der Code liefert 1.

**Wichtig:** Der erste Test prüft, dass ein durchgefallenes Review nie zu `done` führt. Das muss **in jedem Fall** gelten. Nur die Zahl der Runden ist offen.

**Abnahme:** Code und beide Tests passen zur Entscheidung. Ein Live-Backtest mit absichtlich schlechtem Ergebnis endet nicht bei `done`.

## A4 – Neuer Chat behält „Welcome to Notion“ (1 roter Test) – TEAM mit dem Archiv-Fix, Opus 5.5 / high

**Fakten:** `test_native.test_confirmed_creation_is_recovered_without_duplicate`: erwartet Titel `Main`, bekommt `Welcome to Notion`. Gleiche Gegend wie der Archiv-Fehler beim Main-Wechsel. Seit Notion v23.13 fehlen Titel in der Chat-Liste.

**Abnahme:** Test grün. Live: neuer Chat hat den richtigen Titel, alter Main heißt „Archive“.

## A5 – `INVALID_REQUEST` wird wiederholt statt blockiert (1 roter Test) – DIRECT, Sonnet 5.5 / medium

**Fakten:** `test_telemetry.test_failed_attempt_records_its_error_class`: Task bleibt `retry` mit `presend_retries: 1`, statt `blocked`. `INVALID_REQUEST` kommt aus `telemetry.py` (HTTP 400/404).

**Vorschlag:** HTTP 400 ist ein Fehler in der Anfrage. Ein Wiederholen bringt meist nichts. Entweder `INVALID_REQUEST` von `presend_retries` ausnehmen oder den Test bewusst anpassen. Bitte begründen.

**Abnahme:** Test grün, Verhalten im Commit begründet.

## A6 – Drei kleine Test-/UI-Abgleiche – DIRECT, Sonnet 5.5 / low

1. `test_long_calls.test_sync_limit_setting_is_clamped`: Code nutzt jetzt 10–55 s, Standard 45 s. Test anpassen.
2. `test_page_defaults.test_page_shows_the_server_defaults`: Die Einstellungs-Seite zeigt `idle_mode` nicht. Seite ergänzen (nicht den Test löschen).
3. `test_problems.test_server_route_and_install_keep_crlf`: Zeilenenden in `server.py`/Installer geprüft und korrigiert (R8: CRLF-Dateien per Byte-Patch).

**Abnahme:** Alle drei grün.

## A7 – Merge-Sperre bei roten Tests – TEAM, Opus 5.5 / high

**Ziel:** Der Patch-Modus merged nur, wenn die betroffenen Testdateien grün sind.

**Fakten:** 20 rote Tests im Haupt-Branch, Merges liefen trotzdem durch. 14 Konflikte warten.

**Abnahme:** Test mit absichtlich rotem Test → Merge wird abgelehnt, Event im Log, Hinweis im UI.

## A8 – Einheitliche Zeilenenden – DIRECT, Sonnet 5.5 / medium

**Fakten:** 12 Module mit CRLF, Rest LF (chat_context, devtools, edge_transport, harness_account, mains, mcp_servers, no_console, notion, perfection, server, verifier, window_login).

**Weg:** `.gitattributes` anlegen und in einem eigenen Commit umstellen, wenn keine Patch-Kopien mehr offen sind. Danach R8 und `test_problems` anpassen.

**Abnahme:** Voller Testlauf grün, keine offenen Patch-Kopien kaputt.

## A9 – Sicherheit G1 und G2 – TEAM, Opus 5.5 / high

**Ziel:** Prüfen und schließen: G1 (Operator-Key über öffentliche Funnel-Route) und G2 (`host_api_bind` erlaubt `0.0.0.0`). Danach eine Liste G1–G14 mit Status in `docs/SECURITY_AUDIT.md`.

**Abnahme:** Test pro Lücke, Status-Tabelle gepflegt.

## A10 – DB und Main-Context – TEAM, Opus 5.5 / medium

**Fakten:** DB 215 MB, `thread_context` 173 MB in 20 Objekten. Main 20 bei ~4,1 Mio Tokens.

**Ziel:** Alte Context-Blobs kürzen oder auslagern. Ein hartes Limit auch für Mains mit Warnung im UI.

**Abnahme:** DB deutlich kleiner, Test für das Limit, Live: Main über Limit wird gemeldet.

## A11 – Upkeep für blockierte Coworker – DIRECT, Sonnet 5.5 / medium

**Fakten:** Coworker 15 (41 h), 7 (27,7 h), alter 3 (23,9 h) noch da. Regel I32: max. 23 h.

**Abnahme:** Test mit blockiertem, altem Coworker → wird ersetzt. Live: kein Coworker über 23 h.

## A12 – Echtes Modell pro Turn messen – DIRECT, Sonnet 5.5 / medium

**Ziel:** Nach jedem Turn das wirklich genutzte Modell und Effort als Event speichern und mit dem Wunsch vergleichen.

**Abnahme:** Event vorhanden, Abweichung sichtbar im UI.

## A13 – Aufräumen – DIRECT, Sonnet 5.5 / low

- `.bak`-Dateien entfernen: `notiontool\web\desktop\shell.js.bak-20261021-185207`, `studio.js.bak-20261021-185618`, `main.py.bak_quit`.
- `README.md` auf den Stand bringen (1,2 Mio Tokens / 600 Turns statt 60k / 80).

## A14 – Große Funktionen aufteilen – TEAM, Opus 5.5 / high (zuletzt)

`server.action` (526 Z.), `engine._complete` (329 Z.), `notion.run` (215 Z.). Erst wenn alle Tests grün sind. Leere `except … pass` dabei durch Log-Einträge ersetzen.

**Abnahme:** Verhalten gleich, alle Tests grün.

---

## Nachtrag 24.10., 00:10 – Entscheidung des Owners

> „Review-Runde kann 2, aber es muss alles schnell laufen. Das Design-System muss maximal schnell laufen.“

Damit ist **A3 entschieden** und es gibt einen neuen Auftrag **A15**. A3 und A15 gehören in **Welle 1**.

### Messwerte der letzten 7 Tage (DB, nur Lesen, `work\_review_speed_ro.py`)

| Rolle | Tasks | Laufzeit Median | Laufzeit p90 | Wartezeit Median | Wartezeit p90 |
|---|---|---|---|---|---|
| executor | 539 | 214 s | 848 s | 7 s | 664 s |
| reviewer | 235 | 102 s | 238 s | 3 s | 157 s |
| architect | 17 | 216 s | 539 s | 3 s | 14 s |
| planner | 13 | 49 s | 126 s | 19 s | 415 s |

- **Design-Tasks:** 68 Stück, Median **216 s**, p90 **799 s**, max 1202 s. Modell fast immer Opus: `opus/high` 42-mal, `opus` ohne Effort 18-mal, `opus/max` 2-mal. Nur 2-mal Sonnet.
- **Reviewer pro Run:** 97 Runs ohne Review, aber **22 Runs mit 5 oder mehr Reviews**.
- **Review-Anteil an der Task-Zeit:** Median 0 %, p90 37 %, max 95 %.
- **Run-Start bis erster Task:** Median 3 s, p90 139 s, max 2522 s.
- **Bremsen im Netz:** `push_slow` 303, `turn_stalled` 183 im gespeicherten Zeitraum.

### A3 (entschieden) – Review: maximal 2 Runden, so schnell wie möglich – DIRECT, Opus 5.5 / high

**Regeln:**
1. **Höchstens 2 Review-Runden pro Task.** Runde 2 nur, wenn Runde 1 durchfällt.
2. **Nach Runde 2 durchgefallen:** Der Task wird nie `done`. Er geht zum Main (`awaiting_main`) mit der Begründung.
3. **Reviewer-Modell nach Risiko (I35):** normaler Code → Sonnet 5.5 / medium. Nur Kern-Logik, Sicherheit oder Daten-Migration → Opus 5.5 / high.
4. **Review sofort starten,** ohne Warteschlange hinter Executor-Arbeit (Priorität `verify`). Ziel: Review-Wartezeit p90 unter 30 s (heute 157 s).
5. **Ein Review prüft den ganzen Task.** Nicht pro Datei oder Teil neu reviewen (Grund: Runs mit 5+ Reviews).
6. **Kurze Review-Prompts:** nur Diff, Abnahme-Kriterien und Testergebnis senden, nicht den ganzen Kontext.

**Tests:** `test_engine.test_failed_review_never_marks_done` und `test_review_economy.test_fix_rounds_limit_unchanged_and_main_decides` auf 2 Runden anpassen bzw. Code auf 2 Runden bringen. Neuer Test: „nach 2 roten Runden → awaiting_main, nie done“.

**Abnahme:** Tests grün. Live-Backtest mit absichtlich schlechtem Ergebnis: nach 2 Runden `awaiting_main`. Review-Wartezeit p90 unter 30 s, kein Run mit mehr als 2 Reviews pro Task.

### A15 (neu) – Design-Schritt maximal schnell – TEAM, Opus 5.5 / high (Umsetzung), Sonnet 5.5 / medium (Review)

**Ziel:** Der Design-Schritt im Plan bremst die Runs nicht mehr. Zielwerte: Median **unter 60 s** (heute 216 s), p90 **unter 180 s** (heute 799 s).

**Wege (in dieser Reihenfolge prüfen):**
1. **Überspringen, wenn möglich:** Die Einstellung `skip_design_when_specified` (Commit `9d528b2`) auch für UI-Arbeit nutzen, wenn der Auftrag schon Aussehen und Ort nennt. Kleine Änderungen (Farbe, Text, Abstand) brauchen kein Design.
2. **Schnelleres Modell:** Design standardmäßig mit Sonnet 5.5 / medium. Opus 5.5 / high nur für neue Bildschirme oder große Umbauten. Nie `opus/max` für Design.
3. **Parallel statt davor:** Der Executor startet sofort mit Backend- und Logik-Teilen. Nur die reinen UI-Teile warten auf das Design.
4. **Kurzes Ergebnis:** Design liefert eine feste, kurze Vorlage (Komponenten, Farben, Abstände, max. ~40 Zeilen) statt langer Texte.
5. **Wiederverwenden:** Ein festes Design-Profil der Desktop-App (Farben, Abstände, Komponenten) einmal anlegen und mitschicken, statt es jedes Mal neu zu erfinden.
6. **Zeitlimit:** Design-Task nach 180 s abschließen mit dem, was da ist. Event `design_timeout` speichern.

**Dateien:** `notiontool/operator_main.py` (Design-Task, `skip_design_when_specified`), Effort-/Modell-Policy, Task-Router, Prompt für die Rolle `architect`/Design.

**Messung:** Event mit Design-Dauer, Modell und „übersprungen ja/nein“. Ein kleines Skript wie `work\_review_speed_ro.py` zeigt Median und p90.

**Abnahme:** Tests für Überspringen, Modellwahl und Zeitlimit grün. Live-Backtest mit 1 kleiner und 1 großer UI-Aufgabe: Die kleine überspringt Design, die große ist unter 180 s fertig. UI im Desktop-Fenster prüfen (Screenshot).

### Allgemeine Tempo-Regeln für alle Aufträge

- Jeder Auftrag misst vorher und nachher seine Laufzeit. Ein Fix, der etwas langsamer macht, ist nicht fertig.
- Teure Modelle (Opus high/max) nur, wo das Risiko es braucht.
- Wartezeiten in der Queue (Executor p90 664 s, Planner p90 415 s) sind genauso wichtig wie die Laufzeit. A1 (Pool hängt) und A7 (Merge-Sperre) dürfen keine neue Wartezeit erzeugen.
