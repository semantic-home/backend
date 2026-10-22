# Geräte und Automationen für mehrere Haushalte

Umsetzungsstand: 22. Oktober 2026. Die Änderungen befinden sich im lokalen Projekt. Die laufende Installation auf dem Homeserver wurde nicht ersetzt und es wurden keine echten Geräte geschaltet.

Das gemeinsame Modell ist **Zone → Controller → Entitäten mit Rollen und Regeln**. Die ursprüngliche Controller-Oberfläche bleibt der Einstieg. Siehe [Controller und Zonen](CONTROLLERS_AND_ZONES.md) für Beziehungen, Beispiele, Datenmigration und die aktuellen Grenzen der Beleuchtung.

## Was umgesetzt ist

- Konten mit gehashten Passwörtern und widerrufbaren Sitzungen in HttpOnly-Cookies. Schreibzugriffe prüfen den Browser-Ursprung; Anmeldeversuche sind begrenzt.
- Mehrere Haushalte pro Nutzer, mehrere Mitglieder pro Haushalt und die Rollen Eigentümer, Bedienen und Ansehen. Eigentümer können Mitgliedschaften hinzufügen und entfernen. Dazu wird die ausdrücklich geteilte Kontokennung verwendet, die jeder Nutzer unter Konto findet; nicht eine unbestätigte E-Mail-Behauptung.
- Eigene, widerrufbare Zugangsdaten pro lokalem Agenten. Ein neuer Schlüssel kann für dieselbe Verbindung ausgestellt werden; bisherige Schlüssel werden ungültig. Der gemeinsame Beta-Schlüssel ersetzt diese Anmeldung nicht.
- PostgreSQL für Haushalte, Mitgliedschaften, Gerätefunktionen, Regelversionen, Bereitstellungsstatus, Verlauf sowie vorhandene Bewässerungsdefinitionen und deren Pause-/Überspringen-Zustand. SQL-Migrationen laufen beim Start in einer Transaktion.
- Gerätebestand aus Home Assistant mit Raum-, Geräte- und Registry-Zuordnung. Verschiedene Haushalte oder Anbindungen dürfen dieselben externen Entity-IDs haben. Neue Funktionen werden gesucht und seitenweise geladen, statt in einer festen Liste programmiert zu sein.
- Vorlagen für Feuchteregelung, Schaltzeitfenster und Reinigung. Jede Regel kann mehrere ausdrücklich ausgewählte Zielgeräte haben. Funktionsumfang und Parameter werden vor dem Speichern geprüft.
- Eine lesbare Zusammenfassung und eine Vorschau ohne Gerätebefehle. Freier Text wird nicht als ausführbares YAML oder Jinja akzeptiert.
- Entwürfe speichern, aktivieren, pausieren, fortsetzen und entfernen. „Gespeichert“, „Übertragung ausstehend“ und „lokal bestätigt“ sind verschiedene Zustände. Versionsprüfungen verhindern das versehentliche Überschreiben zwischen zwei geöffneten Editoren.
- Der aktualisierte Agent verwaltet ausschließlich app-eigene HA-Automationen mit stabiler ID und passender Eigentumsmarkierung. Er prüft die geladene Automation und deren Aktivierungszustand, bevor er Erfolg meldet.
- Bereits bereitgestellte neue Automationen laufen in Home Assistant, wenn die App, der zentrale Dienst oder der Agent nicht verbunden ist. Eine weiter benötigte Herstellercloud bleibt eine Eigenschaft der jeweiligen Geräteanbindung.
- Ein Verlauf trennt angeforderte Befehle von zurückgemeldeten Gerätezuständen. Eine Bestätigung des Befehls bedeutet nicht automatisch, dass ein physikalisches Ziel erreicht wurde. Während einer getrennten Verbindung kann der Verlauf unvollständig sein.

Eine dargestellte Gerätefunktion entspricht zunächst einer HA-Entity. Mehrere Funktionen eines physischen Geräts behalten dieselbe übernommene Gerätezuordnung. Die App zeigt diese Funktionen mit Geräte- und Raumnamen; es gibt keine feste Obergrenze in einer programmierten Geräteliste.

## Feuchtigkeit zwischen zwei Grenzen regeln

Im Domain-Tab Humidity eine Zone wählen und einen Controller anlegen. Eine oder mehrere schaltbare Entitäten als Entfeuchter-Ausgänge sowie einen Raumluftfeuchtesensor zuordnen. Unter Edit Rules die gewünschte Regel hinzufügen. Bodenfeuchte wird nicht als Raumluftfeuchte akzeptiert.

Mit den Beispielgrenzen 55 % und 52 % gilt: Ab 55 % einschalten, bei höchstens 52 % ausschalten, dazwischen den bisherigen Zustand erhalten. Grenzwerte, Tage, Zeiten, Mindestabstand zwischen Schaltungen und zulässiges Messwertalter sind veränderbar. Der tatsächliche Raumwert kann über die eingestellten Grenzen hinausgehen.

