"""
Parqet Portfolio Dashboard — Sync-Modul
Holt Holdings via Parqet MCP, berechnet Drawdown + 15J-Rendite via Yahoo Finance,
sendet Discord-Alarme bei Kauf-/Verkaufsmarken.
"""
from __future__ import annotations
import json, time, sqlite3, datetime, re
from pathlib import Path
import requests as http
import yfinance as yf

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "portfolio.db"
# config.json liegt im data/-Ordner (persistiert im Docker-Volume).
CONFIG_PATH = DATA_DIR / "config.json"
OLD_CONFIG_PATH = BASE_DIR / "config.json"  # alter Ort — nur zum Migrieren
MCP_BASE = "https://mcp.parqet.com"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if OLD_CONFIG_PATH.exists():
        return json.loads(OLD_CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Parqet MCP Client
# ---------------------------------------------------------------------------

class ParqetMCP:
    def __init__(self, token: str):
        self.token = token
        self._session_id: str | None = None

    def _post(self, method: str, params: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000) % 2**31,
            "method": method,
            "params": params or {},
        }

        resp = http.post(f"{MCP_BASE}/mcp", headers=headers, json=payload, timeout=30)

        sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid

        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            return self._parse_sse(resp.text)

        if resp.status_code >= 400:
            print(f"[MCP] HTTP {resp.status_code}: {resp.text[:300]}")
            return {}

        try:
            return resp.json()
        except Exception:
            return {}

    @staticmethod
    def _parse_sse(text: str) -> dict:
        for line in text.splitlines():
            if line.startswith("data: ") and len(line) > 6:
                try:
                    return json.loads(line[6:])
                except Exception:
                    pass
        return {}

    def initialize(self) -> dict:
        return self._post("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "parqet-dashboard", "version": "1.0"},
        })

    def list_tools(self) -> dict:
        resp = self._post("tools/list")
        # Handle both dict and list responses
        if isinstance(resp, list):
            return {"result": {"tools": resp}}
        return resp

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        return self._post("tools/call", {"name": name, "arguments": arguments or {}})


def _extract_text_content(result: dict) -> str:
    content = result.get("result", {}).get("content", [])
    texts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            texts.append(item.get("text", ""))
    return "\n".join(texts)


def _parse_json_multiple(text: str) -> dict | list | None:
    """Parse JSON that may contain multiple objects concatenated."""
    text = text.strip()
    if not text:
        return None

    # Try direct parse first (single object)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to extract multiple JSON objects
    decoder = json.JSONDecoder()
    idx = 0
    objects = []
    while idx < len(text):
        text_slice = text[idx:].lstrip()
        if not text_slice:
            break
        try:
            obj, end_idx = decoder.raw_decode(text_slice)
            # Only keep dict and list objects, skip primitives
            if isinstance(obj, (dict, list)):
                objects.append(obj)
            idx += len(text[idx:]) - len(text_slice) + end_idx
        except json.JSONDecodeError:
            break

    if len(objects) == 1:
        return objects[0]
    elif len(objects) > 1:
        return objects[0]  # Return first meaningful object
    return None


