"""
FINANZALIVE — Backend FastAPI v2
Webhook Telegram + API dashboard clienti
Tutti i 5 canali configurati
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

app = FastAPI(title="Finanzalive API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL        = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY= os.environ["SUPABASE_SERVICE_KEY"]
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_API        = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
ADMIN_SECRET        = os.environ["ADMIN_SECRET"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Tutti e 5 i canali mappati
CHANNEL_SERVICE_MAP = {
    -1002552300319: "indices",          # Sala Indici World
    -1002517239703: "vanilla_monthly",  # Opzioni Vanilla Mensile
    -1002870950901: "vanilla_weekly",   # Opzioni Vanilla Settimanale
    -1002850487439: "forex",            # Sala Forex
    # fund_paam: nessun canale dedicato per ora
}

def parse_signal(text: str) -> dict:
    text = text.replace('\n', ' ').replace('\r', ' ')
    data = {
        "signal_type": "INFO", "direction": None, "strike": None,
        "strike_pct": None, "premium": None, "drawdown_max": None,
        "pnl": None, "symbol": None, "price": None, "tp": None
    }
    t = text.upper()

    # Simboli indici validi
    INDEX_SYMBOLS = ["US100", "US30", "DE40", "JPN225"]

    # ── VANILLA MENSILE ingresso ──
    # "Alert for US500 SELL PUT su US500 con strike: 5952.06 (Spot: 6649.2) Scadenza: Mensile"
    if "STRIKE:" in t and ("SELL PUT" in t or "BUY PUT" in t or "SCADENZA" in t or "SELL_PUT" in t):
        data["signal_type"] = "OPEN"
        data["direction"] = "SELL_PUT" if ("SELL PUT" in t or "SELL_PUT" in t) else "BUY_PUT"
        m = re.search(r'STRIKE:\s*([\d,\.]+)', t)
        if m: data["strike"] = float(m.group(1).replace(",",""))
        m_spot = re.search(r'SPOT:\s*([\d,\.]+)', t)
        if m and m_spot:
            strike = float(m.group(1).replace(",",""))
            spot   = float(m_spot.group(1).replace(",",""))
            if spot > 0:
                data["strike_pct"] = round(abs(spot - strike) / spot * 100, 2)
                data["premium"]    = round(data["strike_pct"] * 100, 2)
        return data

    # ── VANILLA MENSILE chiusura ──
    # "CHIUSURA PUTSELL Strike: 5952.06 - Max DD: 2.45% - CHIUSURA IN PROFITTO"
    if "CHIUSURA PUTSELL" in t or "CHIUSURA PUTBUY" in t:
        data["signal_type"] = "CLOSE"
        data["direction"] = "SELL_PUT" if "PUTSELL" in t else "BUY_PUT"
        m = re.search(r'STRIKE:\s*([\d,\.]+)', t)
        if m: data["strike"] = float(m.group(1).replace(",",""))
        m = re.search(r'MAX DD:\s*([\d\.]+)', t)
        if m: data["drawdown_max"] = float(m.group(1))
        data["pnl"] = 1 if "PROFITTO" in t else -1
        return data

    # ── INDICI WORLD chiusura ──
    # "Alert for US100CHIUSURA SELL US100" / "Alert for DE40CHIUSURA SELL DE40"
    for sym in INDEX_SYMBOLS:
        if sym+"CHIUSURA" in t or (sym in t and "CHIUSURA" in t):
            data["signal_type"] = "CLOSE"
            data["symbol"] = sym
            m_dir = re.search(r'CHIUSURA\s+(BUY|SELL)', t)
            if m_dir: data["direction"] = m_dir.group(1)
            return data

    # ── INDICI WORLD ingresso ──
    # "Alert for US100SELL SCALPING US100 Apertura: 24,525.80"
    for sym in INDEX_SYMBOLS:
        if sym in t and "SCALPING" in t and "APERTURA" in t:
            data["signal_type"] = "OPEN"
            data["symbol"] = sym
            m_dir = re.search(sym + r'\s*(BUY|SELL)', t)
            if not m_dir: m_dir = re.search(r'ALERT FOR '+sym+r'(BUY|SELL)', t)
            if m_dir: data["direction"] = m_dir.group(1)
            m_price = re.search(r'APERTURA:\s*([\d,\.]+)', t)
            if m_price: data["price"] = float(m_price.group(1).replace(",",""))
            return data

    # ── FOREX chiusura ──
    # "Alert for AUDCHFCLOSE BUY AUDCHF.ecn"
    m = re.search(r'ALERT FOR ([A-Z]{6})CLOSE\s+(BUY|SELL)', t)
    if m:
        data["signal_type"] = "CLOSE"
        data["symbol"]    = m.group(1)
        data["direction"] = m.group(2)
        return data

    # ── FOREX ingresso ──
    # "Alert for AUDCHF BUY AUDCHF.ecn a mercato TP: .55327"
    m = re.search(r'ALERT FOR ([A-Z]{6})\s+(BUY|SELL)', t)
    if m and "CHIUSURA" not in t and "SCALPING" not in t:
        data["signal_type"] = "OPEN"
        data["symbol"]    = m.group(1)
        data["direction"] = m.group(2)
        m_tp = re.search(r'TP:\s*([\d\.]+)', t)
        if m_tp: data["tp"] = float(m_tp.group(1))
        return data

    # ── FALLBACK ──
    if any(w in t for w in ["APERTURA","OPEN","ENTRY"]):
        data["signal_type"] = "OPEN"
    elif any(w in t for w in ["CHIUSURA","CLOSE","EXIT"]):
        data["signal_type"] = "CLOSE"
    elif any(w in t for w in ["TRIGGER","ALERT","WARNING"]):
        data["signal_type"] = "ALERT"

    return data


async def auto_update_trades(service_id: int, service_code: str, parsed: dict, text: str):
    """Crea/aggiorna trades automaticamente per vanilla, forex e indici"""
    if service_code not in ["vanilla_monthly", "forex", "indices"]:
        return
    try:
        if parsed["signal_type"] == "OPEN":
            insert_data = {
                "service_id": service_id,
                "direction":  parsed.get("direction"),
                "status":     "OPEN",
                "opened_at":  datetime.utcnow().isoformat(),
                "notes":      f"Auto - {parsed.get('symbol', service_code)} {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}",
            }
            if parsed.get("strike"):     insert_data["strike"]            = parsed["strike"]
            if parsed.get("price"):      insert_data["strike"]            = parsed["price"]
            if parsed.get("strike_pct"): insert_data["strike_pct"]        = parsed["strike_pct"]
            if parsed.get("premium"):    insert_data["premium_collected"]  = parsed["premium"]
            supabase.table("trades").insert(insert_data).execute()

        elif parsed["signal_type"] == "CLOSE":
            # Trova ultima operazione OPEN per questo servizio
            q = supabase.table("trades").select("id")                .eq("service_id", service_id)                .eq("status", "OPEN")                .order("opened_at", desc=True).limit(1).execute()
            if q.data:
                update_data = {
                    "status":    "CLOSED",
                    "closed_at": datetime.utcnow().isoformat(),
                }
                if parsed.get("drawdown_max"): update_data["drawdown_max"] = parsed["drawdown_max"]
                supabase.table("trades").update(update_data)                    .eq("id", q.data[0]["id"]).execute()
    except Exception as e:
        print(f"Auto trade error: {e}")


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

@app.get("/api/signals")
async def get_signals(service_code: str = None, limit: int = 50, user=Depends(get_user)):
    subs = supabase.table("subscriptions")\
        .select("service_id, services(code)").eq("client_id", str(user.id)).eq("active", True).execute()
    allowed_codes = [s["services"]["code"] for s in subs.data]
    allowed_ids   = [s["service_id"] for s in subs.data]

    if service_code:
        if service_code not in allowed_codes:
            raise HTTPException(status_code=403, detail="Non abbonato")
        svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
        result = supabase.table("signals").select("*, services(code,name)")\
            .eq("service_id", svc.data["id"]).order("created_at", desc=True).limit(limit).execute()
    else:
        result = supabase.table("signals").select("*, services(code,name)")\
            .in_("service_id", allowed_ids).order("created_at", desc=True).limit(limit).execute()
    return result.data

@app.get("/api/trades")
async def get_trades(service_code: str = None, user=Depends(get_user)):
    subs = supabase.table("subscriptions")\
        .select("service_id, services(code)").eq("client_id", str(user.id)).eq("active", True).execute()
    allowed_ids   = [s["service_id"] for s in subs.data]
    allowed_codes = [s["services"]["code"] for s in subs.data]

    if service_code:
        if service_code not in allowed_codes:
            raise HTTPException(status_code=403, detail="Non abbonato")
        svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
        result = supabase.table("trades").select("*, services(code,name)")\
            .eq("service_id", svc.data["id"]).order("opened_at", desc=True).execute()
    else:
        result = supabase.table("trades").select("*, services(code,name)")\
            .in_("service_id", allowed_ids).order("opened_at", desc=True).execute()
    return result.data

@app.get("/api/services")
async def get_my_services(user=Depends(get_user)):
    return supabase.table("subscriptions")\
        .select("active, expires_at, services(code,name,description)")\
        .eq("client_id", str(user.id)).execute().data

@app.get("/api/profile")
async def get_profile(user=Depends(get_user)):
    return supabase.table("clients").select("id,full_name,email,active,created_at")\
        .eq("id", str(user.id)).single().execute().data

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
            "client_id": uid, "service_id": svc.data["id"], "expires_at": data.get("expires_at"),
            "amount": data.get("amount"), "notes": data.get("notes"),
        }).execute()
    return {"ok": True, "client_id": str(uid)}

@app.get("/admin/clients", dependencies=[Depends(require_admin)])
async def list_clients():
    return supabase.table("clients")\
        .select("*, subscriptions(active, services(name,code))").execute().data

@app.patch("/admin/subscription/{client_id}/{service_code}", dependencies=[Depends(require_admin)])
async def toggle_sub(client_id: str, service_code: str, active: bool):
    svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
    supabase.table("subscriptions").update({"active": active})\
        .eq("client_id", client_id).eq("service_id", svc.data["id"]).execute()
    return {"ok": True}

@app.get("/admin/signals", dependencies=[Depends(require_admin)])
async def admin_signals(limit: int = 100):
    return supabase.table("signals").select("*, services(name)")\
        .order("created_at", desc=True).limit(limit).execute().data


@app.patch("/admin/subscription/{client_id}/{service_code}/renew", dependencies=[Depends(require_admin)])
async def renew_sub(client_id: str, service_code: str, data: dict):
    svc = supabase.table("services").select("id").eq("code", service_code).single().execute()
    update_data = {"active": True}
    if data.get("expires_at"): update_data["expires_at"] = data["expires_at"]
    if data.get("amount") is not None: update_data["amount"] = data["amount"]
    if data.get("notes") is not None: update_data["notes"] = data["notes"]
    supabase.table("subscriptions").update(update_data)        .eq("client_id", client_id).eq("service_id", svc.data["id"]).execute()
    return {"ok": True}

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
