# ----------------------- IMPORTS ----------------------- #
from fastapi import FastAPI, Request, Form, UploadFile, File, Depends, HTTPException, Body
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from logic import send_whatsapp_message, process_incoming_incident
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta, timezone
from fastapi.responses import JSONResponse
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, Field
from typing import List, Optional
from passlib.hash import argon2
from dotenv import load_dotenv
from whop import create_usage_overage_invoice
import jwt as pyjwt
import traceback
import psycopg2
import asyncio
import httpx
import json
import re
import math
import os
import sys

# Force UTF-8 encoding for standard output and error on Windows
if sys.platform.startswith("win"):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass



# ----------------------- CONFIG ----------------------- #
load_dotenv()

app = FastAPI(title="Coherzo")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://cringing-niece-playpen.ngrok-free.dev",
        "http://localhost:8080",
        "http://localhost:5173",
        "http://localhost:3000"
        # Optional: Use "*" to allow all origins during development
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"], # Allows all headers (including ngrok-skip-browser-warning)
)

# Mount static files for uploads
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/tmp/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
from fastapi.staticfiles import StaticFiles
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# here you would set Wbot api url and your number from environment variables or defaults
WBOT_API_URL = os.getenv("WBOT_API_URL", "http://localhost:3001").rstrip("/")
PLUMBER_NUMBER = os.getenv("PLUMBER_WHATSAPP_NUMBER")
RETELL_API_KEY = os.getenv("RETELL_API_KEY")
RETELL_AGENT_ID = os.getenv("RETELL_AGENT_ID")
RETELL_FROM_NUMBER = os.getenv("RETELL_FROM_NUMBER")

PLAN_MINUTE_SETTINGS = {
    "starter": {"included_minutes": 60, "overage_per_minute_usd": 0.25},
    "pro": {"included_minutes": 300, "overage_per_minute_usd": 0.20},
    "business": {"included_minutes": 1000, "overage_per_minute_usd": 0.15},
    "default": {"included_minutes": 0, "overage_per_minute_usd": 0.25},
}


# -------------------- PYDANTIC MODELS ----------------------
class IncidentJsonPayload(BaseModel):
    phone: str
    description: str
    location: Optional[str] = None
    customer_name: Optional[str] = None
    contractor_id: Optional[str] = None
    demo: Optional[str] = None
    professional_type: Optional[str] = "contractor"
    language: Optional[str] = "en"


# ----------------------- DATABASE ----------------------- #
def get_db_connection():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL environment variable is missing")
    try:
        conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database connection failed: {str(e)}")


# 🔐 Password hashing helpers
def pqc_hash_password(password: str) -> str:
    """
    Hash a password using argon2. Function name kept as pqc_hash_password
    to avoid touching call sites elsewhere in the codebase.
    """
    return argon2.hash(password)

def pqc_verify_password(password: str, stored_hash: str) -> bool:
    """
    Verify a password against an argon2 hash. Function name kept as
    pqc_verify_password to avoid touching call sites elsewhere.
    """
    try:
        return argon2.verify(password, stored_hash)
    except Exception:
        return False


# ----------------------- ENDPOINTS ----------------------- #
# health check 
@app.get("/")
async def root():
    print("DEBUG: Root health check hit!")
    return {"status": "running", "service": "Coherzo"}

