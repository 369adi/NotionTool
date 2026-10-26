# NotionTool Studio - Plan fuer eigene App + Redesign (2026-10-25)

Status: Plan, noch nichts gebaut. Planung allein gemacht (/api/operator/plan: 5400 s allein vs 5508 s Team, plan_id plb4ef493698e9).

## 1. Ziel
Eigene Desktop-App. Alles, was mit Notion-Daten zu tun hat (Chatten, Modelle, Accounts, Threads, Settings), laeuft ueber die Notion-API (/api/v3) im NotionTool-Backend. Die UI ist komplett unsere eigene. Die echte Notion-App ist nur noch Werkzeug fuer Reverse Engineering und Fallback (Notion-Seite oeffnen).

## 2. Warum (Fehler, die heute von der echten Notion-App kommen)
| Fehler heute | Ursache | Weg mit eigener App |
|---|---|---|
| Chats laden komplett, Lag | Notion rendert alle Messages, wir verstecken nur per DOM | Wir laden nur die letzten 5 Turns, Load older per API |
| Auto als Modell | Notion-UI setzt Modell selbst, wir pinnen per Fetch-Pause | Wir schicken model + modelFromUser selbst im Body |
| Refresh-Leiste, Leiste verschoben | Overlay im fremden React-DOM | Eigenes Layout |
| stepcap/Overlay brechen bei Notion-Update | Selektoren aendern sich | Nur API-Vertrag muss stimmen |
| Neustart NotionTool stoert Tasks | Overlay + CDP haengen am Notion-Renderer | Backend und UI getrennt, MCP/Gateway laeuft weiter |

## 3. API-Inventar (Stand)
| Funktion | Notion-Methode | Status |
|---|---|---|
| Chat senden + Stream | runInferenceTranscript (NDJSON patch-start/patch/patch-sync) | LIVE GEPRUEFT |
| Antwort/Verlauf | getThreadTranscript, syncRecordValues thread / thread_message | LIVE GEPRUEFT |
| Modelle + Aufwand | getAvailableModels, reasoningEffort im Body | LIVE GEPRUEFT |
| Welcome-Chat neu | new_welcome_chat (Backend vorhanden) | LIVE GEPRUEFT |
| Neue Messages live | WebSocket notification versions/<id>:thread_message | LIVE GESEHEN (Probe 2026-10-24) |
| Account, Spaces, User-Settings | noch aufzeichnen (loadUserContent / getSpaces o.ae.) | OFFEN |
| Chat umbenennen/archivieren | saveTransactions (Form noch aufzeichnen) | OFFEN |
| Datei/Bild im Chat | OFFEN | OFFEN |
P0 schliesst die offenen Zeilen: jede Aktion einmal in der echten App klicken, per CDP Network.enable mitschneiden (work/probe_stream2.py als Basis), Methode + Body in docs/plan/redesign/api/*.json ablegen.

## 4. Funktionen der neuen App
1. Sidebar links: Suche, Projekte (aufklappbar), Meine Chats (Mains), darunter Coworker-Tabs mit Status-Punkt, Alter (23 h Rotation), Kontext-Balken. Unten: New chat (= Welcome-Chat), Account-Switcher, MCP-Status.
2. Chat: nur letzte 5 Turns, Load older messages. Modell-Wahl pro Chat, bleibt gespeichert, kein Auto. Aufwand-Wahl. Schritt-Zaehler x/450, Rate-Budget, Stop-Knopf. Tool-Schritte als kompakte Karten (aufklappbar). Composer mit Anhaengen, Slash-Befehlen, Delegieren-Knopf.
3. Projekte = eigener Arbeitsbereich: Projekt-Leiste ganz links (Icons). Projekt oeffnen -> Sidebar zeigt NUR dieses Projekt: oben fest der Projekt-Main-Chat, darunter die Chats des Projekts, darunter seine Coworker. Main-Chat oeffnet sich direkt. Projekt-Startseite: Ziel, Ordner, Aufgaben-Board, Memory, Commits. New chat = Welcome-Chat im Projekt.
3b. Jeder Chat zeigt sein Alter (Sidebar + Kopfzeile, Ring bis 23 h) und hat einen Knopf Switch context (Uebergabe an frischen Chat, wie Main-Switch).
4. Live-Uebersicht: Braucht dich, Laeuft gerade, Team (Coworker-Karten), Runs mit Fortschritt (DAG), Kontext pro Chat, Rotationen, Rate-Limit, Ereignis-Zeitleiste.
5. Rechte Leiste im Chat: Projekt-Kontext, aktive Tasks dieses Chats, Dateien, Memory.
6. Settings: Accounts (per API), Team (Coworker-Anzahl, Rotation), Modelle & Aufwand, MCP-Server (Gateway, Roblox, Blender, Web), Look (Theme, Kompakt), Benachrichtigungen, Kurzbefehle.
7. Ueberall: Ctrl+K Befehlspalette, Not-Aus, Toasts, Desktop-Benachrichtigung bei fertig / braucht dich.

## 5. Architektur
- Backend: bestehendes NotionTool (Python, Port 8790) wird die einzige Stelle fuer Notion-API-Aufrufe. Neue Routen /api/ui/* (chats, chat/<id>/turns?before=, send, stream per SSE, models, projects, accounts).
- UI: eigene Web-UI (notiontool/web/studio/), geladen in eigenem Fenster (Edge WebView2 oder Electron-Shell). Kein CDP-Overlay mehr.
- MCP-Gateway (8792) bleibt eigener Prozess; UI-Neustart beruehrt ihn nie.
- Fallback: Knopf In Notion oeffnen fuer Seiten/Datenbanken.
- Risiko: Notion-ToS (nur privat, eigenes Konto), API kann sich aendern -> api/*.json Vertragstests.

## 6. Phasen (spaeter mit Rollen delegieren)
| Phase | Inhalt | Rolle | Modell |
|---|---|---|---|
| P0 | API-Mitschnitt aller Aktionen, offene Zeilen in Abschnitt 3 | researcher | Opus 5.5 |
| P1 | Backend /api/ui/* + Vertragstests | worker | Sonnet 5.5 |
| P2 | Studio-UI: Sidebar + Chat (5 Turns, Modell-Wahl) | worker | Sonnet 5.5 |
| P3 | Projekte + Live + rechte Leiste | worker | Sonnet 5.5 |
| P4 | Settings, Accounts per API, MCP-Panel | worker | Sonnet 5.5 |
| P5 | Review, alten Overlay-Weg abschalten | quality_planner | Opus 5.5 |
Abhaengig: P1 nach P0; P2-P4 nach P1 (parallel); P5 nach allen.

## 7. Bild-Prompt
Siehe 05-image-prompt.md.