Eine Steckdose erkennt das angeschlossene Haushaltsgerät nicht automatisch. Der Nutzer wählt ihren Zweck und die zugehörige Messung. Der Entfeuchter muss nach Stromwiederkehr selbst weiterlaufen können. Der passende Schaltabstand richtet sich nach dem Gerät; der voreingestellte Wert ist keine gerätespezifische Freigabe.

Bei ungültigen, fehlenden oder veralteten Messungen wird ausgeschaltet. Fehler- und Zeitfenster-Abschaltungen haben Vorrang vor dem normalen Schaltabstand. Lokale Messwertänderungen werden sofort ausgewertet; Zeitfenster, wieder verfügbare Ziele und Schaltabstände werden zusätzlich einmal pro Minute abgeglichen. Dadurch kann sich eine entsprechende Schaltung um bis zu ungefähr eine Minute verzögern.

## Zeitpläne, manuelle Steuerung und Konflikte

Schaltzeitfenster verwenden die Zeitzone des Haushalts, die bei Aktivierung mit HA übereinstimmen muss. Bei Fenstern über Mitternacht gehört der Abschnitt nach Mitternacht zum ausgewählten Starttag. Zeitfenster folgen der lokalen Wanduhr auch bei Sommerzeitwechseln. Reinigung startet nur beim geplanten Zeitereignis; vergangene Starts werden nach einem Neustart nicht nachgeholt. Ein Tageswächter verhindert doppelte Starts am gleichen lokalen Datum.

Beim Aktivieren wird eine konkurrierende app-eigene Regel für dasselbe Ziel zurückgewiesen. Bereits einem Bewässerungscontroller zugeordnete Aktoren sind ebenfalls geschützt. Aktive Regeln werden vor Änderungen oder manueller Bedienung ihrer Ziele pausiert. Externe Automationen in Tuya oder HA werden nicht automatisch erkannt oder entfernt; solche konkurrierenden Regeln müssen bei der Einrichtung gezielt geprüft werden. Ein manueller Eingriff direkt in HA oder am physischen Gerät pausiert die App-Regel nicht automatisch.

Nach einer Geräteumbenennung wird die Registry-Identität erhalten. Eine erkannte Ersetzung erhält eine neue interne Identität; bekannte physische Geräte-IDs werden zusätzlich in den lokalen Regeln geprüft. Der Agent deaktiviert betroffene app-eigene Regeln bei einer erkannten Registry-Ersetzung. Bei einer reinen Entity ohne physische Gerätezuordnung ist diese zusätzliche lokale Geräteprüfung nicht möglich.

Das Widerrufen einer Agentenberechtigung trennt die zentrale Verbindung. Es entfernt keine bereits lokal gespeicherten HA-Automationen. Die Oberfläche weist darauf hin; diese Regeln müssen direkt in HA geprüft oder nach erneuter Verbindung ausdrücklich wieder verwaltet werden.

## Lokale Entwicklung

