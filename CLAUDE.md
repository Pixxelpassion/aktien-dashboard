# Pixxelpassion Portfolio (Parqet-Dashboard)

Flask + SQLite Dashboard. Holdings kommen von Parqet (MCP + OAuth), Kurse/Historie von Yahoo
Finance (yfinance), Discord-Alarme bei Kauf-/Verkaufsmarken. Live: https://portfolio.pixxelpassion.de

## Dateien
- `server.py` — Flask: API, OAuth-Flow, HTTP Basic Auth, DB-Schema (`init_db`)
- `sync.py` — gesamte Sync-Logik (`run_sync`), Parqet-MCP-Client, Yahoo, Discord
- `dashboard.html` — Single-File-Frontend (Chart.js, Logo als Base64 eingebettet)
- `data/` — Docker-Volume auf dem Server: `portfolio.db`, `config.json` (Tokens)

## Lokal starten
```
pip install -r requirements.txt
python server.py        # http://localhost:5000  (Preview: .claude/launch.json "parqet-dashboard")
```
Ohne `DASHBOARD_USER`/`DASHBOARD_PASSWORD_HASH` ist kein Login aktiv (nur lokal gewollt).

## Parqet-Login / Tokens
- Parqet-OAuth erlaubt nur `localhost` als Redirect. Login daher immer lokal ("Parqet verbinden").
- `refresh_token` rotiert bei jeder Nutzung. **Server-`config.json` nie auf einen Entwicklungsrechner
  kopieren und dort benutzen** — sonst wird der Server-Token ungueltig.
- `data/config.json` enthaelt Secrets und gehoert nicht ins Git.

## Deploy
- Push aus **diesem** Repo auf `main` (Remote `github.com/Pixxelpassion/aktien-dashboard`).
  Der aeussere "Projekt 1"-Ordner ist ein anderes Repo und nicht zum Deployen geeignet.
- Server: `/opt/dashboard`, Container `portfolio-dashboard` (gunicorn -w 4), Hostinger-VPS,
  **kein SSH** — Terminal ueber Hostinger hPanel im Browser. Docker-Befehle auf dem Host ausfuehren,
  nicht im Container.
  ```
  cd /opt/dashboard && git pull origin main && docker compose up -d --build
  ```
- Host-Cron (laut Einrichtung, nicht neu verifiziert): `*/5` `deploy.sh` (pull + rebuild nur bei neuem
  Commit), `*/30` `/api/keepalive`, `0 20` Voll-Sync via `docker exec ... sync.run_sync()`.
  Unter gunicorn laeuft der `__main__`-Scheduler nie — Automatik ist ausschliesslich Cron.
- Zeitzone des Servers: Europe/Berlin.

## Nur auf dem Server, NICHT im Repo
`Dockerfile`, `docker-compose.yml`, `deploy.sh` liegen untracked in `/opt/dashboard`.
**Keine Dateien mit diesen Namen ins Repo legen** — `git pull` auf dem Server bricht sonst ab.
Referenz (Stand Sept. 2026):
- Dockerfile: `python:3.12-slim`, `apt install git`, `COPY . .`,
  `pip install -r requirements.txt gunicorn`, `CMD gunicorn -w 4 -b 0.0.0.0:8000 server:app`
- docker-compose.yml: Service `portfolio`, `container_name: portfolio-dashboard`, Port `127.0.0.1:8000:8000`,
  Volume `./data:/app/data`, Netz `n8n_default` (extern), Traefik-Labels fuer
  `portfolio.pixxelpassion.de` inkl. **`tls.certresolver=mytlschallenge`** (Name aus der n8n-Traefik-Config;
  ohne diese Zeile liefert Traefik ein selbstsigniertes Zertifikat und der Browser-Sync schlaegt fehl).
  `environment`: `FLASK_ENV=production`, `DASHBOARD_USER`, `DASHBOARD_PASSWORD_HASH`
  (werkzeug-Hash, gleiches Muster wie im Affiliate-Dashboard unter `/opt/affiliate-dashboard`).

## Git-Hygiene
Es gibt keine `.gitignore`. Getrackt sind u. a. `data/portfolio.db`, `__pycache__/*.pyc` und die alte
Root-`config.json` (Tokens in der Historie). Deshalb: **nie `git add -A` / `git add .`**, Dateien einzeln
adden. `data/portfolio.db` nicht ohne Plan untracken — `git pull` auf dem Server wuerde die Live-DB
loeschen bzw. mit Konflikt abbrechen.
Windows/Claude-Eigenheit: `git push` meldet "repository moved" auf stderr. PowerShell zeigt dann einen
NativeCommandError, der Push ist trotzdem durchgegangen (Zeile `abc..def main -> main` pruefen);
das Bash-Tool liefert dabei faelschlich Exit 128. Zum Pushen PowerShell nutzen.

## Fachliche Stolperfallen (alle schon einmal passiert)
- Signal-Logik ist Stop-Loss/Breakout: Kauf wenn Kurs `>=` Kaufmarke, Verkauf wenn Kurs `<=` Verkaufmarke.
- Holdings-UPSERT in `run_sync` muss `quantity` und `purchase_price` mit aktualisieren (fehlte frueher,
  Nachkaeufe wurden ignoriert und die Gesamtrendite war falsch).
- Verkaufte Positionen sendet Parqet gar nicht mehr; `run_sync` loescht alles, was nicht mehr geliefert wird.
- `exchange_rates.rate` = Einheiten Fremdwaehrung je 1 EUR (EURUSD=X). Nativ = EUR * rate, EUR = nativ / rate.
- Historische Waehrung kommt von Yahoo (`fast_info.currency`), nicht aus dem ISIN-Praefix. Londoner Aktien
  melden "GBp" (Pence) und werden durch 100 geteilt. Der juengste Monat wird gegen `current_value`
  plausibilisiert (Faktor 2), weil Yahoo-Monatskerzen nach Splits kurzzeitig falsch sein koennen.
- Der Chart "Historische Entwicklung" ist eine Rueckrechnung (heutige Stueckzahl x historischer Kurs,
  aktueller Wechselkurs), kein echter Kontoverlauf. Das ungenutzte Parqet-Tool `parqet_get_performance`
  waere der Weg zur echten Historie.
- Portfolio-Namen enthalten Emojis, teils mit Zero-Width-Space (z. B. "📈​ Trading").

## Offen / ungeprueft
- Ob `DASHBOARD_USER`/`DASHBOARD_PASSWORD_HASH` auf dem Server inzwischen gesetzt sind (Code ist deployed;
  ohne die Env-Vars ist die Seite oeffentlich). Pruefen: `grep -A2 DASHBOARD_USER /opt/dashboard/docker-compose.yml`.
- Parqet liefert `currentPrice` in Portfolio-Waehrung (Portfolio "Project X" ist USD), der Alarm-Code
  nimmt EUR an. Nicht verifiziert, ob das zu falschen Alarmen fuehrt.