def _parse_holdings_from_text(text: str) -> list[dict]:
    """Try to parse JSON from tool response text."""
    text = text.strip()

    # Try to parse JSON (single or multiple)
    data = _parse_json_multiple(text)
    if data is not None:
        # Handle list of objects
        if isinstance(data, list):
            if len(data) == 1 and isinstance(data[0], dict):
                data = data[0]
            else:
                return data

        # Handle single object
        if isinstance(data, dict):
            for key in ("holdings", "positions", "assets", "items", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            # Might be wrapped in portfolios
            for key in ("portfolios", "portfolio"):
                if key in data:
                    portfolios = data[key]
                    if isinstance(portfolios, list):
                        all_holdings = []
                        for p in portfolios:
                            all_holdings.extend(p.get("holdings", p.get("positions", [])))
                        return all_holdings
                    if isinstance(portfolios, dict):
                        return portfolios.get("holdings", portfolios.get("positions", []))

    # Try to extract JSON block from markdown code fence
    match = re.search(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            data = _parse_json_multiple(match.group(1))
            if data:
                return _parse_holdings_from_text(json.dumps(data))
        except Exception:
            pass

    return []


def fetch_parqet_holdings(access_token: str) -> list[dict]:
    if not access_token:
        print("[Parqet] ❌ FEHLER: Kein Access Token vorhanden!")
        return []

    client = ParqetMCP(access_token)

    print("[Parqet] Initialisiere MCP-Verbindung...")
    init_resp = client.initialize()
    if "error" in init_resp:
        print(f"[Parqet] ❌ Initialize-Fehler: {init_resp.get('error')}")
    else:
        print(f"[Parqet] ✓ Initialize OK")

    print("[Parqet] Hole Tool-Liste...")
    tools_resp = client.list_tools()
    if isinstance(tools_resp, dict) and "error" in tools_resp:
        print(f"[Parqet] ❌ Tools-Fehler: {tools_resp.get('error')}")
        return []

    tools = tools_resp.get("result", {}).get("tools", []) if isinstance(tools_resp, dict) else (tools_resp if isinstance(tools_resp, list) else [])
    tool_names = [t.get("name", "") if isinstance(t, dict) else str(t) for t in tools]
    print(f"[Parqet] ✓ {len(tools)} Tools gefunden: {tool_names}")

    if not tools:
        print("[Parqet] ⚠️ Keine Tools gefunden — Token ungültig?")
        return []

    # === STRATEGY 1: parqet_list_portfolios + parqet_query_portfolio ===
    if "parqet_list_portfolios" in tool_names:
        print("[Parqet] 📋 Strategie 1: parqet_list_portfolios → Holdings")
        all_holdings = []

        # Step 1: List all portfolios
        print("[Parqet] Rufe parqet_list_portfolios auf...")
        portfolios_resp = client.call_tool("parqet_list_portfolios")
        portfolios_text = _extract_text_content(portfolios_resp)

        try:
            portfolios_data = _parse_json_multiple(portfolios_text) if portfolios_text.strip() else portfolios_resp.get("result", {})
            if isinstance(portfolios_data, dict) and "items" in portfolios_data:
                portfolios = portfolios_data["items"]
            elif isinstance(portfolios_data, list):
                portfolios = portfolios_data
            else:
                portfolios = []

            print(f"[Parqet] ✓ {len(portfolios)} Portfolios gefunden")
            print(f"[Parqet] Portfolio-Daten: {portfolios}")

            # Step 2: For each portfolio, query holdings
            print(f"[Parqet] Prüfe ob 'parqet_query_portfolio' in Tools: {'parqet_query_portfolio' in tool_names}")
            if "parqet_query_portfolio" in tool_names:
                for portfolio in portfolios:
                    if not isinstance(portfolio, dict):
                        continue
                    pf_id = portfolio.get("id", "")
                    pf_name = portfolio.get("name", "")
                    if not pf_id:
                        continue

                    print(f"[Parqet] 📊 Query Holdings für Portfolio '{pf_name}'")
                    holdings_resp = client.call_tool("parqet_query_portfolio", {
                        "portfolioIds": [pf_id],
                        "view": "holdings"
                    })
                    holdings_text = _extract_text_content(holdings_resp)
                    print(f"[Parqet]   Raw response (erste 400 chars): {str(holdings_resp)[:400]}")
                    print(f"[Parqet]   Text response (erste 400 chars): {holdings_text[:400]}")

                    try:
                        holdings_data = _parse_json_multiple(holdings_text) if holdings_text.strip() else holdings_resp.get("result", {})
                        print(f"[Parqet]   Parsed data type: {type(holdings_data).__name__}")

                        holdings = []
                        if isinstance(holdings_data, dict):
                            print(f"[Parqet]   Dict keys: {list(holdings_data.keys())}")
                            if "holdings" in holdings_data:
                                holdings = holdings_data["holdings"]
                            elif "items" in holdings_data:
                                holdings = holdings_data["items"]
                            else:
                                holdings = list(holdings_data.values())
                        elif isinstance(holdings_data, list):
                            holdings = holdings_data

                        if holdings:
                            # Debug: Print complete holding structure from first item
                            if holdings and isinstance(holdings[0], dict):
                                print(f"[Parqet]   DEBUG — Komplettes Holding-Objekt (erstes):")
                                print(f"[Parqet]   Keys: {list(holdings[0].keys())}")
                                print(f"[Parqet]   Daten: {json.dumps(holdings[0], indent=2)[:1500]}")  # Erste 1500 Zeichen

                            # Add portfolio name to each holding
                            enriched = 0
                            for h in holdings:
                                if isinstance(h, dict):
                                    h["portfolio_name"] = pf_name
                                    enriched += 1
                            all_holdings.extend(holdings)
                            print(f"[Parqet]   ✓ {len(holdings)} Holdings hinzugefügt ({enriched} mit Portfolio-Name '{pf_name}')")
                        else:
                            print(f"[Parqet]   ⚠️ Keine Holdings in dieser Antwort")
                    except Exception as e:
                        print(f"[Parqet]   ❌ Fehler: {str(e)[:150]}")

            if all_holdings:
                print(f"[Parqet] ✅ Insgesamt {len(all_holdings)} Holdings gesammelt")
                return all_holdings

        except json.JSONDecodeError as e:
            print(f"[Parqet] ⚠️ JSON-Parse-Fehler: {e}")

    print("[Parqet] ❌ Konnte keine strukturierten Holdings-Daten extrahieren.")
    return []


def fetch_exchange_rates() -> dict:
    """Fetch current exchange rates from Yahoo Finance."""
    print("[Yahoo] Hole Wechselkurse...")
    rates = {}

    # Wichtigste Währungspaare (vs EUR)
    pairs = [
        ("EURUSD=X", "USD"),
        ("EURDKK=X", "DKK"),
        ("EURGBP=X", "GBP"),
        ("EURSEK=X", "SEK"),
        ("EURCHF=X", "CHF"),
        ("EURJPY=X", "JPY"),
        ("EURCNY=X", "CNY"),
        ("EURINR=X", "INR"),
        ("EURHKD=X", "HKD"),
        ("EURSGD=X", "SGD"),
        ("EURCAD=X", "CAD"),
        ("EURAUD=X", "AUD"),
        ("EURKRW=X", "KRW"),
        ("EURBRL=X", "BRL"),
    ]

    for ticker, currency in pairs:
        try:
            data = yf.Ticker(ticker)
            hist = data.history(period="1d")
            if not hist.empty:
                rate = float(hist["Close"].iloc[-1])
                rates[currency] = rate
                print(f"[Yahoo] ✓ EUR/{currency} = {rate:.4f}")
        except Exception as e:
            print(f"[Yahoo] ⚠️ Fehler bei {currency}: {e}")

    return rates


def refresh_token_if_needed(cfg: dict) -> dict:
    expires_at = cfg.get("parqet_token_expires_at", 0)
    refresh = cfg.get("parqet_refresh_token", "")
    if not refresh or time.time() < expires_at - 300:
        return cfg

    print("[OAuth] Erneuere Access Token...")
    webhook = cfg.get("discord_webhook_url", "")
    try:
        resp = http.post("https://connect.parqet.com/oauth2/token", data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": "019c28d5-e0a0-703f-a790-10c15c2310ee",
        }, timeout=15)
    except Exception as e:
        # Vorübergehender Netzwerkfehler — nächster Sync versucht es erneut.
        print(f"[OAuth] Netzwerkfehler beim Erneuern: {e}")
        send_discord_message(
            webhook,
            "⚠️ Parqet: Token-Erneuerung gestört",
            f"Die Verbindung zu Parqet war kurz nicht erreichbar (`{e}`). "
            f"Der nächste Sync versucht es automatisch erneut — vermutlich nichts zu tun.",
        )
        return cfg

    if resp.status_code == 200:
        data = resp.json()
        cfg["parqet_access_token"] = data.get("access_token", cfg["parqet_access_token"])
        cfg["parqet_refresh_token"] = data.get("refresh_token", refresh)
        cfg["parqet_token_expires_at"] = int(time.time()) + data.get("expires_in", 3600)
        save_config(cfg)
        print("[OAuth] Token erneuert.")
    else:
        # Echte Auth-Fehler (Dauer-Schlüssel abgelaufen/ungültig) — Aktion nötig.
        print(f"[OAuth] Fehler beim Erneuern: {resp.status_code} {resp.text[:200]}")
        send_discord_message(
            webhook,
            "🔴 Parqet-Verbindung abgelaufen — Aktion nötig",
            f"Die automatische Token-Erneuerung ist fehlgeschlagen (HTTP {resp.status_code}). "
            f"Der Dauer-Schlüssel ist vermutlich abgelaufen.\n\n"
            f"**Bitte einmal lokal neu mit Parqet verbinden** und den Token auf den Server "
            f"übertragen (die bekannte 2-Minuten-Prozedur). Bis dahin pausiert der Sync.",
            color=0xEF4444,
        )

    return cfg


# ---------------------------------------------------------------------------
# Yahoo Finance — historische Daten
# ---------------------------------------------------------------------------

def fetch_price_history(ticker: str, years: int = 16) -> list[dict]:
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=f"{years}y", interval="1mo", auto_adjust=True)
        if hist.empty:
            return []
        return [
            {"date": str(idx.date()), "price": float(row["Close"])}
            for idx, row in hist.iterrows()
            if not row["Close"] != row["Close"]  # skip NaN
        ]
    except Exception as e:
        print(f"[Yahoo] Fehler bei {ticker}: {e}")
        return []