Voraussetzungen: Python 3.10+, uv, Node/npm und PostgreSQL. In Docker ist PostgreSQL 18 vorgesehen. Für das offizielle Image liegt das Volume ab Version 18 unter `/var/lib/postgresql`; siehe [Docker-PostgreSQL-Dokumentation](https://github.com/docker-library/docs/blob/master/postgres/README.md#pgdata).

Vom gemeinsamen Projektordner aus:

```bash
# POSTGRES_PASSWORD vorher lokal festlegen.
docker compose -f infra/app/compose.dev.yml up -d

cd backend
cp .env.example .env
# DATABASE_URL in .env auf die neue Datenbank setzen.
# Das Passwort in einer Verbindungs-URL gegebenenfalls URL-kodieren.
uv sync --group dev
uv run uvicorn --app-dir src --factory backend.__main__:create_app --host 127.0.0.1 --port 8000
```

In einem zweiten Terminal:

```bash
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Dann unter `http://127.0.0.1:5173` ein Konto und einen Haushalt anlegen. Unter Verbindungen einen lokalen Agenten hinzufügen. Der dort erzeugte Schlüssel wird nur einmal angezeigt. Den aktualisierten Agenten aus dem lokalen `agent`-Checkout ausführen; dessen README beschreibt die Umgebungsvariablen. HA_TOKEN bleibt auf dem Agenten und wird nicht an das Backend übertragen.

Die HA-Konfigurationsschnittstelle verlangt Administratorrechte. Die verwendeten Endpunkte und Rückmeldungen wurden anhand des [HA-Quellcodes 2026.9.4](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/config/automation.py) und der [zugehörigen Konfigurationsansicht](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/config/view.py) geprüft. Es ist kein allgemeines öffentlich garantiertes Versionsprotokoll. Ohne passende HA-Konfiguration bzw. Berechtigung bleibt die Übertragung sichtbar fehlgeschlagen; eine echte Hardware-Abnahme steht aus.

## Bestehende Installation übernehmen

Backend, Frontend und Agent müssen gemeinsam aktualisiert werden. Die bisherigen Agenten mit ausschließlich gemeinsamem Beta-Schlüssel können sich am neuen Backend nicht anmelden.

1. Vor dem Ersetzen des alten Backends dessen Bewässerungscontroller und Regeln exportieren. Die alte Version hält diese Daten im RAM; ein Neustart vor dem Export kann sie verlieren lassen.
2. PostgreSQL und die neue Anwendung zunächst getrennt vorbereiten. Ein Eigentümerkonto, den beabsichtigten Haushalt und eine neue Agentenverbindung anlegen. Den aktualisierten Agenten verbinden und die Gerätezuordnung prüfen.
3. Das neue Backend für den Import anhalten. Der Import verlangt die ausdrücklich gewählte Haushalts- und Agenten-ID, prüft die vorhandenen Geräte und überschreibt keine bestehenden Datensätze. Eine fehlgeschlagene Teilübernahme wird zurückgerollt.
4. Backend wieder starten. Übernommene Regeln beginnen pausiert. Zeitplanung, Geräte und Vorschau prüfen, bevor eine Regel wieder aktiviert wird.

Das Hilfsprogramm wird aus dem aktualisierten Backend-Checkout gestartet:

```bash
uv run python -m backend.platform.migrate_legacy export \
  --url http://OLD-BACKEND:8000/api/v1 --output watering-export.json

# Optional beim Export: --session TAB-SESSION-ID für einen bestimmten Demo-/Tab-Bestand.
# Erst nach dem Anhalten des neuen Backends importieren:
uv run python -m backend.platform.migrate_legacy import \
  --input watering-export.json --home-id HOUSEHOLD-ID --agent-id AGENT-ID
```

Der Export liest nur die angegebene alte API und legt eine neue Datei mit restriktiven Dateirechten an. Die bisherige Bewässerung nutzt weiterhin ihren eigenen zentralen Scheduler; sie wird durch dieses Upgrade nicht stillschweigend in lokale HA-Regeln umgewandelt. Aktive Bewässerungsläufe vor dem Backend-Wechsel geordnet beenden.

## Gemeinsamer Betrieb

`infra/app/compose.yml` ergänzt PostgreSQL mit dauerhaftem Volume, interner Netzwerkverbindung und Healthcheck. Es veröffentlicht keinen Datenbankport. In der produktiven Umgebung DATABASE_URL, POSTGRES_PASSWORD, REGISTRATION_KEY, ALLOWED_ORIGINS und bei HTTPS AUTH_COOKIE_SECURE=true setzen. Beispiele enthalten keine produktiven Geheimnisse. Erst neu gebaute bzw. veröffentlichte Backend-/Frontend-Images verwenden.

Diese Ausbaustufe verwendet **einen Backend-Prozess pro Datenbank**. Der bestehende Bewässerungsscheduler und die Agentenverbindungen liegen teilweise im Prozess. Eine Datenbanksperre verhindert konkurrierende Instanzen. Mehrere Backend-Replikate benötigen eine zusätzliche Verteilung der Agentenverbindungen und Scheduler-Zuständigkeit; allein PostgreSQL macht daraus noch keinen verteilten Laufzeitdienst.

Für Sicherungen beispielsweise innerhalb des Compose-Projekts `docker compose exec -T postgres pg_dump -U semantic_home -d semantic_home -Fc > semantic-home.dump` verwenden und die Wiederherstellung regelmäßig in einer getrennten Datenbank prüfen. Ein Datenbank-Volume allein ist keine Sicherung. Schemaänderungen werden mit nummerierten Migrationen ergänzt; bereits angewandte Migrationen nicht nachträglich umschreiben.

## Prüfung und praktische Grenzen

Die Tests prüfen die bisherigen Bewässerungsfunktionen sowie Anmeldung, Haushaltstrennung, Rechte, Agentenbestätigungen, Regeln, Neustarts, Umbenennungen und Migration. Die Integrationstests benötigen TEST_DATABASE_URL und dürfen ausschließlich eine getrennte Datenbank verwenden, deren Name mit `semantic_home_test` beginnt; sie leeren diese Testdatenbank.

```bash
TEST_DATABASE_URL=postgresql://USER:PASSWORD@localhost:55432/semantic_home_test \
  uv run --group dev python -m unittest discover -s tests -v
```

Die Backend-CI ist für PostgreSQL 18 vorbereitet. Lokal wurde mit der vorhandenen isolierten PostgreSQL-14-Installation geprüft. Eine Kapazitätsprobe umfasst 5.000 Gerätefunktionen mit seitenweisem Zugriff und eine Regel mit 500 Zielen. Dies prüft die variable Datenmenge und ist keine Zusage für unbegrenzte gleichzeitige Nutzer oder Schaltlast. Die Agententests simulieren HA-Antworten und erfordern keine echten Geräte.