# webhook endpoint, accepts slashes at the end
@app.post("/webhook")
@app.post("/webhook/") 
async def whatsapp_webhook(request: Request):
    print(f"\n=================== WEBHOOK INBOUND ===================")
    print(f"DEBUG: Method: {request.method} | URL: {request.url}")
    
    try:
        # Diagnostic check on incoming content types
        content_type = request.headers.get("content-type", "")
        print(f"DEBUG: Content-Type Header: {content_type}")
        
        customer_phone = None
        body_raw = ""
        form_data = None
        wbot_url = None
        from_jid = None  # raw JID (e.g. 15015860002951@lid) for reply routing

        # Robust multi-format parser with explicit debugging
        if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            form_data = await request.form()
            print(f"DEBUG: Form data keys found: {list(form_data.keys())}")
            customer_phone = form_data.get("From")
            body_raw = form_data.get("Body", "").strip()
            wbot_url = form_data.get("WbotUrl")
            from_jid = form_data.get("FromJid")
        else:
            try:
                json_data = await request.json()
                print(f"DEBUG: JSON payload found: {json_data}")
                customer_phone = json_data.get("From")
                body_raw = json_data.get("Body", "").strip()
                wbot_url = json_data.get("WbotUrl")
                from_jid = json_data.get("FromJid")
            except Exception as json_err:
                print(f"DEBUG: Failed parsing as JSON: {str(json_err)}")

        if not customer_phone:
            print("❌ ERROR: Request received but no sender phone ('From') could be resolved.")
            return JSONResponse({"status": "error", "message": "No sender phone found"}, status_code=400)

        # Deduplicate incoming webhook events to avoid processing retries or duplicate forwards
        # Keyed by sender + normalized body
        if not hasattr(app.state, 'recent_inbound'):
            app.state.recent_inbound = {}
        fingerprint = f"{customer_phone}||{body_raw}"
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        # cleanup old entries
        cutoff = now_ts - 60  # 60s window
        keys_to_delete = [k for k, v in app.state.recent_inbound.items() if v < cutoff]
        for k in keys_to_delete:
            del app.state.recent_inbound[k]
        if fingerprint in app.state.recent_inbound:
            print(f"⏳ Duplicate inbound webhook ignored for {customer_phone}")
            return JSONResponse({"status": "ignored", "reason": "duplicate"})
        app.state.recent_inbound[fingerprint] = now_ts

        # Ignore messages originating from internal/operator numbers (so operator
        # replies don't get sent to the AI or re-processed). Configure a
        # comma-separated list via `INTERNAL_WHATSAPP_NUMBERS` env var.
        def _normalize_phone(n):
            if not n:
                return ""
            s = str(n)
            for ch in ["whatsapp:", "+", " ", "-", "(", ")"]:
                s = s.replace(ch, "")
            return "".join([c for c in s if c.isdigit()])

        internal_cfg = os.getenv("INTERNAL_WHATSAPP_NUMBERS", "").split(",") if os.getenv("INTERNAL_WHATSAPP_NUMBERS") else []
        internal_cfg = [x.strip() for x in internal_cfg if x and x.strip()]
        if PLUMBER_NUMBER:
            internal_cfg.append(PLUMBER_NUMBER)

        norm_sender = _normalize_phone(customer_phone)
        norm_internals = [_normalize_phone(x) for x in internal_cfg]
        if norm_sender and norm_sender in norm_internals:
            print(f"🔒 Ignoring message from internal sender: {customer_phone}")
            return JSONResponse({"status": "ignored", "reason": "internal_sender"}, status_code=200)
        # Also support ignoring by raw JID (e.g. 15015860002951@lid) through
        # INTERNAL_WHATSAPP_JIDS env var. This helps when messages arrive with a
        # linked-device jid that better identifies operator devices.
        internal_jids = os.getenv("INTERNAL_WHATSAPP_JIDS", "").split(",") if os.getenv("INTERNAL_WHATSAPP_JIDS") else []
        internal_jids = [x.strip() for x in internal_jids if x and x.strip()]
        if from_jid and from_jid in internal_jids:
            print(f"🔒 Ignoring message from internal JID: {from_jid}")
            return JSONResponse({"status": "ignored", "reason": "internal_jid"}, status_code=200)
        # Process asynchronously to ack immediately and avoid caller retries
        async def handle_incoming():
            try:
                from logic import process_incoming_incident

                print(f"DEBUG (bg): from_jid={from_jid}")


                # 1. Handle Commands (Filtering in WhatsApp Chat)
                filter_keywords = ["URGENT", "NOT_URGENT", "ALL_TASKS", "EMERGENCY", "NON_EMERGENCY", "NO_EMERGENCY", "FILTER", "MID", "ALL"]
                body_upper = body_raw.upper().replace(" ", "_").strip()
                if body_upper in filter_keywords:
                    from database import get_incidents
                    incidents = get_incidents()
                    if body_upper in ["URGENT", "EMERGENCY"]:
                        filtered = [i for i in incidents if i['urgency'] == "HIGH"][:5]
                        title = "*🚨 Recent Urgent Tasks*"
                    elif body_upper in ["NOT_URGENT", "NON_EMERGENCY", "NO_EMERGENCY"]:
                        filtered = [i for i in incidents if i['urgency'] != "HIGH"][:5]
                        title = "*✅ Non-Urgent Tasks*"
                    else:
                        filtered = incidents[:5]
                        title = "*📋 All Recent Tasks*"

                    if not filtered:
                        msg_text = f"{title}\nNo tasks found."
                    else:
                        msg_text = f"{title}\n\n"
                        for i in filtered:
                            time_str = (
                                i['timestamp'].strftime("%H:%M")
                                if hasattr(i['timestamp'], 'strftime')
                                else str(i['timestamp'])[:5]
                            )
                            msg_text += f"• [{i['urgency']}] {i['summary']}\n  Phone: {i['customer_phone']}\n\n"

                    await send_whatsapp_message(
                        to=customer_phone,
                        payload_type="text",
                        content={
                            "body": msg_text,
                            "buttons": ["Emergency", "No Emergency", "All"]
                        }
                    )
                    return

                # 2. Handle New Incidents
                triage_result, _ = await process_incoming_incident(
                    customer_phone, body_raw, None, None, None, None
                )
                # done
            except Exception as bg_err:
                print('Background processing error:', bg_err)

        asyncio.create_task(handle_incoming())
        # Immediately acknowledge to the caller to avoid retries
        return JSONResponse({"status": "accepted"}, status_code=200)
            
        

    except Exception as global_err:
        print(f"❌ CRITICAL WEBHOOK EXCEPTION CRASH:")
        print("".join(traceback.format_exception(type(global_err), global_err, global_err.__traceback__)))
        sys.stdout.flush() # Force log buffer write immediately into Render's stream
        return JSONResponse({"status": "internal_error", "detail": str(global_err)}, status_code=500)


