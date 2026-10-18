# Konzept: E-Mail-Benachrichtigungen und Ergebnisversand

Status: Stufe 1 (E-Mail-Kanal für Benachrichtigungen, Abschnitte 1, 2, 5–7) und Stufe 2 (Ergebnisversand durch
Agenten, Abschnitt 3) sind umgesetzt; Betrieb in [`operations.md`](operations.md#email-notifications) bzw.
[`operations.md`](operations.md#sending-work-results-by-email). Abweichungen der Stufe 2: Anhänge werden nicht in der
Datenbank gespeichert, sondern beim Versand erneut aus Gitea gelesen und gegen den SHA-256 des Auftrags geprüft;
interne Empfänger brauchen eine bestätigte Adresse; das Tageslimit zählt die Aufträge der letzten 24 Stunden.

> Hinweis zur Sprache: Das Dokument folgt der Sprache des Auftrags (Deutsch). Nach `brain.md` sind `docs/` sonst
> englisch; vor einer Veröffentlichung im Public-Export muss es übersetzt oder in `research/` verschoben werden.

Stand 2026-10-16, abgestimmt mit dem Owner. Quelle: Claude-Docs-Dokument „TaskBrigade: E-Mail-Benachrichtigungen und
Ergebnisversand“.

**Entscheidung:** TaskBrigade verschickt nur, es empfängt keine Mails. E-Mail wird ein zweiter Kanal im bestehenden
Benachrichtigungs-Ausgang, versendet per SMTP über ein Postfach bei einem externen Provider. Agenten verschicken
Arbeitsergebnisse nie selbst, sondern beauftragen den Server; der prüft Empfänger, Datenklasse und Freigabe und
versendet. Der Server bleibt aus dem Internet unerreichbar, und es gibt keinen Eingangsweg für Befehle oder Daten.

## Überblick

```
Ereignis (Review, Rückfrage …) → Ausgang (Telegram oder E-Mail) → Zustell-Job (Retry, Drossel) → SMTP beim Provider
Agent (send_email, MCP/CLI) → Server-Prüfung (Empfänger, Datenzone) → Freigabe (Mensch) → Zustell-Job
```

Nichts fließt zurück in TaskBrigade.

## 1. Externer Mail-Provider

Ein eigenes Postfach für TaskBrigade auf der eigenen Domain, z. B. `taskbrigade@<domain>`, bei einem Provider mit Sitz
und Rechenzentrum in der EU. Kein privates Postfach mitbenutzen.

| Anforderung | Warum |
| --- | --- |
| SMTP-Submission (Port 587 STARTTLS oder 465 TLS) mit Benutzer/App-Passwort | Versand ohne eigenen Mailserver |
| Eigene Domain mit DKIM-Signatur durch den Provider, SPF und DMARC setzbar | Mails landen nicht im Spam, Fälschungen sind erkennbar |
| Weiterleitung des Postfachs an einen Menschen (oder Abwesenheitsantwort „wird nicht gelesen“) | Unzustellbar-Meldungen und versehentliche Antworten gehen nicht verloren |
| Vertrag zur Auftragsverarbeitung (Art. 28 DSGVO) | Mails enthalten Namen und Arbeitsinhalte |
| Ratenlimit bekannt (Mails/Stunde, Empfänger/Tag) | TaskBrigade drosselt darunter |

TaskBrigade bleibt providerneutral: nur Standard-SMTP (kein IMAP), Host und Zugang in den Einstellungen, nichts davon
im Repo. Provider-Wahl ist noch offen (Owner).

## 2. Benachrichtigungen per E-Mail

E-Mail wird ein zweiter Kanal im vorhandenen Ausgang (`app/services/notifications.py`, Tabelle `notifications`).
Ereignisse, Empfänger, Stummschalten und Ruhezeiten bleiben; neu ist nur der Zustellweg.

- **Ereignisse:** dieselben wie bei Telegram (`EVENTS`).
- **Kanal je Person und Ereignis:** Telegram, E-Mail, beides oder nichts. Vorgabe: E-Mail nur für Review und Sprint
  fertig; Rückfragen und Blockaden weiter über Telegram.
- **Adresse:** `actors.email` gibt es schon; neu ist eine Bestätigung per Link (`email_verified_at`). Ohne bestätigte
  Adresse keine Mails.
- **Sammelmail:** je Person „sofort“ oder „Bündel alle 15/60 Minuten“ (vorhandene Bündelung `_chunks` mit
  Zeitfenster); Ruhezeiten halten Mails zurück wie Telegram-Nachrichten.
- **Inhalt:** Betreff `[TaskBrigade] DEMO-0092 · Task ready for review`, Text wie die Telegram-Nachricht (Projekt,
  Titel, Agent, gekürzter Inhalt, Link). Text + einfaches HTML, Server-Standardsprache, kein Tracking-Pixel.
  Fußzeile: Antworten auf diese Mail werden nicht verarbeitet – bitte über den Link in TaskBrigade antworten.
- **Zustellung:** Job `email_deliver` im Scheduler mit derselben Logik wie `deliver`: Retry mit Backoff, höchstens
  8 Versuche, Drossel unter dem Provider-Limit, Text nach Versand oder Aufgabe geleert (Protokoll ohne Inhalt,
  30 Tage). Nur ein Prozess versendet (Sperrdatei wie beim Telegram-Bot). Erkannt werden nur Fehler, die der
  SMTP-Server sofort meldet.
- **Kopfzeilen:** `Message-ID` mit eigenem Hostnamen-Teil, `Auto-Submitted: auto-generated`, `List-Unsubscribe`
  (Link zu den Einstellungen).

Technik ohne neue Abhängigkeit: `smtplib` und `email` aus der Standardbibliothek; Tests mit einem Fake-SMTP-Server
(wie `FakeApi` bei Telegram).

## 3. Arbeitsergebnisse per E-Mail

Ein Agent stellt einen Versandauftrag, der Server versendet. Anders als bei SFTP bekommen Agenten keine Zugangsdaten.

**Ablauf**

1. Agent ruft `send_email` (MCP) oder `tbr mail send` (CLI) auf: Task-Nummer, Empfänger, Betreff, Text (Markdown),
   Anhänge als Verweise.
2. Server prüft: Empfänger auf der Liste des Projekts, Datenklasse erlaubt Versand, Größe und Dateitypen,
   Tageslimit des Projekts.
3. Zeile in `outgoing_mails` mit Status `pending_approval` (Vorgabe) oder `queued` (Empfänger mit „ohne Freigabe“).
4. Freigabe durch einen Menschen in der Task-Ansicht oder Inbox: Vorschau mit Text, Empfängern und Anhang-Hashes;
   Freigeben, Ändern oder Ablehnen. Alternativ „Versand bei Abnahme“: der Auftrag geht raus, wenn der Task
   approved/gemergt wird.
5. Versand durch denselben Job wie die Benachrichtigungen; Ergebnis (gesendet, Message-ID, SMTP-Fehler) als
   Systemkommentar am Task und im Audit-Log.

**Anhänge** nur aus Quellen, die der Server selbst lesen kann: Task-Anhänge (`attachments`), Dateien im Task-Branch
bzw. Merge-Commit (`branch:<pfad>`, `merge:<pfad>`, aus Gitea), später freigegebene `run_results` des Output-Gates.

Grenzen (Einstellungen, Vorgaben): 10 MB je Mail; pdf, docx, xlsx, pptx, csv, md, png, jpg (zip nur nach Freigabe);
höchstens 20 Mails je Projekt und Tag.

**Empfängerliste je Projekt** (`project_mail_recipients`): Adresse, Bezeichnung, Modus `freigabe` /
`ohne_freigabe`. Adressen außerhalb der Liste lehnt der Server ab. Interne Empfänger (Menschen mit Konto) dürfen
standardmäßig ohne Freigabe beliefert werden.

**Workflows:** Der Schritttyp `notify` bekommt den Kanal E-Mail; Versand von Ergebnissen am Ende einer Kette ist ein
`notify`-Schritt mit Anhängen aus den Ausgaben des Vorgängers (`{{steps.<name>.output}}`), gleiche Regeln.

## 4. Keine eingehenden Mails

TaskBrigade liest das Postfach nicht: kein IMAP, keine Antworten per Mail, keine Befehle oder Freigaben per Mail,
keine Tasks aus Mails.

- Benachrichtigungen: Fußzeile sagt, dass Antworten nicht verarbeitet werden.
- Ergebnisversand: `Reply-To` ist der Mensch, der den Versand freigegeben hat (bzw. der Projekt-Owner).
- Unzustellbar-Meldungen: Das Postfach beim Provider leitet an einen Menschen (Admin) weiter; TaskBrigade kennt nur
  die sofortigen SMTP-Fehler und zeigt sie am Versandauftrag.

## 5. Datenschutz und Sicherheit

E-Mail an Externe ist ein Zonenwechsel im Sinne von `research/konzept-datenzonen.md`.

| Datenklasse (Projekt/Task) | Benachrichtigung | Ergebnisversand |
| --- | --- | --- |
| `cloud_ok` | voller Text wie Telegram | erlaubt, Vorgabe mit Freigabe |
| `local_only` | nur Task-Nummer, Ereignis, Link | nur vom Output-Gate freigegebene Aggregate; immer mit Freigabe |
| `sealed` | nur Task-Nummer, Ereignis, Link | gesperrt |

- Agent schickt Daten an fremde Adresse → nur Projekt-Liste, Vorgabe Freigabe, Tageslimit, Audit mit Empfängern und
  Anhang-Hashes.
- Prompt-Injection führt zu Versand → Freigabe ist Vorgabe; „ohne Freigabe“ nur für ausdrücklich eingetragene
  Empfänger.
- Zugangsdaten → SMTP-Passwort als Secret in `app_settings` (Fernet), nie an Agenten, nie im Log; App-Passwort.
- Header-Injection → nur über die `email`-Bibliothek, Zeilenumbrüche im Betreff entfernen, Adressen mit
  `email.utils` prüfen.
- Absender-Reputation → Drossel unter dem Provider-Limit, `List-Unsubscribe`, kein Massenversand.

Audit: `mail_requested`, `mail_approved`, `mail_rejected`, `mail_sent`, `mail_failed` mit Task, Empfängern und
Anhang-Hashes – ohne Mailtext.

## 6. Datenmodell, Einstellungen, Schnittstellen

| Tabelle / Spalte | Inhalt |
| --- | --- |
| `notifications.channel` (neu) | `telegram` / `email`; eine Zeile je Empfänger und Kanal |
| `notification_prefs.channels_json` (neu) | Kanal je Ereignis, Bündelintervall |
| `actors.email_verified_at` (neu) | Bestätigung von `actors.email` |
| `email_verify_codes` (neu) | einmalige Bestätigungslinks, 24 h, nur Hash |
| `outgoing_mails` (neu) | Task, Auftraggeber, An/Cc, Reply-To, Betreff, Text, Anhang-Verweise + SHA-256, Status (`pending_approval`, `queued`, `sent`, `failed`, `rejected`), Freigabe durch/wann, Message-ID, SMTP-Fehler; Text/Anhänge nach 30 Tagen geleert |
| `project_mail_recipients` (neu) | Projekt, Adresse, Bezeichnung, Modus `freigabe` / `ohne_freigabe` |

Einstellungen (Admin → Einstellungen → E-Mail, `app_settings`, Passwort als Secret): `email_enabled`, `smtp_host`,
`smtp_port`, `smtp_security` (`starttls`/`tls`), `smtp_user`, `smtp_password`, `mail_from_address`,
`mail_from_name`, `mail_rate_per_hour`, `mail_max_mb`, `mail_daily_limit_per_project`. Keine `.env`-Schlüssel.

| Werkzeug | Parameter | Zweck |
| --- | --- | --- |
| `send_email` / `tbr mail send` | `task_nr`, `to`, `cc`?, `subject`, `body` (Markdown), `attachments` (`attachment:<id>`, `branch:<pfad>`, `merge:<pfad>`), `on_approve`? | Versandauftrag; Antwort nennt Status und ob Freigabe nötig ist |
| `list_emails` / `tbr mail list` | `task_nr` | eigene Aufträge und Status |
| `list_mail_recipients` | `project` | erlaubte Empfänger des Projekts |

TASK.md bekommt einen Abschnitt „Sending results by email“, wenn das Projekt Empfänger eingetragen hat.

## 7. Oberfläche

| Ort | Was dort passiert |
| --- | --- |
| Admin → Einstellungen → E-Mail | SMTP-Zugang, Absender, Limits; „Testmail senden“ mit Klartext-Fehler |
| Profil → Benachrichtigungen | Adresse bestätigen; Ereignis × Kanal, Bündelintervall; Ruhezeiten für beide Kanäle |
| Projekt → Einstellungen → E-Mail-Empfänger | erlaubte Empfänger mit Modus; Tageslimit |
| Task-Detail, Abschnitt „E-Mails“ | Versandaufträge mit Status und SMTP-Fehlern; bei `pending_approval` Vorschau und Freigeben / Bearbeiten / Ablehnen |
| Inbox | Gruppe „E-Mail-Freigaben“ |
| Admin → Benachrichtigungsprotokoll | Spalte Kanal |

Die Freigabe eines Ergebnisversands ist ein eigener Klick, getrennt vom Review-Approve – außer bei „Versand bei
Abnahme“; dann zeigt die Review-Ansicht den Versandauftrag mit an, und Approve löst beides aus.

## 8. Ausbaustufen und Folge-Tasks

| # | Stufe | Task | hängt ab von |
| --- | --- | --- | --- |
| 1 | 1 | E-Mail-Kanal für Benachrichtigungen | – |
| 2 | 2 | Ergebnisversand durch Agenten | 1 |
| 3 | 3 | Integration Datenzonen/Workflows (`notify`-Schritt, `run_results`) | 2, DZ 8, WF 2 |
| 4 | – | Betrieb: Doku `docs/operations.md`, `tbr doctor`-Prüfung | 1 |

Keine neuen Dienste und keine neue Abhängigkeit: Versand im vorhandenen Scheduler-Prozess (`smtplib`, `email`),
Docker-Variante gleich.
