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

---

## Nachtrag 24.10., 00:55 – Richtigstellung: gemeint ist das Rollen-System

> „Ich meinte generell, dass alles durch Rollen bis zur Bewertung geht. Schwere Prompts dauern sehr lange, und es läuft nur ein Coworker gleichzeitig. Der ganze Durchlauf soll schneller gehen und trotzdem bessere Qualität bringen.“

**A15 wird durch A16 ersetzt.** Die Punkte aus A15 zum Design-Schritt gehen in A16 auf.

### Messwerte (7 Tage, DB nur lesen: `work\_review_parallel_ro.py`, `work\_review_parallel2_ro.py`)

| Was | Wert | Bedeutung |
|---|---|---|
| Gleichzeitige Tasks (wenn etwas läuft) | Median **2**, p90 5 | |
| Zeit, in der nur **1** Task läuft | **39 %** | Das bestätigt „nur ein Coworker“ |
| Arbeit ÷ Wanduhr pro Run | Median **1,7** | Runs laufen fast hintereinander |
| `worker_budget` = 1 | **60 von 163 Runs** | Viele Runs dürfen nur 1 Coworker nutzen |
| Tasks mit Abhängigkeit | Median 40 %, p90 90 % | Viele Teile warten unnötig |
| Notion-Rate-Limits (429) | **0** | Das Netz bremst **nicht** |
| Adaptives Parallel-Limit | nur 3–5, 6-mal wegen Latenz gesenkt | Nicht die Hauptbremse |
| Typische Rollen-Kette | `executor > reviewer > executor > reviewer > executor > reviewer (> executor)` | 3–4 Fix-Runden hintereinander |
| Reviewer-Modell | `opus/high` 231 von 236 | Jedes Review teuer und langsam |
| Executor-Modell | `opus/high` 332, `opus` 134, `opus/max` 27, Sonnet ~50 | Fast alles Opus |
| Executor-Dauer nach Prompt-Länge | < 2000 Zeichen: Median **152 s**; 2000–6000: Median **383 s** | Schwere Prompts = 2,5-mal langsamer |
| Review-Urteile (grob gezählt) | fail 87, pass 24, unklar 119 | Viele Runden, weil Runde 1 oft durchfällt |

### Ursachen (nach Wirkung sortiert)

1. **Zu kleines Worker-Budget.** 60 Runs bekamen `worker_budget` 1. Dann läuft wirklich nur ein Coworker, egal wie viele Slots frei sind.
2. **Zu viele Abhängigkeiten im Plan.** Teile, die andere Dateien betreffen, warten trotzdem aufeinander.
3. **Serielle Fix-Schleife mit bis zu 4 Runden.** Jede Runde ist ein neuer schwerer Opus-Prompt. Mit A3 (max. 2 Runden) wird das schon kürzer.
4. **Runde 1 fällt zu oft durch.** Grund: unklare Abnahme-Kriterien und kein Maschinen-Check vor dem Review. Das kostet Zeit **und** Qualität.
5. **Schwere Prompts.** Ein langer Kontext wird in jeden Task kopiert. Ab 2000 Zeichen dauert ein Task 2,5-mal so lange.
6. **Opus für fast alles,** auch für Reviews von normalem Code und für Design.

## A16 – Rollen-Durchlauf schneller **und** besser – TEAM, Opus 5.5 / high (Umsetzung), unabhängiges Review (I14)

Das ist jetzt der **wichtigste Auftrag**. Er gehört in Welle 1, zusammen mit A1 und A3.

### Teil 1 – Mehr Coworker gleichzeitig
- Die Stelle finden, an der `worker_budget` gesetzt wird. Ein Run mit 2 oder mehr unabhängigen Teilen bekommt mindestens so viele Coworker, wie es unabhängige Teile gibt (bis zur Slot-Grenze).
- `worker_budget` 1 nur bei echten Ein-Teil-Aufgaben.
- Der Planer setzt `depends_on` nur bei echter Datei-Überschneidung oder echter Ergebnis-Abhängigkeit. Teile mit verschiedenen Dateien laufen parallel.
- Rollen überlappen: Das Review von Teil A läuft, während Teil B noch gebaut wird.