def calc_drawdown_metrics(prices: list[dict]) -> dict:
    if len(prices) < 6:
        return {"avg_drawdown_pct": None, "max_drawdown_pct": None, "current_drawdown_pct": None}

    values = [p["price"] for p in sorted(prices, key=lambda x: x["date"])]

    # Peak-to-trough drawdown calculation
    drawdowns: list[float] = []
    peak = values[0]
    trough = values[0]
    in_dd = False

    for v in values[1:]:
        if v >= peak:
            if in_dd:
                drawdowns.append((trough - peak) / peak * 100)
                in_dd = False
            peak = v
            trough = v
        else:
            in_dd = True
            trough = min(trough, v)

    if in_dd:
        drawdowns.append((trough - peak) / peak * 100)

    avg_dd = sum(drawdowns) / len(drawdowns) if drawdowns else 0.0
    max_dd = min(drawdowns) if drawdowns else 0.0

    # Current drawdown vs. all-time high
    all_time_peak = max(values)
    current_dd = (values[-1] - all_time_peak) / all_time_peak * 100 if all_time_peak > 0 else 0.0

    return {
        "avg_drawdown_pct": round(avg_dd, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "current_drawdown_pct": round(current_dd, 2),
    }


def calc_15y_return(prices: list[dict]) -> dict:
    if not prices:
        return {"return_15y_pct": None, "return_15y_cagr": None}

    sorted_prices = sorted(prices, key=lambda x: x["date"])
    today = datetime.date.today()
    target_date = today.replace(year=today.year - 15)

    start_entry = None
    actual_years = 15.0
    for p in sorted_prices:
        pd = datetime.date.fromisoformat(p["date"])
        if pd >= target_date:
            start_entry = p
            actual_years = max((today - pd).days / 365.25, 0.5)
            break

    if start_entry is None:
        start_entry = sorted_prices[0]
        actual_years = max(
            (today - datetime.date.fromisoformat(sorted_prices[0]["date"])).days / 365.25, 0.5
        )

    start_price = start_entry["price"]
    end_price = sorted_prices[-1]["price"]

    if start_price <= 0:
        return {"return_15y_pct": None, "return_15y_cagr": None}

    total_ret = (end_price / start_price - 1) * 100
    cagr = ((end_price / start_price) ** (1 / actual_years) - 1) * 100

    return {
        "return_15y_pct": round(total_ret, 2),
        "return_15y_cagr": round(cagr, 2),
    }


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def send_discord_alert(webhook_url: str, ticker: str, alarm_type: str, price: float, target: float):
    if not webhook_url:
        return
    is_buy = alarm_type == "buy"
    color = 0x22C55E if is_buy else 0xEF4444
    emoji = "🟢" if is_buy else "🔴"
    label = "KAUFSIGNAL" if is_buy else "VERKAUFSSIGNAL"
    direction = "unter die Kaufmarke gefallen" if is_buy else "über die Verkaufsmarke gestiegen"

    payload = {
        "embeds": [{
            "title": f"{emoji} {label}: {ticker}",
            "description": f"**{ticker}** ist {direction}.",
            "color": color,
            "fields": [
                {"name": "Aktueller Kurs", "value": f"{price:.2f} €", "inline": True},
                {"name": "Zielmarke", "value": f"{target:.2f} €", "inline": True},
            ],
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "footer": {"text": "Parqet Dashboard · Automatischer Alarm"},
        }]
    }
    try:
        r = http.post(webhook_url, json=payload, timeout=10)
        if r.status_code not in (200, 204):
            print(f"[Discord] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Discord] Fehler: {e}")


def send_discord_message(webhook_url: str, title: str, description: str, color: int = 0xF59E0B):
    """Generische System-Nachricht (z. B. Frühwarnung bei Token-Problemen)."""
    if not webhook_url:
        return
    payload = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "footer": {"text": "Parqet Dashboard · System"},
        }]
    }
    try:
        r = http.post(webhook_url, json=payload, timeout=10)
        if r.status_code not in (200, 204):
            print(f"[Discord] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Discord] Fehler: {e}")


