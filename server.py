"""
Parqet Portfolio Dashboard — Flask Server
Startet via: python server.py
Öffnet automatisch http://localhost:5000
"""
from __future__ import annotations
import json, os, sqlite3, threading, time, hashlib, base64, secrets, webbrowser
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, redirect
import requests as http
import schedule

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "portfolio.db"
# config.json liegt jetzt im data/-Ordner, damit es im Docker-Volume persistiert
# und bei Updates (Rebuild) nicht verloren geht.
CONFIG_PATH = DATA_DIR / "config.json"
OLD_CONFIG_PATH = BASE_DIR / "config.json"  # alter Ort — nur zum Migrieren

CLIENT_ID = "019c28d5-e0a0-703f-a790-10c15c2310ee"
AUTH_ENDPOINT = "https://connect.parqet.com/oauth2/authorize"
TOKEN_ENDPOINT = "https://connect.parqet.com/oauth2/token"
# Die Rücksprungadresse für den OAuth-Login. Parqets "Claude"-Integration
# erlaubt nur localhost — der Login passiert daher immer lokal. Der Server
# braucht keinen Login (er nutzt den refresh_token), kann den Wert aber per
# Umgebungsvariable überschreiben.
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:5000/oauth/callback")

app = Flask(__name__, static_folder=str(BASE_DIR))
app.secret_key = secrets.token_hex(32)