# here we handle new incidents
@app.post("/api/incident")
async def api_incident(request: Request):
    print(f"\n=================== /API/INCIDENT ===================")

    raw_ct = request.headers.get("content-type", "")
    content_type = raw_ct.lower()
    print(f"[DEBUG] Content-Type raw: '{raw_ct}'")

    data = {}
    phone = None
    description = None
    location = None
    customer_name = None
    contractor_id = ""
    demo = None
    professional_type = "contractor"
    language = "en"
    image_bytes = None  # always defined; populated only when a file upload is present

    # ─── Fallback: Extract phone from location or description if missing ───
    if not phone and location:
        import re
        phone_match = re.search(r"(\d{3}[-\.\s]?\d{3}[-\.\s]?\d{4}|\d{7,10})", location)
        if phone_match:
            phone = phone_match.group(0)

    if not phone and description:
        import re
        phone_match = re.search(r"(\d{3}[-\.\s]?\d{3}[-\.\s]?\d{4}|\d{7,10})", description)
        if phone_match:
            phone = phone_match.group(0)

    # ─── JSON branch ───
    if "application/json" in content_type:
        print("[INFO] Routing to JSON parser")
        body_bytes = await request.body()
        print(f"[DEBUG] Raw body bytes length: {len(body_bytes)}")

        if not body_bytes:
            print("[ERROR] JSON body is empty")
            return JSONResponse({"status": "error", "detail": "Empty JSON body"}, status_code=400)

        # Explicit decode with fallback
        try:
            body_str = body_bytes.decode("utf-8")
        except UnicodeDecodeError:
            body_str = body_bytes.decode("utf-8", errors="replace")

        print(f"[DEBUG] Decoded body: {body_str[:2000]}")

        try:
            data = json.loads(body_str)
        except json.JSONDecodeError as e:
            print(f"[ERROR] JSON parse failed: {e}")
            return JSONResponse({"status": "error", "detail": f"Invalid JSON: {e}"}, status_code=400)

        if not isinstance(data, dict):
            print(f"[ERROR] JSON root is {type(data).__name__}, expected object")
            return JSONResponse({"status": "error", "detail": "JSON must be an object"}, status_code=400)

        phone = data.get("phone")
        description = data.get("description")
        location = data.get("location")
        customer_name = data.get("customer_name")
        contractor_id = data.get("contractor_id") or data.get("plumber_id") or ""
        demo = data.get("demo")
        professional_type = data.get("professional_type") or "contractor"
        language = data.get("language") or "en"
        print(f"[INFO] JSON parsed OK")

    # ─── Form branch ───
    elif "multipart" in content_type or "urlencoded" in content_type:
        print("[INFO] Routing to Form parser")
        try:
            form = await request.form()
        except Exception as e:
            print(f"[ERROR] FastAPI form parse failed: {e}")
            return JSONResponse({"status": "error", "detail": f"Form parse error: {e}"}, status_code=400)

        phone = form.get("phone")
        description = form.get("description")
        location = form.get("location")
        customer_name = form.get("customer_name")
        contractor_id = form.get("contractor_id") or form.get("plumber_id") or ""
        demo = form.get("demo")
        professional_type = form.get("professional_type") or "contractor"
        language = form.get("language") or "en"

        uploaded = form.get("image")
        if uploaded and hasattr(uploaded, "filename") and uploaded.filename:
            image_bytes = await uploaded.read()
            print(f"DEBUG: Image attached: {uploaded.filename} ({len(image_bytes)} bytes)")
        print(f"[INFO] Form parsed OK")

    # ─── Unknown content type ───
    else:
        print(f"[WARNING] Unrecognized Content-Type: '{raw_ct}'")
        # Last-ditch: try form parser anyway
        try:
            form = await request.form()
            phone = form.get("phone")
            description = form.get("description")
            location = form.get("location")
            customer_name = form.get("customer_name")
            contractor_id = form.get("contractor_id") or form.get("plumber_id") or ""
            demo = form.get("demo")
            professional_type = form.get("professional_type") or "contractor"
            language = form.get("language") or "en"
            print(f"[INFO] Fallback form parser succeeded")
        except Exception as e:
            print(f"[ERROR] Fallback parser also failed: {e}")
            return JSONResponse({"status": "error", "detail": f"Unsupported Content-Type: {raw_ct}"}, status_code=415)

    # ─── Validation ───
    print(f"[DEBUG] phone={phone} | description={description} | location={location} | name={customer_name}")

    if not phone:
        return JSONResponse({"status": "error", "detail": "phone is required"}, status_code=400)
    if not description:
        return JSONResponse({"status": "error", "detail": "description is required"}, status_code=400)

    # ─── Demo detection ───
    is_demo = str(demo).lower() in ("true", "1", "on", "yes")
    print(f"🌐 phone={phone} | client={customer_name or 'Unknown'} | contractor={contractor_id} | type={professional_type} | demo={is_demo}")

    # ─── Business logic ───
    try:
        from logic import process_incoming_incident

        dispatcher_email = None
        dispatcher_name = None
        if contractor_id:
            from database import SessionLocal, Contractor
            db = SessionLocal()
            try:
                contractor_obj = db.query(Contractor).filter(
                    (Contractor.id == contractor_id) | (Contractor.contractor_phone.like(f"%{contractor_id}"))
                ).first()
                if contractor_obj:
                    dispatcher_email = getattr(contractor_obj, 'email', None)
                    dispatcher_name = getattr(contractor_obj, 'name', None)
                    print(f"DEBUG: contractor={dispatcher_name} email={dispatcher_email}")
            finally:
                db.close()

        triage_result, notification_sent = await process_incoming_incident(
            customer_phone=phone,
            body=description,
            location=location,
            customer_name=customer_name,
            media_url=None,
            sender_override=None,
            contractor_override=contractor_id,
            image_bytes=image_bytes,
            demo=is_demo,
            professional_type=professional_type or 'contractor',
            dispatcher_email=dispatcher_email,
            dispatcher_name=dispatcher_name,
            language=language
        )

        urgency = triage_result.get("urgency", "MEDIUM")
        summary = triage_result.get("summary", "")
        print(f"DEBUG: urgency={urgency} | notified={notification_sent}")

        from logic import get_localized_incident_copy
        reply_msg = "\n".join(
            get_localized_incident_copy(
                "customer_confirmation",
                language=language,
                notification_sent=notification_sent,
                summary=summary,
            )
        )

        await send_whatsapp_message(
            to=phone,
            payload_type="text",
            content={"body": reply_msg}
        )

        gear_info = triage_result.get("gear", "Standard kit")
        if isinstance(gear_info, list):
            gear_info = ", ".join(str(x) for x in gear_info)

        print("Web form registration complete.")
        return JSONResponse({
            "status": "success",
            "urgency": urgency,
            "summary": summary,
            "gear": gear_info
        })

    except Exception as api_err:
        print(f"❌ CRITICAL API_INCIDENT EXCEPTION:")
        traceback.print_exc()
        sys.stdout.flush()
        return JSONResponse({"status": "error", "detail": str(api_err)}, status_code=500)


