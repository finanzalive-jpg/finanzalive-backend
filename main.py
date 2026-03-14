"""
IUPPITER — Backend FastAPI v2
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
    data = {"signal_type": "INFO", "direction": None, "strike": None,
            "strike_pct": None, "premium": None, "drawdown_max": None, "pnl": None}
    t = text.upper()

    if any(w in t for w in ["APERTURA","SELL_PUT","BUY_PUT","OPEN","ENTRY","LONG","BUY"]):
        data["signal_type"] = "OPEN"
        if "SELL_PUT" in t: data["direction"] = "SELL_PUT"
        elif "BUY_PUT" in t: data["direction"] = "BUY_PUT"
        elif "SELL" in t or "SHORT" in t: data["direction"] = "SELL"
        elif "BUY" in t or "LONG" in t: data["direction"] = "BUY"
        m = re.search(r'STRIKE[^\d]*(\d+\.?\d*)', t)
        if m: data["strike"] = float(m.group(1))
        m = re.search(r'\((\d+\.?\d*)\s*%\)', text)
        if m:
            data["strike_pct"] = float(m.group(1))
            data["premium"] = round(data["strike_pct"] * 100, 2)

    elif any(w in t for w in ["CHIUSURA","PROFITTO","CLOSE","EXIT","PROFIT","TARGET","TP","SL"]):
        data["signal_type"] = "CLOSE"
        m = re.search(r'DRAWDOWN[^\d]*(\d+\.?\d*)', t)
        if m: data["drawdown_max"] = float(m.group(1))
        m = re.search(r'[+\-](\d+\.?\d*)', text)
        if m: data["pnl"] = float(m.group(1))

    elif any(w in t for w in ["TRIGGER","COPERTURA","ALERT","WARNING","ATTENZIONE"]):
        data["signal_type"] = "ALERT"

    return data

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
    return {"ok": True}

async def notify_subscribers(service_id, signal_id, text, service_code):
    try:
        subs = supabase.table("subscriptions")\
            .select("client_id, clients(telegram_chat_id)")\
            .eq("service_id", service_id).eq("active", True).execute()
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

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