# ---------------------------------------------------------------------------
# Main Sync
# ---------------------------------------------------------------------------

def _normalize_holding(h: dict) -> dict | None:
    """Normalize Parqet API response into a flat dict."""

    # Extract ticker/identifier
    ticker = (
        h.get("identifier") or
        h.get("ticker") or h.get("symbol") or h.get("isin") or
        h.get("assetTicker") or h.get("wkn") or ""
    ).upper().strip()

    # For crypto, use the identifier as ticker if no better option
    if not ticker and h.get("assetType") == "crypto":
        ticker = h.get("name", "").upper().strip()

    if not ticker:
        return None

    purchase_price = float(
        h.get("purchasePrice") or h.get("avgBuyPrice") or
        h.get("averageBuyPrice") or h.get("buyPrice") or h.get("averagePrice") or 0
    )
    current_price = float(
        h.get("currentPrice") or h.get("lastPrice") or
        h.get("price") or h.get("marketPrice") or h.get("recentPrice") or 0
    )

    # Rendite selbst aus Einstand/Kurs berechnen — Parqet liefert die
    # Rendite-Felder oft nicht (dann stand +/- immer auf 0,00 %).
    # Fallback: ein evtl. doch geliefertes Parqet-Feld.
    parqet_ret = float(
        h.get("returnPct") or h.get("return_pct") or
        h.get("totalReturn") or h.get("return") or h.get("performance") or 0
    )
    if purchase_price > 0 and current_price > 0:
        total_return_pct = (current_price / purchase_price - 1) * 100
    else:
        total_return_pct = parqet_ret

    return {
        "ticker": ticker,
        "name": h.get("name") or h.get("nickname") or h.get("assetName") or h.get("title") or ticker,
        "isin": h.get("isin") or h.get("identifier") or "",
        "quantity": float(h.get("quantity") or h.get("shares") or h.get("amount") or h.get("position") or 0),
        "purchase_price": purchase_price,
        "current_price": current_price,
        "current_value": float(
            h.get("currentValue") or h.get("totalValue") or
            h.get("marketValue") or h.get("value") or 0
        ),
        "total_return_pct": total_return_pct,
        "weight": float(
            h.get("weight") or h.get("portfolioWeight") or
            h.get("allocation") or 0
        ),
        "portfolio_name": h.get("portfolio_name") or "",
    }