def calculate_cycle_minutes_overage(plan_tier: str, used_seconds: int) -> dict:
    """Returns included minutes, used minutes, extra minutes, and amount for the cycle."""
    plan_name = (plan_tier or "starter").lower()
    config = PLAN_MINUTE_SETTINGS.get(plan_name, PLAN_MINUTE_SETTINGS["default"])
    included_minutes = int(config["included_minutes"])
    per_minute_price = float(config["overage_per_minute_usd"])

    used_minutes = max(0, math.ceil(max(0, int(used_seconds)) / 60.0))
    extra_minutes = max(0, used_minutes - included_minutes)
    extra_cost = round(extra_minutes * per_minute_price, 2)

    return {
        "plan_tier": plan_name,
        "included_minutes": included_minutes,
        "used_minutes": used_minutes,
        "extra_minutes": extra_minutes,
        "per_minute_price_usd": per_minute_price,
        "extra_cost": extra_cost,
    }


def get_subscription_cycle_summary(contractor_id: str) -> dict:
    from database import SessionLocal, Subscription

    db = SessionLocal()
    try:
        sub = db.query(Subscription).filter(Subscription.contractor_id == contractor_id).first()
        if not sub:
            return {"status": "not_found", "contractor_id": contractor_id, "plan_tier": "starter", "used_seconds": 0, "used_minutes": 0, "extra_minutes": 0, "extra_cost": 0.0}
        summary = calculate_cycle_minutes_overage(sub.plan_tier, sub.used_seconds)
        whop_user_id = getattr(sub, "whop_user_id", None) or getattr(sub, "whop_member_id", None)
        summary.update({
            "status": "ok",
            "contractor_id": contractor_id,
            "used_seconds": int(sub.used_seconds or 0),
            "cycle_start_date": sub.cycle_start_date,
            "whop_user_id": whop_user_id,
            "whop_payment_method_id": sub.whop_payment_method_id,
        })
        return summary
    finally:
        db.close()


def reconcile_cycle_overages_for_contractor(contractor_id: str) -> dict:
    from database import SessionLocal, Subscription, reset_subscription_cycle

    db = SessionLocal()
    try:
        sub = db.query(Subscription).filter(Subscription.contractor_id == contractor_id).first()
        if not sub:
            return {"status": "not_found", "contractor_id": contractor_id}

        summary = calculate_cycle_minutes_overage(sub.plan_tier, sub.used_seconds)
        whop_user_id = getattr(sub, "whop_user_id", None) or getattr(sub, "whop_member_id", None)
        if summary["extra_minutes"] > 0:
            invoice = create_usage_overage_invoice(
                member_id=whop_user_id,
                account_id=os.getenv("WHOP_ACCOUNT_ID", "biz_LfLkh5acNNVNWI"),
                payment_method_id=sub.whop_payment_method_id,
                extra_minutes=summary["extra_minutes"],
                unit_price_usd=summary["per_minute_price_usd"],
                product_title=f"{sub.plan_tier.title()} usage overage",
                description=f"Retell overage: {summary['extra_minutes']} extra minutes billed at ${summary['per_minute_price_usd']:.2f}/minute",
            )
            summary["invoice"] = invoice
            print(f"[BILLING] Charged {summary['extra_minutes']} minutes for contractor_id={contractor_id} -> ${summary['extra_cost']:.2f}")
        reset_subscription_cycle(sub.id)
        summary["status"] = "processed"
        summary["used_seconds_after_reset"] = 0
        return summary
    except Exception as exc:
        print(f"[BILLING] reconcile_cycle_overages_for_contractor error for {contractor_id}: {exc}")
        return {"status": "error", "contractor_id": contractor_id, "error": str(exc)}
    finally:
        db.close()


