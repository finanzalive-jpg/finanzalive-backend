"""
IUPPITER — Backend FastAPI v3
Webhook Telegram + API dashboard clienti
Parser automatico: Vanilla Mensile, Forex, Indici World
"""

from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os
import re
from datetime import datetime
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Iuppiter API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TELEGRAM_BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_API         = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
ADMIN_SECRET         = os.environ["ADMIN_SECRET"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

CHANNEL_SERVICE_MAP = {
    -1002552300319: "indices",
    -1002517239703: "vanilla_monthly",
    -1002870950901: "vanilla_weekly",
    -1002850487439: "forex",
}

INDEX_SYMBOLS = ["US100", "US30", "DE40", "JPN225"]

# ─────────────────────────────────────────────
# PARSER SEGNALI
# ─────────────────────────────────────────────
def parse_signal(text: str) -> dict:
    text = text.replace("\n", " ").replace("\r", " ")
    data = {
        "signal_type": "INFO", "direction": None, "strike": None,
        "strike_pct": None, "premium": None, "drawdown_max": None,
        "pnl": None, "symbol": None, "price": None, "tp": None
    }
    t = text.upper()

    # VANILLA MENSILE ingresso
    if "STRIKE:" in t and ("SELL PUT" in t or "BUY PUT" in t or "SCADENZA" in t or "SELL_PUT" in t):
        data["signal_type"] = "OPEN"
        data["direction"] = "SELL_PUT" if ("SELL PUT" in t or "SELL_PUT" in t) else "BUY_PUT"
        m = re.search(r"STRIKE:\s*([\d,\.]+)", t)
        if m: data["strike"] = float(m.group(1).replace(",", ""))
        m_spot = re.search(r"SPOT:\s*([\d,\.]+)", t)
        if m and m_spot:
            strike = float(m.group(1).replace(",", ""))
            spot   = float(m_spot.group(1).replace(",", ""))
            if spot > 0:
                data["strike_pct"] = round(abs(spot - strike) / spot * 100, 2)
                data["premium"]    = round(data["strike_pct"] * 100, 2)
        return data

    # VANILLA MENSILE chiusura
    if "CHIUSURA PUTSELL" in t or "CHIUSURA PUTBUY" in t:
        data["signal_type"] = "CLOSE"
        data["direction"] = "SELL_PUT" if "PUTSELL" in t else "BUY_PUT"
        m = re.search(r"STRIKE:\s*([\d,\.]+)", t)
        if m: data["strike"] = float(m.group(1).replace(",", ""))
        m = re.search(r"MAX DD:\s*([\d\.]+)", t)
        if m: data["drawdown_max"] = float(m.group(1))
        data["pnl"] = 1 if "PROFITTO" in t else -1
        return data

    # INDICI chiusura
    for sym in INDEX_SYMBOLS:
        if sym + "CHIUSURA" in t or (sym in t and "CHIUSURA" in t):
            data["signal_type"] = "CLOSE"
            data["symbol"] = sym
            m_dir = re.search(r"CHIUSURA\s+(BUY|SELL)", t)
            if m_dir: data["direction"] = m_dir.group(1)
            return data

    # INDICI ingresso
    for sym in INDEX_SYMBOLS:
        if sym in t and "SCALPING" in t and "APERTURA" in t:
            data["signal_type"] = "OPEN"
            data["symbol"] = sym
            m_dir = re.search(sym + r"\s*(BUY|SELL)", t)
            if not m_dir: m_dir = re.search(r"ALERT FOR " + sym + r"(BUY|SELL)", t)
            if m_dir: data["direction"] = m_dir.group(1)
            m_price = re.search(r"APERTURA:\s*([\d,\.]+)", t)
            if m_price: data["price"] = float(m_price.group(1).replace(",", ""))
            return data

    # FOREX chiusura
    m = re.search(r"ALERT FOR ([A-Z]{6})CLOSE\s+(BUY|SELL)", t)
    if m:
        data["signal_type"] = "CLOSE"
        data["symbol"]    = m.group(1)
        data["direction"] = m.group(2)
        return data

    # FOREX ingresso
    # Supporta: "AUDCHF BUY", "EURUSD 🚀 SELL", "AUDCHF  BUY" (doppio spazio)
    m_sym = re.search(r"ALERT FOR ([A-Z]{6,7})", t)
    m_dir = re.search(r"\b(BUY|SELL)\b", t)
    if m_sym and m_dir and "CHIUSURA" not in t and "SCALPING" not in t and "CLOSE" not in t.split()[0]:
        data["signal_type"] = "OPEN"
        data["symbol"]    = m_sym.group(1)
        data["direction"] = m_dir.group(1)
        m_tp = re.search(r"TP:\s*([\d\.]+)", t)
        if m_tp: data["tp"] = float(m_tp.group(1))
        m_ap = re.search(r"APERTURA:\s*([\d,\.]+)", t)
        if m_ap: data["price"] = float(m_ap.group(1).replace(",",""))
        return data

    # FALLBACK
    if any(w in t for w in ["APERTURA", "OPEN", "ENTRY"]): data["signal_type"] = "OPEN"
    elif any(w in t for w in ["CHIUSURA", "CLOSE", "EXIT"]): data["signal_type"] = "CLOSE"
    elif any(w in t for w in ["TRIGGER", "WARNING"]): data["signal_type"] = "ALERT"
    # NOTA: "ALERT" rimosso dal fallback perché appare in tutti i messaggi "Alert for..."
    return data

# ─────────────────────────────────────────────
# AUTO AGGIORNAMENTO TRADES
# ─────────────────────────────────────────────
async def auto_update_trades(service_id: int, service_code: str, parsed: dict, text: str):
    if service_code not in ["vanilla_monthly", "forex", "indices"]:
        return
    try:
        symbol = parsed.get("symbol", "")

        if parsed["signal_type"] == "OPEN":
            insert_data = {
                "service_id": service_id,
                "direction":  parsed.get("direction"),
                "status":     "OPEN",
                "opened_at":  datetime.utcnow().isoformat(),
                "notes":      f"Auto - {symbol or service_code} {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}",
            }
            if parsed.get("strike"):     insert_data["strike"]           = parsed["strike"]
            if parsed.get("price"):      insert_data["strike"]           = parsed["price"]
            if parsed.get("strike_pct"): insert_data["strike_pct"]       = parsed["strike_pct"]
            if parsed.get("premium"):    insert_data["premium_collected"] = parsed["premium"]
            supabase.table("trades").insert(insert_data).execute()

        elif parsed["signal_type"] == "CLOSE":
            # Cerca la trade OPEN con il simbolo corretto
            q = supabase.table("trades").select("id, strike, direction")                .eq("service_id", service_id)                .eq("status", "OPEN")                .order("opened_at", desc=True)                .execute()

            trade_id = None
            trade_entry = None
            trade_direction = None
            if q.data and symbol:
                for trade in q.data:
                    notes = trade.get("notes", "") or ""
                    if symbol.upper() in notes.upper():
                        trade_id = trade["id"]
                        trade_entry = trade.get("strike")
                        trade_direction = trade.get("direction")
                        break
                if not trade_id:
                    print(f"Close signal for {symbol} but no matching OPEN trade found - skipping")
                    return

            if trade_id:
                update_data = {
                    "status":    "CLOSED",
                    "closed_at": datetime.utcnow().isoformat(),
                }
                if parsed.get("drawdown_max"): update_data["drawdown_max"] = parsed["drawdown_max"]

                # Calcola PnL da prezzo ingresso e uscita
                exit_price = parsed.get("price")
                if exit_price and trade_entry:
                    entry = float(trade_entry)
                    direction = trade_direction or parsed.get("direction", "")
                    if direction in ["BUY", "BUY_PUT"]:
                        pnl = round(exit_price - entry, 2)
                    else:  # SELL
                        pnl = round(entry - exit_price, 2)
                    update_data["pnl"] = pnl
                elif parsed.get("pnl"):
                    update_data["pnl"] = parsed["pnl"]

                supabase.table("trades").update(update_data).eq("id", trade_id).execute()
    except Exception as e:
        print(f"Auto trade error: {e}")

# ─────────────────────────────────────────────
# WEBHOOK TELEGRAM
# ─────────────────────────────────────────────
@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    try:
        body = await request.json()
    except:
        return {"ok": True}

    message = body.get("message") or body.get("channel_post")
    if not message:
        return {"ok": True}

    chat_id = message.get("chat", {}).get("id")
    msg_id  = message.get("message_id")
    text    = message.get("text") or message.get("caption", "")
    if not text:
        return {"ok": True}

    service_code = CHANNEL_SERVICE_MAP.get(chat_id)
    if not service_code:
        return {"ok": True}

    try:
        svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
        service_id = svc.data["id"]
    except:
        return {"ok": True}

    parsed = parse_signal(text)

    try:
        sig = supabase.table("signals").insert({
            "service_id":          service_id,
            "telegram_message_id": msg_id,
            "telegram_chat_id":    chat_id,
            "message_text":        text,
            "signal_type":         parsed["signal_type"],
            "direction":           parsed["direction"],
            "strike":              parsed["strike"],
            "strike_pct":          parsed["strike_pct"],
            "premium":             parsed["premium"],
            "drawdown_max":        parsed["drawdown_max"],
            "pnl":                 parsed["pnl"],
            "raw_json":            body,
        }).execute()
        signal_id = sig.data[0]["id"]
    except Exception as e:
        print(f"DB error: {e}")
        return {"ok": True}

    await notify_subscribers(service_id, signal_id, text, service_code)
    await auto_update_trades(service_id, service_code, parsed, text)
    return {"ok": True}

# ─────────────────────────────────────────────
# NOTIFICA SUBSCRIBERS
# ─────────────────────────────────────────────
async def notify_subscribers(service_id: int, signal_id: int, text: str, service_code: str):
    try:
        subs = supabase.table("subscriptions")            .select("client_id, clients(telegram_chat_id)")            .eq("service_id", service_id).eq("active", True).execute()
    except:
        return

    svc_names = {
        "indices":         "🌐 Sala Indici World",
        "vanilla_monthly": "📅 Vanilla Mensile",
        "vanilla_weekly":  "📆 Vanilla Settimanale",
        "forex":           "💱 Sala Forex",
        "fund_paam":       "💼 Fondo PAAM",
    }
    msg = f"🔔 *{svc_names.get(service_code, service_code)}*\n\n{text}"

    async with httpx.AsyncClient() as client:
        for sub in subs.data:
            c = sub.get("clients") or {}
            tg_id = c.get("telegram_chat_id")
            if not tg_id:
                continue
            try:
                r = await client.post(f"{TELEGRAM_API}/sendMessage", json={
                    "chat_id": tg_id, "text": msg, "parse_mode": "Markdown"
                }, timeout=10)
                sent = r.status_code == 200
                err  = None if sent else r.text
            except Exception as e:
                sent = False; err = str(e)
            try:
                supabase.table("notifications").insert({
                    "signal_id": signal_id, "client_id": sub["client_id"],
                    "channel": "telegram", "sent": sent,
                    "sent_at": datetime.utcnow().isoformat() if sent else None,
                    "error": err,
                }).execute()
            except:
                pass

# ─────────────────────────────────────────────
# AUTH HELPERS
# ─────────────────────────────────────────────
def get_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token mancante")
    try:
        return supabase.auth.get_user(authorization.split(" ")[1]).user
    except:
        raise HTTPException(status_code=401, detail="Token non valido")

def require_admin(x_admin_secret: str = Header(None)):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Accesso negato")

# ─────────────────────────────────────────────
# API CLIENTI
# ─────────────────────────────────────────────
@app.get("/api/signals")
async def get_signals(service_code: str = None, limit: int = 50, user=Depends(get_user)):
    subs = supabase.table("subscriptions")        .select("service_id, services(code)").eq("client_id", str(user.id)).eq("active", True).execute()
    allowed_codes = [s["services"]["code"] for s in subs.data]
    allowed_ids   = [s["service_id"] for s in subs.data]
    if service_code:
        if service_code not in allowed_codes:
            raise HTTPException(status_code=403, detail="Non abbonato")
        svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
        result = supabase.table("signals").select("*, services(code,name)")            .eq("service_id", svc.data["id"]).order("created_at", desc=True).limit(limit).execute()
    else:
        result = supabase.table("signals").select("*, services(code,name)")            .in_("service_id", allowed_ids).order("created_at", desc=True).limit(limit).execute()
    return result.data

@app.get("/api/trades")
async def get_trades(service_code: str = None, user=Depends(get_user)):
    subs = supabase.table("subscriptions")        .select("service_id, services(code)").eq("client_id", str(user.id)).eq("active", True).execute()
    allowed_ids   = [s["service_id"] for s in subs.data]
    allowed_codes = [s["services"]["code"] for s in subs.data]
    if service_code:
        if service_code not in allowed_codes:
            raise HTTPException(status_code=403, detail="Non abbonato")
        svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
        result = supabase.table("trades").select("*, services(code,name)")            .eq("service_id", svc.data["id"]).order("opened_at", desc=True).execute()
    else:
        result = supabase.table("trades").select("*, services(code,name)")            .in_("service_id", allowed_ids).order("opened_at", desc=True).execute()
    return result.data

@app.get("/api/services")
async def get_my_services(user=Depends(get_user)):
    return supabase.table("subscriptions")        .select("active, expires_at, amount, notes, services(code,name,description)")        .eq("client_id", str(user.id)).execute().data

@app.get("/api/profile")
async def get_profile(user=Depends(get_user)):
    return supabase.table("clients").select("id,full_name,email,active,created_at")        .eq("id", str(user.id)).single().execute().data

# ─────────────────────────────────────────────
# ADMIN API
# ─────────────────────────────────────────────
@app.post("/admin/client", dependencies=[Depends(require_admin)])
async def create_client(data: dict):
    auth_user = supabase.auth.admin.create_user({
        "email": data["email"], "password": data["password"], "email_confirm": True,
    })
    uid = auth_user.user.id
    supabase.table("clients").insert({
        "id": uid, "full_name": data.get("full_name",""), "email": data["email"],
        "telegram_chat_id": data.get("telegram_chat_id"),
        "telegram_username": data.get("telegram_username"),
    }).execute()
    for svc_code in data.get("services", []):
        svc = supabase.table("services").select("id").eq("code", svc_code).single().execute()
        supabase.table("subscriptions").insert({
            "client_id": uid, "service_id": svc.data["id"],
            "expires_at": data.get("expires_at"),
            "amount": data.get("amount"),
            "notes": data.get("notes"),
        }).execute()
    return {"ok": True, "client_id": str(uid)}

@app.get("/admin/clients", dependencies=[Depends(require_admin)])
async def list_clients():
    return supabase.table("clients")        .select("*, subscriptions(active, expires_at, amount, notes, services(name,code))").execute().data

@app.patch("/admin/subscription/{client_id}/{service_code}", dependencies=[Depends(require_admin)])
async def toggle_sub(client_id: str, service_code: str, active: bool):
    svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
    supabase.table("subscriptions").update({"active": active})        .eq("client_id", client_id).eq("service_id", svc.data["id"]).execute()
    return {"ok": True}

@app.patch("/admin/subscription/{client_id}/{service_code}/renew", dependencies=[Depends(require_admin)])
async def renew_sub(client_id: str, service_code: str, data: dict):
    svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
    update_data = {"active": True}
    if data.get("expires_at"): update_data["expires_at"] = data["expires_at"]
    if data.get("amount") is not None: update_data["amount"] = data["amount"]
    if data.get("notes") is not None: update_data["notes"] = data["notes"]
    supabase.table("subscriptions").update(update_data)        .eq("client_id", client_id).eq("service_id", svc.data["id"]).execute()
    return {"ok": True}

@app.get("/admin/signals", dependencies=[Depends(require_admin)])
async def admin_signals(limit: int = 100):
    return supabase.table("signals").select("*, services(name)")        .order("created_at", desc=True).limit(limit).execute().data


@app.get("/api/quotes")
async def get_quotes():
    """Recupera quotazioni mercati in tempo reale via Yahoo Finance"""
    symbols = {
        "S&P 500":   "%5EGSPC",
        "Nasdaq":    "%5EIXIC",
        "Dow Jones": "%5EDJI",
        "DAX":       "%5EGDAXI",
        "Nikkei":    "%5EN225",
        "UK 100":    "%5EFTSE",
        "EUR/USD":   "EURUSD%3DX",
        "Oro":       "GC%3DF",
        "Petrolio":  "CL%3DF",
        "Bitcoin":   "BTC-USD",
        "VIX":       "%5EVIX",
        "BTP 10Y":   "BTP10Y%3DX",
    }
    results = []
    async with httpx.AsyncClient(timeout=8) as client:
        for name, sym in symbols.items():
            try:
                r = await client.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2d",
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                d = r.json()
                meta = d["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice", 0)
                prev  = meta.get("previousClose") or meta.get("chartPreviousClose", price)
                chg   = ((price - prev) / prev * 100) if prev else 0
                results.append({
                    "name":   name,
                    "price":  round(price, 4),
                    "change": round(chg, 2),
                    "up":     chg >= 0
                })
            except:
                results.append({"name": name, "price": 0, "change": 0, "up": True, "error": True})
    return results


@app.get("/api/news")
async def get_news():
    return supabase.table("news").select("*").eq("active", True).order("created_at", desc=True).limit(10).execute().data

@app.post("/admin/news", dependencies=[Depends(require_admin)])
async def create_news(data: dict):
    supabase.table("news").insert({
        "message": data["message"],
        "active": True
    }).execute()
    return {"ok": True}

@app.delete("/admin/news/{news_id}", dependencies=[Depends(require_admin)])
async def delete_news(news_id: int):
    supabase.table("news").update({"active": False}).eq("id", news_id).execute()
    return {"ok": True}

@app.get("/health")
@app.head("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
