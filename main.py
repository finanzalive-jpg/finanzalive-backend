"""
IUPPITER — Backend FastAPI v3
Webhook Telegram + API dashboard clienti
Parser automatico: Vanilla Mensile, Forex, Indici World

Sorgenti segnali:
  - Telegram  → Gold, Vanilla Mensile, Vanilla Settimanale
  - MT4 (EA)  → Indici World, Forex, Fondo PAMM (e altri)
"""

from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os
import re
import hashlib
import hmac as _hmac
import base64 as _b64
import time as _time
import json as _json
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
SUPERADMIN_SECRET    = os.environ.get("SUPERADMIN_SECRET", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

CHANNEL_SERVICE_MAP = {
    -1002552300319: "indices",
    -1002517239703: "vanilla_monthly",
    -1002870950901: "vanilla_weekly",
    -1002850487439: "forex",
    -1002628465268: "gold",
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

    # VANILLA MENSILE chiusura — va controllata PRIMA dell'ingresso
    if "CHIUSURA PUTSELL" in t or "CHIUSURA PUTBUY" in t:
        data["signal_type"] = "CLOSE"
        data["direction"] = "SELL_PUT" if "PUTSELL" in t else "BUY_PUT"
        m = re.search(r"STRIKE:\s*([\d,\.]+)", t)
        if m: data["strike"] = float(m.group(1).replace(",", ""))
        m = re.search(r"MAX DD:\s*([\d\.]+)", t)
        if m: data["drawdown_max"] = float(m.group(1))
        data["pnl"] = 1 if "PROFITTO" in t else -1
        return data

    # VANILLA MENSILE chiusura — formato "Chiusura trade - Esito: CHIUSURA IN PROFITTO"
    if "CHIUSURA" in t and "STRIKE:" in t and ("SELL PUT" in t or "BUY PUT" in t or "SELL_PUT" in t):
        data["signal_type"] = "CLOSE"
        data["direction"] = "SELL_PUT" if ("SELL PUT" in t or "SELL_PUT" in t) else "BUY_PUT"
        m = re.search(r"STRIKE:\s*([\d,\.]+)", t)
        if m: data["strike"] = float(m.group(1).replace(",", ""))
        m = re.search(r"MAX DD:\s*([\d\.]+)", t)
        if m: data["drawdown_max"] = float(m.group(1))
        data["pnl"] = 1 if "PROFITTO" in t else -1
        print(f"VANILLA CLOSE parsed: strike={data['strike']} pnl={data['pnl']}")
        return data

    # VANILLA MENSILE ingresso — solo se NON e' una chiusura
    if "STRIKE:" in t and ("SELL PUT" in t or "BUY PUT" in t or "SCADENZA" in t or "SELL_PUT" in t) and "CHIUSURA" not in t:
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

    # GOLD ingresso
    if "GOLD" in t and ("BUY" in t or "LONG" in t or "ACQUISTO" in t) and "CHIUSURA" not in t and "CLOSE" not in t:
        data["signal_type"] = "OPEN"
        data["symbol"] = "GOLD"
        data["direction"] = "BUY"
        m_price = re.search(r"(?:APERTURA|ENTRY|PREZZO|@)\s*:?\s*([\d,\.]+)", t)
        if m_price: data["price"] = float(m_price.group(1).replace(",", ""))
        return data

    if "GOLD" in t and ("SELL" in t or "SHORT" in t or "VENDITA" in t) and "CHIUSURA" not in t and "CLOSE" not in t:
        data["signal_type"] = "OPEN"
        data["symbol"] = "GOLD"
        data["direction"] = "SELL"
        m_price = re.search(r"(?:APERTURA|ENTRY|PREZZO|@)\s*:?\s*([\d,\.]+)", t)
        if m_price: data["price"] = float(m_price.group(1).replace(",", ""))
        return data

    # GOLD chiusura
    if "GOLD" in t and ("CHIUSURA" in t or "CLOSE" in t or "EXIT" in t or "USCITA" in t):
        data["signal_type"] = "CLOSE"
        data["symbol"] = "GOLD"
        m_dir = re.search(r"(BUY|SELL|LONG|SHORT)", t)
        if m_dir:
            d = m_dir.group(1)
            data["direction"] = "BUY" if d in ("BUY", "LONG") else "SELL"
        m_exit = re.search(r"(?:USCITA|EXIT|CLOSE|@)\s*:?\s*([\d,\.]+)", t)
        if m_exit: data["price"] = float(m_exit.group(1).replace(",", ""))
        return data

    # INDICI chiusura
    for sym in INDEX_SYMBOLS:
        if sym in t and "CHIUSURA" in t:
            data["signal_type"] = "CLOSE"
            data["symbol"] = sym
            m_dir = re.search(r"CHIUSURA\s+(BUY|SELL)", t)
            if m_dir: data["direction"] = m_dir.group(1)
            else:
                m_dir2 = re.search(r"\b(BUY|SELL)\b", t)
                if m_dir2: data["direction"] = m_dir2.group(1)
            m_exit = re.search(r"USCITA:\s*([\d,\.]+)", t)
            if m_exit:
                data["price"] = float(m_exit.group(1).replace(",", ""))
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

    # ── FIX #1: FOREX chiusura — formato "Alert for AUDCHF\nCLOSE SELL AUDCHF.ecn"
    m_sym_close = re.search(r"ALERT FOR ([A-Z]{6,7})", t)
    if m_sym_close and "CLOSE" in t:
        m_dir_close = re.search(r"CLOSE\s+(BUY|SELL)", t)
        if m_dir_close:
            data["signal_type"] = "CLOSE"
            data["symbol"]    = m_sym_close.group(1)
            data["direction"] = m_dir_close.group(1)
            print(f"FOREX CLOSE parsed: sym={data['symbol']} dir={data['direction']}")
            return data

    # FOREX chiusura — formato vecchio "ALERT FOR AUDCHFCLOSE BUY/SELL"
    m = re.search(r"ALERT FOR ([A-Z]{6})CLOSE\s+(BUY|SELL)", t)
    if m:
        data["signal_type"] = "CLOSE"
        data["symbol"]    = m.group(1)
        data["direction"] = m.group(2)
        return data

    # FOREX ingresso
    m_sym = re.search(r"ALERT FOR ([A-Z]{6,7})", t)
    m_dir = re.search(r"\b(BUY|SELL)\b", t)
    print(f"FOREX check: sym={m_sym.group(1) if m_sym else None} dir={m_dir.group(1) if m_dir else None} CHIUSURA={'CHIUSURA' in t} SCALPING={'SCALPING' in t}")
    if m_sym and m_dir and "CHIUSURA" not in t and "SCALPING" not in t and "CLOSE" not in t:
        data["signal_type"] = "OPEN"
        data["symbol"]    = m_sym.group(1)
        data["direction"] = m_dir.group(1)
        m_tp = re.search(r"TP:\s*([\d\.]+)", t)
        if m_tp: data["tp"] = float(m_tp.group(1))
        m_ap = re.search(r"APERTURA:\s*([\d,\.]+)", t)
        if m_ap: data["price"] = float(m_ap.group(1).replace(",",""))
        return data

    # FALLBACK
    print(f"FALLBACK reached for: {t[:80]}")
    if any(w in t for w in ["APERTURA", "OPEN", "ENTRY"]): data["signal_type"] = "OPEN"
    elif any(w in t for w in ["CHIUSURA", "CLOSE", "EXIT"]): data["signal_type"] = "CLOSE"
    elif any(w in t for w in ["TRIGGER", "WARNING"]): data["signal_type"] = "ALERT"
    return data

# ─────────────────────────────────────────────
# AUTO AGGIORNAMENTO TRADES
# ─────────────────────────────────────────────
async def auto_update_trades(service_id: int, service_code: str, parsed: dict, text: str):
    # MT4 gestisce: indices, forex, fund_pamm
    # Telegram gestisce: gold, vanilla_monthly, vanilla_weekly
    if service_code not in ["gold", "vanilla_monthly", "vanilla_weekly"]:
        return
    try:
        symbol = parsed.get("symbol", "") or ""
        print(f"AUTO_TRADE: service={service_code} type={parsed['signal_type']} symbol={symbol} price={parsed.get('price')} direction={parsed.get('direction')}")

        if parsed["signal_type"] == "OPEN":
            note_label = symbol if symbol else service_code.upper()
            insert_data = {
                "service_id": service_id,
                "direction":  parsed.get("direction"),
                "status":     "OPEN",
                "opened_at":  datetime.utcnow().isoformat(),
                "notes":      f"Auto - {note_label} {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}",
            }
            if parsed.get("strike"):     insert_data["strike"]           = parsed["strike"]
            if parsed.get("price"):      insert_data["strike"]           = parsed["price"]
            if parsed.get("strike_pct"): insert_data["strike_pct"]       = parsed["strike_pct"]
            if parsed.get("premium"):    insert_data["premium_collected"] = parsed["premium"]
            supabase.table("trades").insert(insert_data).execute()
            print(f"AUTO_TRADE OPEN inserted: {note_label} @ {parsed.get('price') or parsed.get('strike')}")

        elif parsed["signal_type"] == "CLOSE":
            q = supabase.table("trades").select("id, strike, direction") \
                .eq("service_id", service_id) \
                .eq("status", "OPEN") \
                .order("opened_at", desc=True) \
                .execute()

            trade_id = None
            trade_entry = None
            trade_direction = None

            if q.data:
                if symbol:
                    for trade in q.data:
                        notes = trade.get("notes", "") or ""
                        if symbol.upper() in notes.upper():
                            trade_id = trade["id"]
                            trade_entry = trade.get("strike")
                            trade_direction = trade.get("direction")
                            break
                    if not trade_id:
                        if service_code == "gold":
                            trade_id = q.data[0]["id"]
                            trade_entry = q.data[0].get("strike")
                            trade_direction = q.data[0].get("direction")
                        else:
                            print(f"AUTO_TRADE CLOSE: no OPEN trade found for symbol={symbol} in service={service_code}")
                            return
                else:
                    trade_id = q.data[0]["id"]
                    trade_entry = q.data[0].get("strike")
                    trade_direction = q.data[0].get("direction")

            if trade_id:
                update_data = {
                    "status":    "CLOSED",
                    "closed_at": datetime.utcnow().isoformat(),
                }
                if parsed.get("drawdown_max"): update_data["drawdown_max"] = parsed["drawdown_max"]

                exit_price = parsed.get("price")
                if exit_price and trade_entry:
                    entry = float(trade_entry)
                    direction = trade_direction or parsed.get("direction", "")
                    if direction in ["BUY", "BUY_PUT"]:
                        pnl = round(exit_price - entry, 2)
                    else:
                        pnl = round(entry - exit_price, 2)
                    update_data["pnl"] = pnl
                    print(f"AUTO_TRADE PnL: entry={entry} exit={exit_price} dir={direction} pnl={pnl}")
                elif parsed.get("pnl"):
                    update_data["pnl"] = parsed["pnl"]

                supabase.table("trades").update(update_data).eq("id", trade_id).execute()
                print(f"AUTO_TRADE CLOSED: id={trade_id} symbol={symbol} pnl={update_data.get('pnl')}")
            else:
                print(f"AUTO_TRADE CLOSE: no open trade found for service={service_code}")

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

    # Solo gold, vanilla_monthly, vanilla_weekly sono gestiti da Telegram
    # Tutti gli altri servizi (indices, forex, fund_pamm, ecc.) sono gestiti esclusivamente da MT4
    if service_code not in ["gold", "vanilla_monthly", "vanilla_weekly"]:
        return {"ok": True}

    try:
        svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
        service_id = svc.data["id"]
    except:
        return {"ok": True}

    parsed = parse_signal(text)

    signal_id = None
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
        print(f"DB signal insert error (non bloccante): {e}")

    if signal_id:
        await notify_subscribers(service_id, signal_id, text, service_code)
    await auto_update_trades(service_id, service_code, parsed, text)
    return {"ok": True}

# ─────────────────────────────────────────────
# NOTIFICA SUBSCRIBERS
# ─────────────────────────────────────────────
async def notify_subscribers(service_id: int, signal_id: int, text: str, service_code: str):
    try:
        subs = supabase.table("subscriptions") \
            .select("client_id, clients(telegram_chat_id)") \
            .eq("service_id", service_id).eq("active", True).execute()
    except:
        return

    svc_names = {
        "indices":         "🌐 Sala Indici World",
        "vanilla_monthly": "📅 Vanilla Mensile",
        "vanilla_weekly":  "📆 Vanilla Settimanale",
        "forex":           "💱 Sala Forex",
        "fund_pamm":       "💼 Fondo PAAM",
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

# ─────────────────────────────────────────────
# PASSWORD HASHING  (PBKDF2-HMAC-SHA256, stdlib)
# ─────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key  = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return _b64.b64encode(salt + key).decode('ascii')

def verify_password(password: str, stored: str) -> bool:
    try:
        data = _b64.b64decode(stored.encode('ascii'))
        salt, key = data[:16], data[16:]
        new_key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
        return _hmac.compare_digest(key, new_key)
    except Exception:
        return False

# ─────────────────────────────────────────────
# ADMIN TOKEN  (HMAC-SHA256 stateless, 8h TTL)
# ─────────────────────────────────────────────
_TOKEN_TTL = 28800

def _token_sign(payload_b64: str) -> str:
    return _hmac.new(ADMIN_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()

def create_admin_token(admin_id: str, username: str, full_name: str) -> str:
    payload = _json.dumps({"id": admin_id, "u": username, "n": full_name,
                           "exp": int(_time.time()) + _TOKEN_TTL}, separators=(',', ':'))
    p64 = _b64.urlsafe_b64encode(payload.encode()).decode()
    return f"{p64}.{_token_sign(p64)}"

def decode_admin_token(token: str):
    try:
        p64, sig = token.rsplit(".", 1)
        if not _hmac.compare_digest(sig, _token_sign(p64)):
            return None
        data = _json.loads(_b64.urlsafe_b64decode(p64 + "==").decode())
        if data.get("exp", 0) < int(_time.time()):
            return None
        return data
    except Exception:
        return None

# ─────────────────────────────────────────────
# AUTH DEPENDENCIES
# ─────────────────────────────────────────────
def require_admin(x_admin_secret: str = Header(None), x_admin_token: str = Header(None)):
    """Accetta token utente admin OPPURE il secret legacy (usato dall'EA MT4)."""
    if x_admin_token:
        data = decode_admin_token(x_admin_token)
        if data:
            return {"id": data["id"], "username": data["u"], "full_name": data.get("n", "")}
    if x_admin_secret and x_admin_secret == ADMIN_SECRET:
        return {"id": "system", "username": "system", "full_name": "Sistema"}
    raise HTTPException(status_code=403, detail="Accesso negato")

def require_superadmin(x_superadmin_secret: str = Header(None)):
    if not SUPERADMIN_SECRET or x_superadmin_secret != SUPERADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Accesso superadmin negato")

def log_admin_action(action: str, description: str, target_email: str = None,
                     target_client_id: str = None, details: dict = None,
                     admin_username: str = None):
    """Scrive un record di audit nella tabella admin_logs (fire-and-forget)"""
    try:
        supabase.table("admin_logs").insert({
            "action":           action,
            "description":      description,
            "target_email":     target_email,
            "target_client_id": target_client_id,
            "details":          details or {},
            "admin_username":   admin_username or "unknown",
        }).execute()
    except Exception as e:
        print(f"[AUDIT LOG ERROR] {e}")

# ─────────────────────────────────────────────
# ADMIN LOGIN
# ─────────────────────────────────────────────
@app.post("/admin/login")
async def admin_login(data: dict):
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username e password obbligatori")
    res = supabase.table("admin_users").select("*").eq("username", username).eq("active", True).execute()
    if not res.data:
        raise HTTPException(status_code=401, detail="Credenziali non valide")
    user = res.data[0]
    if not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Credenziali non valide")
    token = create_admin_token(str(user["id"]), user["username"], user.get("full_name", ""))
    return {"ok": True, "token": token, "username": user["username"], "full_name": user.get("full_name", "")}

# ─────────────────────────────────────────────
# API CLIENTI
# ─────────────────────────────────────────────
@app.get("/api/signals")
async def get_signals(service_code: str = None, limit: int = 50, user=Depends(get_user)):
    subs = supabase.table("subscriptions") \
        .select("service_id, services(code)").eq("client_id", str(user.id)).eq("active", True).execute()
    allowed_codes = [s["services"]["code"] for s in subs.data]
    allowed_ids   = [s["service_id"] for s in subs.data]
    if service_code:
        if service_code not in allowed_codes:
            raise HTTPException(status_code=403, detail="Non abbonato")
        svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
        result = supabase.table("signals").select("*, services(code,name)") \
            .eq("service_id", svc.data["id"]).order("created_at", desc=True).limit(limit).execute()
    else:
        result = supabase.table("signals").select("*, services(code,name)") \
            .in_("service_id", allowed_ids).order("created_at", desc=True).limit(limit).execute()
    return result.data

@app.get("/api/trades")
async def get_trades(service_code: str = None, user=Depends(get_user)):
    subs = supabase.table("subscriptions") \
        .select("service_id, services(code)").eq("client_id", str(user.id)).eq("active", True).execute()
    allowed_ids   = [s["service_id"] for s in subs.data]
    allowed_codes = [s["services"]["code"] for s in subs.data]
    if service_code:
        if service_code not in allowed_codes:
            raise HTTPException(status_code=403, detail="Non abbonato")
        svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
        all_data = []
        offset = 0
        while True:
            batch = supabase.table("trades").select("*, services(code,name)") \
                .eq("service_id", svc.data["id"]).order("opened_at", desc=False) \
                .range(offset, offset+999).execute()
            if not batch.data: break
            all_data.extend(batch.data)
            if len(batch.data) < 1000: break
            offset += 1000
        class Result: data = all_data
        result = Result()
    else:
        result = supabase.table("trades").select("*, services(code,name)") \
            .in_("service_id", allowed_ids).order("opened_at", desc=True).execute()
    return result.data

@app.get("/api/services")
async def get_my_services(user=Depends(get_user)):
    return supabase.table("subscriptions") \
        .select("active, expires_at, amount, notes, services(code,name,description)") \
        .eq("client_id", str(user.id)).execute().data

@app.get("/api/profile")
async def get_profile(user=Depends(get_user)):
    return supabase.table("clients").select("id,full_name,email,active,created_at") \
        .eq("id", str(user.id)).single().execute().data

# ─────────────────────────────────────────────
# ADMIN API
# ─────────────────────────────────────────────
@app.post("/admin/client")
async def create_client(data: dict, admin=Depends(require_admin)):
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
    log_admin_action(
        action="CREATE_CLIENT",
        description=f"Nuovo cliente creato: {data['email']} ({data.get('full_name','')})",
        target_email=data["email"],
        target_client_id=str(uid),
        details={
            "full_name": data.get("full_name", ""),
            "services": data.get("services", []),
            "expires_at": data.get("expires_at"),
            "amount": data.get("amount"),
            "telegram_username": data.get("telegram_username"),
        },
        admin_username=admin["username"]
    )
    return {"ok": True, "client_id": str(uid)}

@app.get("/admin/clients", dependencies=[Depends(require_admin)])
async def list_clients():
    return supabase.table("clients") \
        .select("*, subscriptions(active, expires_at, amount, notes, services(name,code))").execute().data

@app.patch("/admin/subscription/{client_id}/{service_code}")
async def toggle_sub(client_id: str, service_code: str, active: bool, admin=Depends(require_admin)):
    svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
    service_id = svc.data["id"]
    existing = supabase.table("subscriptions").select("id") \
        .eq("client_id", client_id).eq("service_id", service_id).execute()
    if existing.data:
        supabase.table("subscriptions").update({"active": active}) \
            .eq("client_id", client_id).eq("service_id", service_id).execute()
    else:
        supabase.table("subscriptions").insert({
            "client_id": client_id,
            "service_id": service_id,
            "active": active,
        }).execute()
    stato = "ATTIVATO" if active else "DISATTIVATO"
    log_admin_action(
        action="TOGGLE_SUBSCRIPTION",
        description=f"Servizio '{service_code}' {stato} per cliente {client_id}",
        target_client_id=client_id,
        details={"service_code": service_code, "active": active},
        admin_username=admin["username"]
    )
    return {"ok": True}

@app.patch("/admin/subscription/{client_id}/{service_code}/renew")
async def renew_sub(client_id: str, service_code: str, data: dict, admin=Depends(require_admin)):
    svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
    update_data = {"active": True}
    if data.get("expires_at"): update_data["expires_at"] = data["expires_at"]
    if data.get("amount") is not None: update_data["amount"] = data["amount"]
    if data.get("notes") is not None: update_data["notes"] = data["notes"]
    supabase.table("subscriptions").update(update_data) \
        .eq("client_id", client_id).eq("service_id", svc.data["id"]).execute()
    log_admin_action(
        action="RENEW_SUBSCRIPTION",
        description=f"Abbonamento '{service_code}' rinnovato per cliente {client_id}",
        target_client_id=client_id,
        details={"service_code": service_code, **update_data},
        admin_username=admin["username"]
    )
    return {"ok": True}

@app.get("/admin/signals", dependencies=[Depends(require_admin)])
async def admin_signals(limit: int = 100):
    return supabase.table("signals").select("*, services(name)") \
        .order("created_at", desc=True).limit(limit).execute().data

@app.get("/api/quotes")
async def get_quotes():
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

@app.post("/admin/news")
async def create_news(data: dict, admin=Depends(require_admin)):
    msg = data["message"]
    supabase.table("news").insert({"message": msg, "active": True}).execute()
    preview = msg[:80] + ("..." if len(msg) > 80 else "")
    log_admin_action(
        action="CREATE_NEWS",
        description=f"Nuova news pubblicata: {preview}",
        details={"message": msg},
        admin_username=admin["username"]
    )
    return {"ok": True}

@app.delete("/admin/news/{news_id}")
async def delete_news(news_id: int, admin=Depends(require_admin)):
    supabase.table("news").update({"active": False}).eq("id", news_id).execute()
    log_admin_action(
        action="DELETE_NEWS",
        description=f"News id={news_id} disattivata",
        details={"news_id": news_id},
        admin_username=admin["username"]
    )
    return {"ok": True}

# ─────────────────────────────────────────────
# MT4 TRADE ENDPOINT
# FIX: rimosso .like() che causava Cloudflare 1101
# Il filtro per ticket viene fatto in Python con startswith()
# ─────────────────────────────────────────────
@app.post("/mt4/trade")
async def mt4_trade(request: Request, x_admin_secret: str = Header(None)):
    """Riceve operazioni da EA MT4"""
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Accesso negato")
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="JSON non valido")

    action       = data.get("action")
    service_code = data.get("service_code", "fund_pamm")
    ticket       = data.get("ticket")
    symbol       = data.get("symbol", "")
    direction    = data.get("direction")
    ticket_note  = f"MT4-{ticket}"

    print(f"MT4 incoming: action={action} service={service_code} ticket={ticket} sym={symbol} dir={direction}")

    try:
        svc_res = supabase.table("services").select("id").eq("code", service_code).execute()
        if not svc_res.data:
            raise HTTPException(status_code=404, detail=f"Servizio {service_code} non trovato nel DB")
        service_id = svc_res.data[0]["id"]
        print(f"MT4 service_id={service_id} per code={service_code}")
    except HTTPException:
        raise
    except Exception as e:
        print(f"MT4 service lookup error: {e}")
        raise HTTPException(status_code=500, detail=f"Service lookup error: {str(e)}")

    if action == "OPEN":
        price     = data.get("price", 0)
        # open_time dall'EA è ora broker (UTC+2/+3) — usiamo utcnow() per coerenza col resto del DB
        try:
            price_val = round(float(price), 5) if price else None
        except:
            price_val = None

        # Fetch tutti i trades aperti del servizio e filtra in Python
        # (evita .like() che causa Cloudflare 1101)
        try:
            all_open = supabase.table("trades").select("id,notes") \
                .eq("service_id", service_id).eq("status", "OPEN").execute()
            already = any(
                (t.get("notes") or "").startswith(ticket_note)
                for t in (all_open.data or [])
            )
        except Exception as e:
            print(f"MT4 duplicate check warn: {e}")
            already = False

        if not already:
            try:
                supabase.table("trades").insert({
                    "service_id": service_id,
                    "direction":  direction,
                    "strike":     price_val,
                    "status":     "OPEN",
                    "opened_at":  datetime.utcnow().isoformat(),
                    "notes":      f"{ticket_note} {symbol}",
                }).execute()
                print(f"MT4 OPEN OK: {symbol} {direction} @ {price_val} ticket={ticket}")
            except Exception as e:
                print(f"MT4 INSERT ERROR: {e}")
                raise HTTPException(status_code=500, detail=f"DB insert error: {str(e)}")

            try:
                supabase.table("signals").insert({
                    "service_id":   service_id,
                    "message_text": f"Alert for {symbol} {direction} Apertura: {price_val}",
                    "signal_type":  "OPEN",
                    "direction":    direction,
                    "strike":       price_val,
                }).execute()
            except Exception as e:
                print(f"MT4 signal warn: {e}")
        else:
            print(f"MT4 OPEN: ticket={ticket} già presente in DB, skip")

    elif action == "CLOSE":
        close_price = data.get("close_price", 0)
        pnl         = data.get("pnl", 0)
        # close_time dall'EA è ora broker — usiamo utcnow() per coerenza
        try:
            pnl_val   = round(float(pnl), 2) if pnl is not None else 0
            close_val = round(float(close_price), 5) if close_price else None
        except:
            pnl_val = 0; close_val = None

        # Idempotency: se il trade esiste già come CLOSED, rispondi OK e non fare nulla
        try:
            all_trades = supabase.table("trades").select("id,notes,status") \
                .eq("service_id", service_id).execute()
            already_closed = any(
                (t.get("notes") or "").startswith(ticket_note) and t.get("status") == "CLOSED"
                for t in (all_trades.data or [])
            )
            if already_closed:
                print(f"MT4 CLOSE: ticket={ticket} già CLOSED in DB, skip (idempotent)")
                return {"ok": True, "action": action, "ticket": ticket}
        except Exception as e:
            print(f"MT4 idempotency check warn: {e}")

        # Fetch tutti i trades aperti e filtra in Python
        try:
            all_open = supabase.table("trades").select("id,notes") \
                .eq("service_id", service_id).eq("status", "OPEN").execute()
            matching = [
                t for t in (all_open.data or [])
                if (t.get("notes") or "").startswith(ticket_note)
            ]
        except Exception as e:
            print(f"MT4 CLOSE search error: {e}")
            matching = []

        if matching:
            trade_id = matching[0]["id"]
            try:
                supabase.table("trades").update({
                    "status":    "CLOSED",
                    "closed_at": datetime.utcnow().isoformat(),
                    "pnl":       pnl_val,
                }).eq("id", trade_id).execute()
                print(f"MT4 CLOSE OK: ticket={ticket} PnL={pnl_val}")
            except Exception as e:
                print(f"MT4 UPDATE ERROR: {e}")
                raise HTTPException(status_code=500, detail=f"DB update error: {str(e)}")

            try:
                supabase.table("signals").insert({
                    "service_id":   service_id,
                    "message_text": f"Alert for {symbol} Chiusura {direction} Uscita: {close_val}",
                    "signal_type":  "CLOSE",
                    "direction":    direction,
                    "strike":       close_val,
                    "pnl":          pnl_val,
                }).execute()
            except Exception as e:
                print(f"MT4 signal close warn: {e}")
        else:
            print(f"MT4 CLOSE: ticket={ticket} not found in DB (notes prefix: {ticket_note})")

    return {"ok": True, "action": action, "ticket": ticket}

@app.get("/api/fund_movements")
async def get_fund_movements(service_code: str = None, user=Depends(get_user)):
    """Movimenti di capitale — solo deposit/withdrawal per i clienti (no fee)"""
    subs = supabase.table("subscriptions") \
        .select("service_id, services(code)").eq("client_id", str(user.id)).eq("active", True).execute()
    allowed_codes = [s["services"]["code"] for s in subs.data]
    if service_code and service_code not in allowed_codes:
        raise HTTPException(status_code=403, detail="Non abbonato")
    if service_code:
        svc = supabase.table("services").select("id").eq("code", service_code).execute()
        if not svc.data:
            raise HTTPException(status_code=404, detail="Servizio non trovato")
        service_id = svc.data[0]["id"]
        result = supabase.table("fund_movements") \
            .select("*").eq("service_id", service_id) \
            .in_("type", ["deposit","withdrawal","adjustment","balance_snapshot"]) \
            .order("moved_at", desc=False).execute()
    else:
        allowed_ids = [s["service_id"] for s in subs.data]
        result = supabase.table("fund_movements") \
            .select("*").in_("service_id", allowed_ids) \
            .in_("type", ["deposit","withdrawal","adjustment","balance_snapshot"]) \
            .order("moved_at", desc=False).execute()
    return result.data

@app.get("/admin/fund_movements", dependencies=[Depends(require_admin)])
async def admin_fund_movements(service_id: int = None):
    """Tutti i movimenti incluse fee — solo admin"""
    q = supabase.table("fund_movements").select("*")
    if service_id:
        q = q.eq("service_id", service_id)
    return q.order("moved_at", desc=False).execute().data

@app.post("/admin/fund_movements")
async def create_fund_movement(data: dict, admin=Depends(require_admin)):
    amount_val = round(float(data["amount"]), 2)
    mov_type   = data["type"]
    supabase.table("fund_movements").insert({
        "service_id": data["service_id"],
        "amount":     amount_val,
        "moved_at":   data.get("moved_at", datetime.utcnow().isoformat()),
        "type":       mov_type,
        "notes":      data.get("notes", ""),
    }).execute()
    log_admin_action(
        action="CREATE_FUND_MOVEMENT",
        description=f"Movimento fondo inserito: tipo={mov_type}, importo={amount_val}EUR, service_id={data['service_id']}",
        details={
            "service_id": data["service_id"],
            "amount": amount_val,
            "type": mov_type,
            "notes": data.get("notes", ""),
            "moved_at": data.get("moved_at"),
        },
        admin_username=admin["username"]
    )
    return {"ok": True}

@app.delete("/admin/fund_movements/{movement_id}")
async def delete_fund_movement(movement_id: int, admin=Depends(require_admin)):
    supabase.table("fund_movements").delete().eq("id", movement_id).execute()
    log_admin_action(
        action="DELETE_FUND_MOVEMENT",
        description=f"Movimento fondo eliminato: id={movement_id}",
        details={"movement_id": movement_id},
        admin_username=admin["username"]
    )
    return {"ok": True}


@app.post("/mt4/balance")
async def mt4_balance(request: Request, x_admin_secret: str = Header(None)):
    """Riceve il saldo reale del conto MT4 dall'EA e lo salva come balance_snapshot"""
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Accesso negato")
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="JSON non valido")

    balance      = data.get("balance")
    service_code = data.get("service_code", "fund_pamm")

    if balance is None:
        raise HTTPException(status_code=400, detail="Campo 'balance' mancante")

    try:
        balance_val = round(float(balance), 2)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Valore 'balance' non valido")

    svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
    if not svc.data:
        raise HTTPException(status_code=404, detail=f"Servizio '{service_code}' non trovato")

    supabase.table("fund_movements").insert({
        "service_id": svc.data["id"],
        "amount":     balance_val,
        "moved_at":   datetime.utcnow().isoformat(),
        "type":       "balance_snapshot",
        "notes":      f"Saldo MT4 aggiornato automaticamente dall'EA",
    }).execute()

    return {"ok": True, "balance": balance_val}


@app.get("/superadmin/admin_users", dependencies=[Depends(require_superadmin)])
async def list_admin_users():
    return supabase.table("admin_users").select("id,username,full_name,active,created_at") \
        .order("created_at", desc=False).execute().data

@app.post("/superadmin/admin_users", dependencies=[Depends(require_superadmin)])
async def create_admin_user(data: dict):
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    full_name = data.get("full_name", "").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username e password obbligatori")
    existing = supabase.table("admin_users").select("id").eq("username", username).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail="Username già esistente")
    supabase.table("admin_users").insert({
        "username":      username,
        "full_name":     full_name,
        "password_hash": hash_password(password),
        "active":        True,
    }).execute()
    return {"ok": True, "username": username}

@app.patch("/superadmin/admin_users/{admin_id}", dependencies=[Depends(require_superadmin)])
async def toggle_admin_user(admin_id: str, data: dict):
    supabase.table("admin_users").update({"active": data["active"]}).eq("id", admin_id).execute()
    return {"ok": True}

@app.patch("/superadmin/admin_users/{admin_id}/password", dependencies=[Depends(require_superadmin)])
async def reset_admin_password(admin_id: str, data: dict):
    password = data.get("password") or ""
    if not password:
        raise HTTPException(status_code=400, detail="Password obbligatoria")
    supabase.table("admin_users").update({"password_hash": hash_password(password)}).eq("id", admin_id).execute()
    return {"ok": True}

@app.get("/superadmin/logs", dependencies=[Depends(require_superadmin)])
async def superadmin_logs(limit: int = 200, offset: int = 0):
    """Giornale audit — solo superadmin"""
    result = supabase.table("admin_logs") \
        .select("*") \
        .order("created_at", desc=True) \
        .range(offset, offset + limit - 1) \
        .execute()
    return result.data

@app.head("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