# ----------------------- RETELL ----------------------- #

# ── Track Retell Call Duration for Usage Overage ──────────────────────────────
def extract_and_track_duration(full_call_data: dict, contractor_id: str):
    # Retell gives start_timestamp and end_timestamp in ms, or duration_ms directly
    start_ts = full_call_data.get("start_timestamp")
    end_ts = full_call_data.get("end_timestamp")
    duration_ms = full_call_data.get("duration_ms")

    if not duration_ms and (start_ts and end_ts):
        duration_ms = end_ts - start_ts

    if duration_ms and contractor_id:
        duration_seconds = int(duration_ms / 1000)
        from database import increment_usage_seconds
        increment_usage_seconds(contractor_id, duration_seconds)
        print(f"[USAGE] Logged {duration_seconds}s for contractor_id={contractor_id}")


@app.post("/retell/start-call")
async def start_retell_call(request: Request, payload: dict | None = None):
    body = payload or {}
    query = request.query_params

    try:
        json_body = await request.json()
        if isinstance(json_body, dict):
            body = json_body
    except Exception:
        body = body or {}

    if not RETELL_API_KEY:
        raise HTTPException(status_code=500, detail="RETELL_API_KEY is not configured")

    provided_agent_id = body.get("agent_id") or query.get("agent_id")
    if provided_agent_id and provided_agent_id != RETELL_AGENT_ID:
        print(f"[RETELL] Ignoring client-supplied agent_id={provided_agent_id!r}; using configured agent_id={RETELL_AGENT_ID!r}")

    agent_id = RETELL_AGENT_ID
    if not agent_id:
        raise HTTPException(status_code=500, detail="RETELL_AGENT_ID is not configured")

    contractor_id = (
        body.get("contractor_id")
        or body.get("id")
        or query.get("contractor_id")
        or query.get("id")
        or "1"
    )

    extra_vars = body.get("retell_llm_dynamic_variables") or {}
    if not isinstance(extra_vars, dict):
        extra_vars = {}

    from database import get_business_name
    business_name = get_business_name(str(contractor_id)) or "Unknown Business"

    call_payload = {
        "agent_id": agent_id,
        "contractor_id": str(contractor_id),
        "transcript": True,
        "retell_llm_dynamic_variables": {
            "business_name": business_name,
            "contractor_id": str(contractor_id),
            **extra_vars,
        },
    }

    print(f"[RETELL] Creating v3 web call for agent={agent_id} contractor_id={contractor_id} business_name={business_name}")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.retellai.com/v3/create-web-call",
                headers={
                    "Authorization": f"Bearer {RETELL_API_KEY}",
                    "Content-Type": "application/json"
                },
                json=call_payload,
                timeout=30.0,
            )

            if response.status_code >= 400:
                print(f"[RETELL] {response.status_code} Response: {response.text}")

            response.raise_for_status()
            data = response.json()

            return {
                "status": "success",
                "access_token": data.get("access_token"),
                "call_id": data.get("call_id"),
                "transport": data.get("transport"),
                "ice_servers": data.get("ice_servers"),
                "data": data
            }

    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Retell API error: {exc.response.text}"
        )
    except Exception as exc:
        print(f"[RETELL] Unexpected error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/billing/reconcile-cycle")
async def billing_reconcile_cycle(payload: dict | None = None):
    body = payload or {}
    contractor_id = (body.get("contractor_id") or body.get("plumber_id") or body.get("id") or "").strip()
    if not contractor_id:
        raise HTTPException(status_code=400, detail="contractor_id is required")
    return reconcile_cycle_overages_for_contractor(contractor_id)

def sanitize_phone(phone_input: str) -> str:
    """Extract digits or convert spelled-out number strings to numeric digits."""
    if not phone_input:
        return ""
    
    # Standardize and strip punctuation
    phone_str = str(phone_input).strip()
    
    # 1. Direct digit extraction
    digits = re.sub(r"\D", "", phone_str)
    if digits:
        return digits
        
    # 2. Word-to-digit fallback (if LLM passed spelled-out numbers like "three three five")
    word_map = {
        "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
        "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"
    }
    tokens = re.findall(r"\b[a-zA-Z]+\b", phone_str.lower())
    translated = "".join([word_map[t] for t in tokens if t in word_map])
    return translated