def run_sync():
    print(f"\n{'='*50}")
    print(f"[Sync] Start: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    cfg = load_config()
    cfg = refresh_token_if_needed(cfg)

    access_token = cfg.get("parqet_access_token", "")
    if not access_token:
        print("[Sync] Kein Access Token — bitte zuerst Parqet verbinden.")
        return False

    # === STEP 0: Fetch exchange rates ===
    rates = fetch_exchange_rates()
    if rates:
        with get_db() as db:
            for currency, rate in rates.items():
                db.execute(
                    "INSERT INTO exchange_rates (currency, rate, updated_at) VALUES (?, ?, datetime('now')) ON CONFLICT(currency) DO UPDATE SET rate = excluded.rate, updated_at = excluded.updated_at",
                    (currency, rate)
                )
        print(f"[Sync] ✓ {len(rates)} Wechselkurse aktualisiert")

    # --- Step 1: Fetch holdings from Parqet ---
    raw_holdings = fetch_parqet_holdings(access_token)
    holdings = [n for h in raw_holdings if (n := _normalize_holding(h)) is not None]

    if not holdings:
        print("[Sync] Keine Holdings-Daten — Sync abgebrochen.")
        return False

    print(f"[Sync] {len(holdings)} normalisierte Positionen")

    # --- Step 2: Persist holdings in DB ---
    print(f"[Sync] Sample holdings (erste 3): {holdings[:3] if holdings else 'keine'}")
    with get_db() as db:
        saved_count = 0
        for h in holdings:
            portfolio_name = h.get("portfolio_name", "")
            if portfolio_name:
                saved_count += 1
            db.execute("""
                INSERT INTO holdings
                    (ticker, name, isin, quantity, purchase_price,
                     current_price, current_value, total_return_pct, weight, portfolio_name, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT(ticker) DO UPDATE SET
                    name            = excluded.name,
                    current_price   = excluded.current_price,
                    current_value   = excluded.current_value,
                    total_return_pct= excluded.total_return_pct,
                    weight          = excluded.weight,
                    portfolio_name  = excluded.portfolio_name,
                    synced_at       = excluded.synced_at
            """, (
                h["ticker"], h["name"], h["isin"],
                h["quantity"], h["purchase_price"],
                h["current_price"], h["current_value"],
                h["total_return_pct"], h["weight"], portfolio_name,
            ))
        print(f"[Sync] ✓ {saved_count} Holdings mit Portfolio-Namen gespeichert")

    # --- Step 3: Fetch historical data + compute metrics ---
    tickers = [h["ticker"] for h in holdings]
    for ticker in tickers:
        print(f"[Yahoo] Historische Daten für {ticker}...")
        prices = fetch_price_history(ticker, years=16)

        if not prices:
            print(f"[Yahoo] Keine Daten für {ticker} — übersprungen.")
            continue

        dd = calc_drawdown_metrics(prices)
        ret = calc_15y_return(prices)

        with get_db() as db:
            db.execute("""
                INSERT INTO metrics
                    (ticker, avg_drawdown_pct, max_drawdown_pct,
                     current_drawdown_pct, return_15y_pct, return_15y_cagr, computed_at)
                VALUES (?,?,?,?,?,?,datetime('now'))
                ON CONFLICT(ticker) DO UPDATE SET
                    avg_drawdown_pct     = excluded.avg_drawdown_pct,
                    max_drawdown_pct     = excluded.max_drawdown_pct,
                    current_drawdown_pct = excluded.current_drawdown_pct,
                    return_15y_pct       = excluded.return_15y_pct,
                    return_15y_cagr      = excluded.return_15y_cagr,
                    computed_at          = excluded.computed_at
            """, (
                ticker,
                dd["avg_drawdown_pct"], dd["max_drawdown_pct"],
                dd["current_drawdown_pct"], ret["return_15y_pct"], ret["return_15y_cagr"],
            ))

    # --- Step 4: Check buy/sell alarms ---
    discord_url = cfg.get("discord_webhook_url", "")
    today_str = datetime.date.today().isoformat()

    with get_db() as db:
        rows = db.execute("""
            SELECT h.ticker, h.current_price, a.buy_target, a.sell_target
            FROM holdings h
            JOIN annotations a ON h.ticker = a.ticker
            WHERE a.buy_target IS NOT NULL OR a.sell_target IS NOT NULL
        """).fetchall()

        for row in rows:
            ticker = row["ticker"]
            price = row["current_price"] or 0.0
            buy_t = row["buy_target"]
            sell_t = row["sell_target"]

            if buy_t and price <= buy_t:
                already = db.execute(
                    "SELECT 1 FROM alarm_log WHERE ticker=? AND alarm_type='buy' AND date(triggered_at)=?",
                    (ticker, today_str)
                ).fetchone()
                if not already:
                    db.execute(
                        "INSERT INTO alarm_log (ticker, alarm_type, price) VALUES (?, 'buy', ?)",
                        (ticker, price)
                    )
                    send_discord_alert(discord_url, ticker, "buy", price, buy_t)
                    print(f"[Alarm] KAUF  {ticker} Kurs={price:.2f} <= Marke={buy_t:.2f}")

            if sell_t and price >= sell_t:
                already = db.execute(
                    "SELECT 1 FROM alarm_log WHERE ticker=? AND alarm_type='sell' AND date(triggered_at)=?",
                    (ticker, today_str)
                ).fetchone()
                if not already:
                    db.execute(
                        "INSERT INTO alarm_log (ticker, alarm_type, price) VALUES (?, 'sell', ?)",
                        (ticker, price)
                    )
                    send_discord_alert(discord_url, ticker, "sell", price, sell_t)
                    print(f"[Alarm] VERK  {ticker} Kurs={price:.2f} >= Marke={sell_t:.2f}")

    print(f"[Sync] Abgeschlossen: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return True


if __name__ == "__main__":
    run_sync()