### Teil 2 – Weniger Runden durch bessere erste Runde
- **Maschinen-Check vor jedem AI-Review:** betroffene Tests, `nt_test_app check` und Syntax/Lint laufen automatisch. Ist das rot, geht es direkt zurück an den Executor, ohne Review-Prompt.
- **Klare Abnahme-Kriterien** sind Pflicht in jedem Plan-Teil (welcher Test, welches Verhalten). Ohne Kriterien startet kein Task.
- Der Executor bekommt dieselben Kriterien und prüft sich selbst, bevor er abgibt.
- **Fix im selben Coworker-Chat:** Der Kontext bleibt da, nur die Review-Punkte werden nachgeschickt, kein neuer schwerer Prompt.
- Max. 2 Review-Runden (A3).

### Teil 3 – Leichtere Prompts
- Gemeinsamer Run-Kontext liegt **einmal als Datei** im Run-Ordner. Der Prompt nennt nur den Pfad und die betroffenen Dateien.
- Zielgröße: Executor-Prompt unter 2000 Zeichen im Median (heute p90 2984, max 50485).
- Reviewer bekommt nur: Diff, Abnahme-Kriterien, Ergebnis vom Maschinen-Check.

### Teil 4 – Modell nach Risiko (I35)
- Executor: Sonnet 5.5 / high für normale, klar beschriebene Teile. Opus 5.5 / high für Kern-Logik, Nebenläufigkeit, Sicherheit, Migration. `opus/max` nur auf ausdrücklichen Wunsch.
- Reviewer: Sonnet 5.5 / medium für normalen Code, Opus 5.5 / high für riskante Teile.
- Architect/Design nur bei 3+ Teilen oder neuer Oberfläche, sonst überspringen. Wenn er läuft: kurze feste Vorlage, Zeitlimit 180 s.
- Das echte Modell pro Turn messen (A12), damit die Wahl auch wirklich ankommt.

### Qualität darf nicht sinken – Schutzregeln
- Das unabhängige Review (I14) bleibt für jeden Code-Task. Nur das Modell wird nach Risiko gewählt.
- Ein Task wird nie `done` ohne grünen Maschinen-Check **und** bestandenes Review.
- Sinkt die Erstrunden-Quote oder steigt die Zahl der Nacharbeiten nach `done`, wird das Modell für diese Art von Task automatisch wieder erhöht.

### Messung und Ziele (vorher/nachher mit den Skripten oben)

| Kennzahl | Heute | Ziel |
|---|---|---|
| Zeit mit nur 1 Task (wenn etwas läuft) | 39 % | **unter 15 %** |
| Arbeit ÷ Wanduhr pro Run (Median) | 1,7 | **mindestens 3** |
| Runs mit `worker_budget` 1 bei 2+ Teilen | viele | **0** |
| Review besteht in Runde 1 | ~10–20 % (grob) | **mindestens 60 %** |
| Review-Runden pro Task | bis 4 | **max. 2** |
| Executor-Prompt Median | ~1000 Zeichen, lange Ausreißer | **unter 2000, max 6000** |
| Dauer eines Runs mit 4 Teilen | messen | **mindestens 40 % schneller** |
| Nacharbeit nach `done` | messen | **nicht höher als heute** |

Dazu kommen eigene Events für `review_first_pass` (ja/nein), `machine_check` (grün/rot, Dauer) und `worker_budget` (Wert, Anzahl unabhängiger Teile).

### Dateien (Startpunkte)
`notiontool/operator_main.py` (Plan, Delegation, `worker_budget`, Design-Skip), `notiontool/engine.py` (Review-Schleife, `_complete`), Prompt-Bau für Coworker, Modell-/Effort-Policy, `notiontool/network_budget.py` (nur prüfen, ist nicht die Hauptbremse).