_pkce_store: dict[str, str] = {}  # state -> verifier


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # Migration: alte config.json (im Hauptordner) einmalig übernehmen
    if OLD_CONFIG_PATH.exists():
        cfg = json.loads(OLD_CONFIG_PATH.read_text(encoding="utf-8"))
        save_config(cfg)
        return cfg
    return {
        "discord_webhook_url": "",
        "parqet_access_token": "",
        "parqet_refresh_token": "",
        "parqet_token_expires_at": 0,
        "server_port": 5000,
        "sync_hour": 20,
        "sync_minute": 0,
    }


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS holdings (
            ticker          TEXT PRIMARY KEY,
            name            TEXT,
            isin            TEXT,
            quantity        REAL DEFAULT 0,
            purchase_price  REAL DEFAULT 0,
            current_price   REAL DEFAULT 0,
            current_value   REAL DEFAULT 0,
            total_return_pct REAL DEFAULT 0,
            weight          REAL DEFAULT 0,
            portfolio_name  TEXT DEFAULT '',
            synced_at       TEXT DEFAULT (datetime('now'))
        );
        """)

        # Add portfolio_name column if it doesn't exist
        try:
            db.execute("ALTER TABLE holdings ADD COLUMN portfolio_name TEXT DEFAULT ''")
        except:
            pass

        # Add currency_override column if it doesn't exist
        try:
            db.execute("ALTER TABLE annotations ADD COLUMN currency_override TEXT DEFAULT ''")
        except:
            pass

        # Add position_size column if it doesn't exist
        try:
            db.execute("ALTER TABLE annotations ADD COLUMN position_size TEXT DEFAULT ''")
        except:
            pass

        # Add typical_drawdown column if it doesn't exist
        try:
            db.execute("ALTER TABLE annotations ADD COLUMN typical_drawdown REAL")
        except:
            pass

        # Add report_url column if it doesn't exist
        try:
            db.execute("ALTER TABLE annotations ADD COLUMN report_url TEXT DEFAULT ''")
        except:
            pass

        db.executescript("""
        CREATE TABLE IF NOT EXISTS annotations (
            ticker            TEXT PRIMARY KEY,
            stock_type        TEXT DEFAULT '',
            sector            TEXT DEFAULT '',
            country           TEXT DEFAULT '',
            buy_target        REAL,
            sell_target       REAL,
            notes             TEXT DEFAULT '',
            currency_override TEXT DEFAULT '',
            position_size     TEXT DEFAULT '',
            typical_drawdown  REAL,
            report_url        TEXT DEFAULT '',
            updated_at        TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS metrics (
            ticker               TEXT PRIMARY KEY,
            avg_drawdown_pct     REAL,
            max_drawdown_pct     REAL,
            current_drawdown_pct REAL,
            return_15y_pct       REAL,
            return_15y_cagr      REAL,
            computed_at          TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS alarm_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker       TEXT,
            alarm_type   TEXT,
            price        REAL,
            triggered_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS exchange_rates (
            currency     TEXT PRIMARY KEY,
            rate         REAL,
            updated_at   TEXT DEFAULT (datetime('now'))
        );
        """)


init_db()


# ---------------------------------------------------------------------------
# OAuth PKCE
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


@app.route("/oauth/start")
def oauth_start():
    state = secrets.token_urlsafe(16)
    verifier, challenge = _pkce_pair()
    _pkce_store[state] = verifier

    import urllib.parse
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "portfolio:read",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)
    return redirect(url)


@app.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    error = request.args.get("error", "")

    if error:
        return f"<h1>Autorisierungsfehler</h1><p>{error}</p><a href='/'>Zurück</a>", 400

    verifier = _pkce_store.pop(state, None)
    if not verifier:
        return "<h1>Ungültiger State</h1><a href='/'>Zurück</a>", 400

    resp = http.post(TOKEN_ENDPOINT, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    }, timeout=15)

    if resp.status_code != 200:
        return f"<h1>Token-Fehler</h1><pre>{resp.status_code}: {resp.text}</pre><a href='/'>Zurück</a>", 400

    data = resp.json()
    cfg = load_config()
    cfg["parqet_access_token"] = data.get("access_token", "")
    cfg["parqet_refresh_token"] = data.get("refresh_token", "")
    cfg["parqet_token_expires_at"] = int(time.time()) + data.get("expires_in", 3600)
    cfg["parqet_last_refresh_ok"] = True
    save_config(cfg)

    return redirect("/?connected=1")


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "dashboard.html")


@app.route("/api/status")
def api_status():
    cfg = load_config()
    access = cfg.get("parqet_access_token", "")
    refresh = cfg.get("parqet_refresh_token", "")
    # "Verbunden" hängt am Dauer-Schlüssel (refresh_token), nicht am kurzlebigen
    # Access-Token (läuft stündlich ab und wird beim Sync automatisch erneuert).
    # Rot wird es nur, wenn die letzte automatische Erneuerung wirklich fehlschlug.
    connected = bool(access or refresh)
    token_valid = bool(refresh) and cfg.get("parqet_last_refresh_ok", True) is not False

    with get_db() as db:
        last_sync = db.execute("SELECT MAX(synced_at) AS t FROM holdings").fetchone()["t"]
        count = db.execute("SELECT COUNT(*) AS c FROM holdings").fetchone()["c"]
        alarms_today = db.execute(
            "SELECT COUNT(*) AS c FROM alarm_log WHERE date(triggered_at) = date('now')"
        ).fetchone()["c"]

    return jsonify({
        "connected": connected,
        "token_valid": token_valid,
        "last_sync": last_sync,
        "holdings_count": count,
        "alarms_today": alarms_today,
    })


@app.route("/api/portfolio")
def api_portfolio():
    with get_db() as db:
        rows = db.execute("""
            SELECT
                h.ticker, h.name, h.isin, h.quantity, h.purchase_price,
                h.current_price, h.current_value, h.total_return_pct, h.weight, h.synced_at,
                h.portfolio_name,
                a.stock_type, a.sector, a.country, a.buy_target, a.sell_target, a.notes, a.currency_override, a.position_size, a.typical_drawdown, a.report_url,
                m.avg_drawdown_pct, m.max_drawdown_pct, m.current_drawdown_pct,
                m.return_15y_pct, m.return_15y_cagr
            FROM holdings h
            LEFT JOIN annotations a ON h.ticker = a.ticker
            LEFT JOIN metrics     m ON h.ticker = m.ticker
            ORDER BY h.portfolio_name, h.current_value DESC
        """).fetchall()
        holdings = [dict(r) for r in rows]

    total_value = sum(h["current_value"] or 0 for h in holdings)
    total_cost  = sum((h["quantity"] or 0) * (h["purchase_price"] or 0) for h in holdings)
    total_ret   = ((total_value / total_cost) - 1) * 100 if total_cost > 0 else 0.0

    return jsonify({
        "holdings": holdings,
        "summary": {
            "total_value":      round(total_value, 2),
            "total_cost":       round(total_cost, 2),
            "total_return_pct": round(total_ret, 2),
            "count":            len(holdings),
        },
    })


@app.route("/api/annotations/<ticker>", methods=["GET", "POST"])
def api_annotations(ticker: str):
    ticker = ticker.upper()
    if request.method == "GET":
        with get_db() as db:
            row = db.execute("SELECT * FROM annotations WHERE ticker = ?", (ticker,)).fetchone()
            return jsonify(dict(row) if row else {})

    data = request.get_json(silent=True) or {}
    with get_db() as db:
        db.execute("""
            INSERT INTO annotations
                (ticker, stock_type, sector, country, buy_target, sell_target, notes, currency_override, position_size, typical_drawdown, report_url, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(ticker) DO UPDATE SET
                stock_type         = excluded.stock_type,
                sector             = excluded.sector,
                country            = excluded.country,
                buy_target         = excluded.buy_target,
                sell_target        = excluded.sell_target,
                notes              = excluded.notes,
                currency_override  = excluded.currency_override,
                position_size      = excluded.position_size,
                typical_drawdown   = excluded.typical_drawdown,
                report_url         = excluded.report_url,
                updated_at         = excluded.updated_at
        """, (
            ticker,
            data.get("stock_type", ""),
            data.get("sector", ""),
            data.get("country", ""),
            data.get("buy_target") or None,
            data.get("sell_target") or None,
            data.get("notes", ""),
            data.get("currency_override", ""),
            data.get("position_size", ""),
            data.get("typical_drawdown") or None,
            data.get("report_url", ""),
        ))
    return jsonify({"ok": True})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    def _bg():
        from sync import run_sync
        run_sync()
    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync läuft im Hintergrund..."})


@app.route("/api/alarms")
def api_alarms():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM alarm_log ORDER BY triggered_at DESC LIMIT 100"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/exchange-rates")
def api_exchange_rates():
    with get_db() as db:
        rows = db.execute("SELECT currency, rate FROM exchange_rates").fetchall()
    return jsonify({row["currency"]: row["rate"] for row in rows})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        cfg = load_config()
        return jsonify({k: v for k, v in cfg.items() if "token" not in k})

    data = request.get_json(silent=True) or {}
    cfg = load_config()
    if "discord_webhook_url" in data:
        cfg["discord_webhook_url"] = data["discord_webhook_url"]
    if "sync_hour" in data:
        cfg["sync_hour"] = int(data["sync_hour"])
    if "sync_minute" in data:
        cfg["sync_minute"] = int(data["sync_minute"])
    save_config(cfg)
    _setup_scheduler(cfg)
    return jsonify({"ok": True})


@app.route("/api/discord/test", methods=["POST"])
def api_discord_test():
    cfg = load_config()
    url = cfg.get("discord_webhook_url", "")
    if not url:
        return jsonify({"ok": False, "error": "Kein Webhook konfiguriert"}), 400
    try:
        r = http.post(url, json={"content": "✅ Parqet Dashboard — Webhook-Test erfolgreich!"}, timeout=10)
        return jsonify({"ok": r.status_code in (200, 204)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/parqet/test", methods=["POST"])
def api_parqet_test():
    cfg = load_config()
    token = cfg.get("parqet_access_token", "")
    if not token:
        return jsonify({"ok": False, "error": "Kein Access Token — bitte Parqet verbinden"}), 400

    from sync import ParqetMCP
    client = ParqetMCP(token)

    # Test 1: Initialize
    init_resp = client.initialize()
    if "error" in init_resp:
        return jsonify({
            "ok": False,
            "step": "initialize",
            "error": init_resp.get("error", {}).get("message", "Unbekannter Fehler")
        }), 400

    # Test 2: List Tools
    tools_resp = client.list_tools()
    if isinstance(tools_resp, dict) and "error" in tools_resp:
        return jsonify({
            "ok": False,
            "step": "list_tools",
            "error": tools_resp.get("error", {}).get("message", "Unbekannter Fehler")
        }), 400

    tools = tools_resp.get("result", {}).get("tools", []) if isinstance(tools_resp, dict) else (tools_resp if isinstance(tools_resp, list) else [])
    tool_names = [t.get("name", "") if isinstance(t, dict) else str(t) for t in tools]

    return jsonify({
        "ok": True,
        "initialize": "✓",
        "tools_count": len(tools),
        "tools": tool_names,
        "message": f"✓ Parqet verbunden! {len(tools)} Tools verfügbar."
    })


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------

def _setup_scheduler(cfg: dict):
    schedule.clear()
    h = cfg.get("sync_hour", 20)
    m = cfg.get("sync_minute", 0)
    time_str = f"{h:02d}:{m:02d}"

    def _job():
        from sync import run_sync
        run_sync()

    schedule.every().day.at(time_str).do(_job)
    print(f"[Scheduler] Täglicher Sync um {time_str} Uhr eingestellt.")


def _scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(30)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg = load_config()
    _setup_scheduler(cfg)
    threading.Thread(target=_scheduler_loop, daemon=True).start()

    port = cfg.get("server_port", 5000)
    print(f"\n{'='*50}")
    print(f"  Parqet Portfolio Dashboard")
    print(f"  http://localhost:{port}")
    print(f"{'='*50}\n")

    # Open browser after brief delay
    def _open():
        time.sleep(1.2)
        webbrowser.open(f"http://localhost:{port}")
    threading.Thread(target=_open, daemon=True).start()

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