@app.post("/retell-webhook")
async def retell_webhook(request: Request):
    try:
        body = await request.body()
        if not body:
            return JSONResponse({"status": "ignored", "reason": "empty body"})

        payload = json.loads(body)
        print(f"[RETELL] Raw payload keys: {list(payload.keys())}")

        event = payload.get("event")
        call = payload.get("call") or {}
        call_id = call.get("call_id") or payload.get("call_id")

        print(f"📞 Event: {event!r} | Call ID: {call_id!r}")

        # ── 1. Priority Path: Direct Tool-Call Arguments ──────────────────────
        # When Retell triggers a custom tool (submit_incident), arguments are
        # nested inside 'args' or 'tool_call'. Handle these immediately.
        tool_call_args = None
        if "args" in payload:
            tool_call_args = payload.get("args")
        elif "tool_call" in payload and isinstance(payload["tool_call"], dict):
            tool_call_args = payload["tool_call"].get("arguments")
        elif payload.get("interaction_type") == "tool_call" or event == "tool_call":
            tool_call_args = payload.get("args") or payload.get("tool_call_arguments")

        if tool_call_args and isinstance(tool_call_args, dict):
            print(f"[RETELL] Captured tool execution payload: {tool_call_args}")
            
            raw_phone = str(tool_call_args.get("phone") or "")
            json_data = {
                "phone": sanitize_phone(raw_phone),
                "description": str(tool_call_args.get("description") or ""),
                "location": str(tool_call_args.get("location") or ""),
                "customer_name": str(tool_call_args.get("customer_name") or ""),
                "contractor_id": str(tool_call_args.get("contractor_id") or ""),
                "professional_type": str(tool_call_args.get("professional_type") or "contractor"),
                "language": str(tool_call_args.get("language") or "en"),
                "demo": str(tool_call_args.get("demo") or "false").lower(),
            }

            base_url = str(request.base_url).rstrip("/")
            async with httpx.AsyncClient() as client:
                response = await client.post(f"{base_url}/api/incident", json=json_data, timeout=60.0)

            if response.status_code == 200:
                return JSONResponse({"status": "success", "incident_response": response.json()})
            else:
                print(f"[RETELL] /api/incident returned {response.status_code}: {response.text[:500]}")
                return JSONResponse({"status": "error", "detail": response.text[:500]}, status_code=200)

        # ── 2. Fast Path: Flat Webhook Object ─────────────────────────────────
        has_direct_fields = bool(payload.get("phone") and payload.get("description"))
        if has_direct_fields:
            raw_phone = str(payload.get("phone") or "")
            json_data = {
                "phone": sanitize_phone(raw_phone),
                "description": str(payload.get("description") or ""),
                "location": str(payload.get("location") or ""),
                "customer_name": str(payload.get("customer_name") or ""),
                "contractor_id": str(payload.get("contractor_id") or ""),
                "professional_type": str(payload.get("professional_type") or "contractor"),
                "language": str(payload.get("language") or "en"),
                "demo": str(payload.get("demo") or "false").lower(),
            }

            print(f"[RETELL] Direct payload → phone={json_data['phone']!r} | name={json_data['customer_name']!r}")

            base_url = str(request.base_url).rstrip("/")
            async with httpx.AsyncClient() as client:
                response = await client.post(f"{base_url}/api/incident", json=json_data, timeout=60.0)

            if response.status_code == 200:
                return JSONResponse({"status": "success", "incident_response": response.json()})
            else:
                print(f"[RETELL] /api/incident returned {response.status_code}: {response.text[:500]}")
                return JSONResponse({"status": "error", "detail": response.text[:500]}, status_code=200)

        # ── 3. Fallback: Standard Post-Call Analysis (call_ended / call_analyzed)
        if event not in ("call_analyzed", "call_ended"):
            return JSONResponse({"status": "ignored", "event": event})

        if not call_id:
            return JSONResponse({"status": "error", "detail": "No call_id"}, status_code=400)

        full_call = call
        if RETELL_API_KEY:
            try:
                async with httpx.AsyncClient() as client:
                    retell_response = await client.get(
                        f"https://api.retellai.com/v2/get-call/{call_id}",
                        headers={"Authorization": f"Bearer {RETELL_API_KEY}"},
                        timeout=10.0
                    )
                    if retell_response.status_code == 200:
                        full_call = retell_response.json()
                        print("[RETELL] Fetched full call details from API")
            except Exception as http_err:
                print(f"[WARNING] API fetch failed, using webhook payload: {http_err}")

        # Cascade extraction: Top-Level -> call_analysis -> dynamic_variables
        call_analysis = full_call.get("call_analysis") or {}
        dynamic_vars = full_call.get("retell_llm_dynamic_variables") or {}

        phone = (payload.get("phone") 
                 or call_analysis.get("phone") 
                 or full_call.get("from_number") 
                 or dynamic_vars.get("phone") 
                 or "")

        description = (payload.get("description") 
                       or call_analysis.get("description") 
                       or call_analysis.get("summary") 
                       or dynamic_vars.get("description") 
                       or "")

        location = (payload.get("location") 
                    or call_analysis.get("location") 
                    or dynamic_vars.get("location") 
                    or "")

        customer_name = (payload.get("customer_name") 
                         or call_analysis.get("customer_name") 
                         or dynamic_vars.get("customer_name") 
                         or "")

        contractor_id = str(payload.get("contractor_id") 
                            or payload.get("plumber_id") 
                            or call_analysis.get("contractor_id") 
                            or dynamic_vars.get("contractor_id") 
                            or "")

        professional_type = (payload.get("professional_type") 
                             or call_analysis.get("professional_type") 
                             or "contractor")

        language = payload.get("language") or "en"
        demo = str(payload.get("demo") or "false").lower()

        # Transcript Regex fallback (last resort)
        transcript = full_call.get("transcript") or ""
        # Never forward the raw live call transcript to the contractor-facing incident.
        # Keep the voice UI local-only and use a generic summary instead.
        if not description:
            description = "Customer requested a voice callback for an emergency service inquiry."
        if transcript and not customer_name:
            name_match = re.search(r"my name is ([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)", transcript, re.IGNORECASE)
            if name_match:
                customer_name = name_match.group(1)
        if transcript and not phone:
            phone_match = re.search(r"(\d{3}[-.\s]?\d{3}[-.\s]?\d{4}|\d{7,10})", transcript)
            if phone_match:
                phone = phone_match.group(0)
        if transcript and not location:
            loc_match = re.search(r"(?:in|at)\s+([A-Z][a-zA-Z\s,]+)", transcript, re.IGNORECASE)
            if loc_match:
                location = re.split(r'[.!?;]', loc_match.group(1).strip())[0].strip()

        # Always sanitize phone string to digits before dispatch
        sanitized_phone = sanitize_phone(phone)

        print(f"[RETELL] Processed payload → phone={sanitized_phone!r} | name={customer_name!r} | location={location!r}")

        json_data = {
            "phone": sanitized_phone,
            "description": description,
            "location": location,
            "customer_name": customer_name,
            "contractor_id": contractor_id,
            "professional_type": professional_type,
            "language": language,
            "demo": demo,
        }

        extract_and_track_duration(full_call, contractor_id)

        base_url = str(request.base_url).rstrip("/")
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{base_url}/api/incident", json=json_data, timeout=60.0)

        if response.status_code == 200:
            return JSONResponse({"status": "success", "incident_response": response.json()})
        else:
            print(f"[RETELL] /api/incident returned {response.status_code}: {response.text[:500]}")
            return JSONResponse({"status": "error", "detail": response.text[:500]}, status_code=200)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