### Abnahme
- Tests für: Budget gleich Anzahl unabhängiger Teile, kein `depends_on` ohne Datei-Überschneidung, Maschinen-Check vor Review, Fix im selben Chat, Modellwahl nach Risiko, max. 2 Runden.
- Live-Backtest: derselbe Auftrag mit 4 Teilen einmal vorher und einmal nachher. Gemessen werden Dauer, Parallelität, Runden und Ergebnis-Qualität (Tests grün, Review-Urteil).
- Die Ziel-Tabelle vorher und nachher ausfüllen.

### Reihenfolge innerhalb von A16
1. Teil 1 (Budget + Abhängigkeiten): schnellster Gewinn.
2. Teil 2 (Maschinen-Check + Fix im selben Chat): spart Runden und verbessert die Qualität.
3. Teil 3 und Teil 4 parallel.

---

## Fester Mess-Auftrag für A16 (vorher/nachher gleich)

Zweck: Derselbe Auftrag läuft **vor** und **nach** A16. So ist der Gewinn bei Tempo und Qualität klar messbar. Er läuft in einem Wegwerf-Ordner und ändert keinen Harness-Code.

**Ordner:** `C:\Users\liket\Downloads\NotionTool-harness\NotionTool\work\bench\a16\<datum-uhrzeit>\` (jedes Mal neu)

**Text für `/api/send` (immer wörtlich gleich):**

```
Mess-Auftrag A16. Projektordner: work\bench\a16\<datum-uhrzeit>. Baue 4 unabhängige kleine Python-Module mit pytest-Tests. Jedes Modul hat eine eigene Datei und eine eigene Testdatei, es gibt keine gemeinsamen Dateien.
1. durations.py: parse("1h30m") -> 5400, unterstützt d/h/m/s, Fehler bei leerem oder falschem Text (ValueError). Mindestens 8 Tests.
2. slugify.py: slugify("Ärger & Co. 2026!") -> "aerger-co-2026", Umlaute ersetzen, max. 60 Zeichen, nie Bindestrich am Anfang/Ende. Mindestens 8 Tests.
3. csvsum.py: summarize(pfad) -> {spalte: {min, max, mean}} nur für Zahlenspalten, leere Zellen ignorieren, Datei ohne Zahlen -> {}. Mindestens 6 Tests mit tmp_path.
4. ratelimit.py: TokenBucket(rate, capacity, clock) mit allow() -> bool, Uhr injizierbar, keine echten sleeps in Tests. Mindestens 6 Tests.
Abnahme: python -m pytest im Projektordner ist grün, keine Abhängigkeiten außer der Standardbibliothek und pytest.
```

**Erwartung an einen guten Durchlauf:** 4 Teile ohne `depends_on`, `worker_budget` 4, 4 Coworker gleichzeitig, kein Architect, Modell Sonnet 5.5 für Executor und Review, höchstens 2 Review-Runden pro Teil.

**Messprotokoll (pro Lauf eine Zeile):**

| Datum | Run-ID | Wanduhr bis done | max. gleichzeitig | Arbeit ÷ Wanduhr | worker_budget | Runden pro Teil | Review Runde 1 bestanden | Modelle | pytest | Bemerkung |
|---|---|---|---|---|---|---|---|---|---|---|
| vorher | | | | | | | | | | |
| nachher | | | | | | | | | | |

Werte aus `/api/run?id=<run>&compact=1` und `work\_review_parallel_ro.py` (nur lesen).

**Erfolg:** Wanduhr mindestens 40 % kürzer, max. gleichzeitig 4, Review Runde 1 bei mindestens 3 von 4 Teilen bestanden, pytest grün.

## Hinweis Main 20

`/api/chats` (24.10., 01:20): **Main 20** steht bei rund **10,2 Mio Tokens** in 44 Turns (vor ein paar Stunden noch ~4,1 Mio). Das ist weit über jedem sinnvollen Limit. Er sollte keine neuen Aufträge bekommen, bevor A10 (Context-Limit für Mains) erledigt oder Main 20 gewechselt ist.