# ==============================================================================
# ADMIN AUTHENTICATION & DASHBOARD ENDPOINTS
# ==============================================================================

_ADMIN_JWT_SECRET = os.getenv("ADMIN_JWT_SECRET", "your_random_secret")
_ADMIN_JWT_ALGO = "HS256"


class AdminSetPasswordRequest(BaseModel):
    phone: str
    password: str

class AdminLoginRequest(BaseModel):
    phone: str
    password: str

class AdminStatusRequest(BaseModel):
    id: str
    status: str

def _clean_phone(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit())

def _issue_admin_token(payload: dict) -> str:
    payload["exp"] = datetime.now(timezone.utc) + timedelta(hours=24)
    return pyjwt.encode(payload, _ADMIN_JWT_SECRET, algorithm=_ADMIN_JWT_ALGO)

def _verify_admin_token(token: str) -> dict:
    try:
        return pyjwt.decode(token, _ADMIN_JWT_SECRET, algorithms=[_ADMIN_JWT_ALGO])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

async def _get_current_admin(request: Request) -> dict:
    """Reads JWT from Authorization header OR from admin_token cookie."""
    token = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.cookies.get("admin_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _verify_admin_token(token)


@app.post("/admin/set-password")
async def admin_set_password(body: AdminSetPasswordRequest):
    print('setting the password')
    """Set or update password for an existing contractor using their registered phone."""
    if not body.password or len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    
    print(f"admin set-password: received phone='{body.phone}' password length={len(body.password)}")
    clean = _clean_phone(body.phone)
    print(f"admin set-password: raw='{body.phone}' clean='{clean}'")

    from database import SessionLocal, Contractor
    db = SessionLocal()
    try:
        # Match by checking if the stored number ends with the cleaned input
        contractor = db.query(Contractor).filter(Contractor.contractor_phone.like(f"%{clean}")).first()
        if not contractor:
            # Show registered phones in error so user knows what to type
            all_plumbers = db.query(Contractor).all()
            phones = ", ".join(f"{p.name}: {p.contractor_phone}" for p in all_plumbers) or "none"
            raise HTTPException(
                status_code=404,
                detail=f"Phone '{clean}' not found. Registered phones: {phones}"
            )
        # 🔐 Hash with argon2
        hashed = pqc_hash_password(body.password)
        contractor.password_hash = hashed
        db.commit()
        print(f"admin set-password success for {contractor.name} ({clean})")
        return {"success": True, "name": contractor.name}
    finally:
        db.close()


@app.post("/admin/login")
async def admin_login(body: AdminLoginRequest, request: Request):
    """Login: phone + password (or 'admin' + ADMIN_MASTER_PASSWORD for master access)."""
    print("got Login: phone + password (or 'admin' + ADMIN_MASTER_PASSWORD for master access).")
    master_pwd = os.getenv("ADMIN_MASTER_PASSWORD")
    print(f"admin login: received phone='{body.phone}' password length={len(body.password)}")

    # Master admin bypass
    if body.phone.strip().lower() == "admin":
        if not master_pwd:
            raise HTTPException(status_code=401, detail="ADMIN_MASTER_PASSWORD is not set. Add it to your environment variables.")
        if body.password != master_pwd:
            raise HTTPException(status_code=401, detail="Incorrect master password.")
        token = _issue_admin_token({"id": "master", "name": "Master Admin", "phone": "ALL", "isMaster": True})
        response = JSONResponse({"success": True, "name": "Master Admin", "is_master": True})
        response.set_cookie(
            key="admin_token",
            value=token,
            httponly=True,
            secure=True,  # set to False for local dev if needed
            samesite="lax"
        )
        return response

    # Contractor login
    clean = _clean_phone(body.phone)
    from database import SessionLocal, Contractor
    db = SessionLocal()
    try:
        contractor = db.query(Contractor).filter(Contractor.contractor_phone.like(f"%{clean}")).first()
        if not contractor:
            raise HTTPException(status_code=401, detail="Phone not found. Use 'admin' for master access.")
        if not contractor.password_hash:
            raise HTTPException(status_code=401, detail="No password set. Use the Set Password option first.", headers={"X-Needs-Password": "true"})
        # 🔐 Verify with argon2
        if not pqc_verify_password(body.password, contractor.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials.")
        token = _issue_admin_token({"id": contractor.id, "name": contractor.name, "phone": contractor.contractor_phone, "isMaster": False})
        response = JSONResponse({"success": True, "name": contractor.name})
        response.set_cookie(
            key="admin_token",
            value=token,
            httponly=True,
            secure=True,  # set to False for local dev if needed
            samesite="lax"
        )
        return response
    finally:
        db.close()

@app.get("/admin/me")
async def admin_me(request: Request):
    """Check current session."""
    user = await _get_current_admin(request)
    return {"name": user["name"], "phone": user["phone"]}


@app.get("/admin/plumbers")
async def admin_list_plumbers(request: Request):
    """Debug: list all registered plumbers and whether they have a password set."""
    from database import SessionLocal, Contractor
    db = SessionLocal()
    try:
        plumbers = db.query(Contractor).order_by(Contractor.id).all()
        return {"plumbers": [
            {
                "id": p.id,
                "name": p.name,
                "contractor_phone": p.contractor_phone,
                "active": p.active,
                "has_password": bool(p.password_hash)
            } for p in plumbers
        ]}
    finally:
        db.close()


@app.get("/admin/incidents")
async def admin_incidents(request: Request,
                          urgency: str = None,
                          status: str = None,
                          from_date: str = None,
                          to_date: str = None):
    """Return incidents filtered by contractor or all (master admin)."""
    user = await _get_current_admin(request)
    is_master = user.get("isMaster", False)
    contractor_phone = user.get("phone")

    from database import SessionLocal, Incident
    from sqlalchemy import and_
    db = SessionLocal()
    try:
        q = db.query(Incident)
        if not is_master:
            q = q.filter(Incident.contractor_phone == contractor_phone)
        if urgency and urgency != "ALL":
            q = q.filter(Incident.urgency == urgency)
        if status and status != "ALL":
            q = q.filter(Incident.status == status)
        if from_date:
            q = q.filter(Incident.timestamp >= datetime.fromisoformat(from_date))
        if to_date:
            q = q.filter(Incident.timestamp <= datetime.fromisoformat(to_date + "T23:59:59"))
        incidents = q.order_by(Incident.timestamp.desc()).limit(200).all()
        return {"incidents": [
            {
                "id": i.id,
                "customer_phone": i.customer_phone,
                "contractor_phone": i.contractor_phone,
                "urgency": i.urgency,
                "summary": i.summary,
                "raw_message": i.raw_message,
                "location": i.location,
                "customer_name": i.customer_name,
                "image_url": i.image_url,
                "status": i.status,
                "gear": i.gear,
                "timestamp": i.timestamp.isoformat() if i.timestamp else None,
            } for i in incidents
        ]}
    finally:
        db.close()



# --------- FUTURE --------------#
# ----------------------- LINE (local messaging) ----------------------- #
"""
we will need something like this for line integration

from fastapi import Request
from fastapi.responses import JSONResponse
import httpx
import os

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

@app.post("/line/webhook")
async def line_webhook(request: Request):
    body = await request.json()
    print("LINE webhook event:", body)

    # LINE sends events in an array under "events"
    for event in body.get("events", []):
        if event.get("type") == "message":
            user_id = event["source"]["userId"]
            text = event["message"].get("text", "")

            # Example: echo back the message
            reply_token = event["replyToken"]
            reply_payload = {
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": f"You said: {text}"}]
            }

            headers = {
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json"
            }
            async with httpx.AsyncClient() as client:
                await client.post(
                    "https://api.line.me/v2/bot/message/reply",
                    headers=headers,
                    json=reply_payload
                )

    return JSONResponse({"status": "ok"})
"""