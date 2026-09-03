import os
import sys
import traceback

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

from fastapi import FastAPI, Request, Form, UploadFile, File, Depends, HTTPException, Body
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
from logic import send_whatsapp_message, process_incoming_property_message, build_property_pdf
import jwt as pyjwt
import asyncio
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
import os
import psycopg2
from psycopg2.extras import RealDictCursor

# 🔐 Password hashing (argon2 via passlib)
from passlib.hash import argon2

# load our local environment from .env
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
                from logic import process_incoming_property_message, process_incoming_incident

                print(f"DEBUG (bg): from_jid={from_jid}")

                is_prop_handled = await process_incoming_property_message(customer_phone, body_raw, wbot_url, from_jid)
                if is_prop_handled:
                    print("DEBUG (bg): property message handled; returning")
                    return

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
# here we handle new incidents
@app.post("/api/incident")
async def api_incident(
    phone: str = Form(...),
    description: str = Form(...),
    location: str = Form(None),
    customer_name: str = Form(None),
    plumber_id: str = Form(None),
    image: UploadFile = File(None),
    demo: str = Form(None),
    professional_type: str = Form(None)
):

    # check if demo mode is enabled
    print(f"\n=================== WEB FORM INBOUND ===================")
    # CRITICAL: Robust demo detection. Handles "true", "1", "on", boolean True, etc.
    is_demo = str(demo).lower() in ("true", "1", "on", "yes")
    print(f"🌐 Submission processing for destination endpoint: {phone} | Client: {customer_name or 'Unknown'} | Plumber: {plumber_id} | Type: {professional_type or 'plumber'} | Demo Mode: {is_demo}")
    
    try:
        image_bytes = None
        if image and image.filename:
            image_bytes = await image.read()
            print(f"DEBUG: Web form binary attachment detected: {image.filename} ({len(image_bytes)} bytes)")
            
        from logic import process_incoming_incident
        
        # CHANGED: Passed location and customer_name as keyword arguments into your processing routine
        triage_result, _ = await process_incoming_incident(
            customer_phone=phone, 
            body=description, 
            location=location,
            customer_name=customer_name,
            media_url=None, 
            sender_override=None,
            plumber_override=plumber_id,
            image_bytes=image_bytes,
            demo=is_demo,
            professional_type=professional_type or 'plumber',
        )
        
        urgency = triage_result.get("urgency", "MEDIUM")
        summary = triage_result.get("summary", "")
        print(f"DEBUG: Web form AI evaluations resolved. Status level: {urgency}")

        if urgency == "HIGH":
            reply_msg = f"🚨 *EMERGENCY DETECTED*\n\nWe received your web request. We've flagged this as high priority: {summary}\n\nA plumber is being paged now!"
        else:
            reply_msg = f"✅ *Request Received*\n\nSummary: {summary}\n\nThis has been logged from the web form. We will contact you shortly."

        # Send customer confirmation (demo no longer blocks this behavior)
        await send_whatsapp_message(
            to=phone,
            payload_type="text",
            content={"body": reply_msg}
        )

        gear_info = triage_result.get("gear", "Standard kit")
        if isinstance(gear_info, list):
            gear_info = ", ".join(str(x) for x in gear_info)

        print("✅ Web form registration complete.")
        # send the structured json as a message
        return JSONResponse({
            "status": "success", 
            "urgency": urgency, 
            "summary": summary,
            "gear": gear_info
        })

    except Exception as api_err:
        print(f"❌ CRITICAL API_INCIDENT EXCEPTION CRASH:")
        print("".join(traceback.format_exception(type(api_err), api_err, api_err.__traceback__)))
        sys.stdout.flush()
        return JSONResponse({"status": "error", "detail": str(api_err)}, status_code=500)
    



# --- QR SCAN & PROPERTY INQUIRY ENDPOINT ---
class PropertyInquiryRequest(BaseModel):
    customer_phone: str
    customer_name: str
    property_id: str
    budget: str
    timeline: str
    marketer_phone: str = None  # Optional override, defaults to system marketer



@app.post("/api/property-inquiry")
async def api_property_inquiry(payload: PropertyInquiryRequest = Body(...)):
    """
    Endpoint triggered when a user scans a property QR code, passes hardcoded
    qualification steps, and submits their budget/timeline.
    """
    print(f"\n=================== QR PROPERTY INQUIRY ===================")
    print(f"🏠 Property: {payload.property_id} | Client: {payload.customer_name} ({payload.customer_phone})")
    
    try:
        from logic import process_property_lead
        
        result = await process_property_lead(
            customer_phone=payload.customer_phone,
            customer_name=payload.customer_name,
            property_id=payload.property_id,
            budget=payload.budget,
            timeline=payload.timeline,
            marketer_phone=payload.marketer_phone
        )
        
        return JSONResponse({
            "status": "success",
            "message": "Inquiry processed and marketer notified via WhatsApp.",
            "lead_summary": result.get("lead_summary")
        })
    except Exception as err:
        print(f"❌ PROPERTY INQUIRY ERROR: {err}")
        return JSONResponse({"status": "error", "detail": str(err)}, status_code=500)


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
    """Set or update password for an existing plumber using their registered phone."""
    if not body.password or len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    
    print(f"admin set-password: received phone='{body.phone}' password length={len(body.password)}")
    clean = _clean_phone(body.phone)
    print(f"admin set-password: raw='{body.phone}' clean='{clean}'")

    from database import SessionLocal, Plumber
    db = SessionLocal()
    try:
        # Match by checking if the stored number ends with the cleaned input
        plumber = db.query(Plumber).filter(Plumber.plumber_phone.like(f"%{clean}")).first()
        if not plumber:
            # Show registered phones in error so user knows what to type
            all_plumbers = db.query(Plumber).all()
            phones = ", ".join(f"{p.name}: {p.plumber_phone}" for p in all_plumbers) or "none"
            raise HTTPException(
                status_code=404,
                detail=f"Phone '{clean}' not found. Registered phones: {phones}"
            )
        # 🔐 Hash with argon2
        hashed = pqc_hash_password(body.password)
        plumber.password_hash = hashed
        db.commit()
        print(f"admin set-password success for {plumber.name} ({clean})")
        return {"success": True, "name": plumber.name}
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

    # Plumber login
    clean = _clean_phone(body.phone)
    from database import SessionLocal, Plumber
    db = SessionLocal()
    try:
        plumber = db.query(Plumber).filter(Plumber.plumber_phone.like(f"%{clean}")).first()
        if not plumber:
            raise HTTPException(status_code=401, detail="Phone not found. Use 'admin' for master access.")
        if not plumber.password_hash:
            raise HTTPException(status_code=401, detail="No password set. Use the Set Password option first.", headers={"X-Needs-Password": "true"})
        # 🔐 Verify with argon2
        if not pqc_verify_password(body.password, plumber.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials.")
        token = _issue_admin_token({"id": plumber.id, "name": plumber.name, "phone": plumber.plumber_phone, "isMaster": False})
        response = JSONResponse({"success": True, "name": plumber.name})
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
    from database import SessionLocal, Plumber
    db = SessionLocal()
    try:
        plumbers = db.query(Plumber).order_by(Plumber.id).all()
        return {"plumbers": [
            {
                "id": p.id,
                "name": p.name,
                "plumber_phone": p.plumber_phone,
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
    """Return incidents filtered by plumber or all (master admin)."""
    user = await _get_current_admin(request)
    is_master = user.get("isMaster", False)
    plumber_phone = user.get("phone")

    from database import SessionLocal, Incident
    from sqlalchemy import and_
    db = SessionLocal()
    try:
        q = db.query(Incident)
        if not is_master:
            q = q.filter(Incident.plumber_phone == plumber_phone)
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
                "plumber_phone": i.plumber_phone,
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


@app.patch("/admin/incident-status")
async def admin_update_status(body: AdminStatusRequest, request: Request):
    """Update incident status (PENDING / RESOLVED)."""
    await _get_current_admin(request)  # auth check
    if body.status not in ("PENDING", "RESOLVED"):
        raise HTTPException(status_code=400, detail="status must be PENDING or RESOLVED")
    from database import SessionLocal, Incident
    db = SessionLocal()
    try:
        incident = db.query(Incident).filter(Incident.id == body.id).first()
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found")
        incident.status = body.status
        db.commit()
        return {"success": True}
    finally:
        db.close()


# PROPERTIES ======================================================================================================== # 

# --- IMPORTS ---
import base64
import os
import shutil
import uuid
import json
from fastapi import Request, UploadFile, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# --- SCHEMAS ---
class PropertyManagerCreate(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
class PropertyManagerResponse(BaseModel):
    id: str
    name: str
    phone: str
    email: Optional[str]
class PropertyCreate(BaseModel):
    id: str = Field(..., description="Unique Property ID (e.g. ATH-39)")
    manager_id: str
    title: str
    address: str
    description: Optional[str] = None
    budget_range: Optional[str] = None
    image_url: Optional[str] = None
    pdf_url: Optional[str] = None
class PropertyResponse(BaseModel):
    id: str
    manager_id: Optional[str]
    title: str
    address: str
    description: Optional[str]
    budget_range: Optional[str]
    image_url: Optional[str]
    pdf_url: Optional[str]
    portal_links: Optional[List[dict]] = []


# --- PROPERTY MANAGER ENDPOINTS ---
@app.post("/api/property-managers", response_model=PropertyManagerResponse)
async def create_property_manager(payload: PropertyManagerCreate):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO property_managers (name, phone, email)
                VALUES (%s, %s, %s)
                RETURNING id, name, phone, email
                """,
                (payload.name, payload.phone, payload.email)
            )
            manager = cur.fetchone()
            conn.commit()
            return manager
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=400, detail="A property manager with this phone number already exists.")
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/property-managers", response_model=List[PropertyManagerResponse])
async def list_property_managers():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, phone, email FROM property_managers ORDER BY name ASC")
            return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    """
    Catches Pydantic validation errors and safely serializes them.
    Replaces raw bytes (which crash jsonable_encoder) with a safe placeholder.
    """
    safe_errors = []
    for error in exc.errors():
        err = dict(error)
        # The 'input' key holds the raw value that failed validation.
        # If a client sent a file where a string was expected, this is bytes.
        if "input" in err and isinstance(err["input"], bytes):
            err["input"] = f"<binary data: {len(err['input'])} bytes>"
        safe_errors.append(err)

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": safe_errors},
    )


def save_upload_file(upload_file: UploadFile, destination: str) -> str:
    """Persist an uploaded file to disk and close the handle."""
    try:
        with open(destination, "wb") as buffer:
            shutil.copyfileobj(upload_file.file, buffer)
    finally:
        upload_file.file.close()
    return destination

# --- PROPERTY ENDPOINTS ---
@app.post("/api/properties", response_model=PropertyResponse)
async def create_property(request: Request):
    content_type = request.headers.get("content-type", "")
    
    # Configure where files are stored (use env var or adjust as needed)
    upload_dir = os.environ.get("UPLOAD_DIR", "/tmp/uploads")
    os.makedirs(upload_dir, exist_ok=True)

    # Variables to collect
    prop_id = manager_id = title = address = None
    description = budget_range = image_url = pdf_url = None

    # ------------------------------------------------------------------
    # A) JSON mode: client sends PropertyCreate as JSON with URL strings
    # ------------------------------------------------------------------
    if "application/json" in content_type:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        # Reuse your existing Pydantic rules for JSON payloads
        try:
            payload = PropertyCreate(**body)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=e.errors())

        prop_id       = payload.id
        manager_id    = payload.manager_id
        title         = payload.title
        budget_range  = payload.budget_range
        pdf_url       = payload.pdf_url

    # ------------------------------------------------------------------
    # B) Multipart mode: client sends form fields + optional file uploads
    # ------------------------------------------------------------------
    elif "multipart/form-data" in content_type:
        form = await request.form()

        prop_id      = form.get("id")
        manager_id   = form.get("manager_id")
        title        = form.get("title")
        description  = form.get("description") or None
        budget_range = form.get("budget_range") or None

        # Grab files and strings completely separately
        pdf_file = form.get("pdf_file")
        pdf_url_str = form.get("pdf_url")


        # --- PDF ---
        if hasattr(pdf_file, "filename") and pdf_file.filename:
            ext = os.path.splitext(pdf_file.filename)[1]
            pdf_name = f"{uuid.uuid4()}{ext}"
            pdf_path = os.path.join(upload_dir, pdf_name)
            save_upload_file(pdf_file, pdf_path)
            pdf_url = f"/uploads/{pdf_name}"
        elif isinstance(pdf_url_str, str) and pdf_url_str.strip():
            pdf_url = pdf_url_str
        else:
            pdf_url = None

        # Dynamic PDF brochure builder fallback (Requirement 0)
        if not pdf_url:
            compiled_pdf = build_property_pdf(
                property_id=prop_id,
                title=title,
                address=address,
                description=description or "",
                budget_range=budget_range or "",
                image_url=image_url
            )
            if compiled_pdf:
                pdf_url = compiled_pdf
    else:
        raise HTTPException(status_code=415, detail="Unsupported Media Type")

    # ------------------------------------------------------------------
    # Database insert (same logic as before)
    # ------------------------------------------------------------------
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO properties (
                    id, manager_id, title, budget_range, pdf_url
                )
                VALUES (%s, %s::uuid, %s, %s, %s)
                RETURNING id, manager_id, title, budget_range, pdf_url
                """,
                (
                    str(prop_id).upper().strip(),
                    manager_id,
                    title,
                    budget_range,
                    pdf_url,
                ),
            )
            prop = cur.fetchone()
            
            # Save portal links if provided
            portal_links_raw = form.get("portal_links") if "multipart/form-data" in content_type else getattr(payload, "portal_links", None)
            if portal_links_raw:
                try:
                    links = json.loads(portal_links_raw) if isinstance(portal_links_raw, str) else portal_links_raw
                    for link in links:
                        cur.execute(
                            "INSERT INTO property_links (property_id, url, source_tag) VALUES (%s, %s, %s)",
                            (prop_id, link.get("url"), link.get("source"))
                        )
                except Exception as e:
                    print(f"Error saving portal links: {e}")
            
            conn.commit()
            
            # Fetch links to return
            cur.execute("SELECT url, source_tag as source FROM property_links WHERE property_id = %s", (prop_id,))
            prop["portal_links"] = cur.fetchall()
            
            return prop
    except psycopg2.IntegrityError as e:
        conn.rollback()
        print("FAILED CONSTRAINT:", e.diag.constraint_name)
        print("DETAIL:", e.diag.message_detail)
        raise HTTPException(
            status_code=400,
            detail=f"Constraint [{e.diag.constraint_name}] failed: {e.diag.message_detail}"
        )
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/properties", response_model=List[PropertyResponse])
async def list_properties():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.manager_id, p.title, p.budget_range, p.pdf_url
                FROM properties p
                """
            )
            props = cur.fetchall()
            
            for p in props:
                cur.execute("SELECT url, source_tag as source FROM property_links WHERE property_id = %s", (p["id"],))
                p["portal_links"] = cur.fetchall()
                
            print("####################################################################")   
            print("props gotten", props)
            return props
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/properties/{property_id}/assets")
async def get_property_assets(property_id: str, request: Request):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT p.title, pdf_url FROM properties WHERE id = %s", (property_id,))
            prop = cur.fetchone()
            if not prop:
                raise HTTPException(status_code=404, detail="Property not found")
            
            assets = []
            base_url = str(request.base_url).rstrip("/")
            
            # Helper to make full url
            def make_url(url_val):
                if not url_val: return None
                url_val = url_val.strip()
                if url_val.startswith("http://") or url_val.startswith("https://"):
                    return url_val
                return f"{base_url}{url_val if url_val.startswith('/') else '/' + url_val}"
                        
            if prop["pdf_url"]:
                full_pdf = make_url(prop["pdf_url"])
                if full_pdf:
                    assets.append({
                        "url": full_pdf,
                        "mimetype": "application/pdf",
                        "fileName": f"{property_id}.pdf"
                    })

            print("####################################################################")
            print(f"DEBUG: Returning assets for property {property_id}: {assets}")
                    
            return assets
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

class PropertyUpdate(BaseModel):
    title: Optional[str] = None
    address: Optional[str] = None
    description: Optional[str] = None
    budget_range: Optional[str] = None
    image_url: Optional[str] = None
    pdf_url: Optional[str] = None

@app.put("/api/properties/{property_id}")
async def update_property(property_id: str, payload: PropertyUpdate):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            updates = []
            values = []
            if payload.title is not None:
                updates.append("title = %s")
                values.append(payload.title)
            if payload.budget_range is not None:
                updates.append("budget_range = %s")
                values.append(payload.budget_range)
            if payload.pdf_url is not None:
                updates.append("pdf_url = %s")
                values.append(payload.pdf_url)
                
            if not updates:
                return {"status": "success"}
                
            values.append(property_id)
            query = f"UPDATE properties SET {', '.join(updates)} WHERE id = %s"
            cur.execute(query, values)
            conn.commit()
            return {"status": "success"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


class HotLeadNotifyRequest(BaseModel):
    lead: dict
    managerEmail: Optional[str] = None
    reminder: Optional[bool] = False

@app.post("/api/notify/hot-lead")
async def notify_hot_lead(payload: HotLeadNotifyRequest):
    lead = payload.lead
    manager_email = "nmirnes32@gmail.com" #payload.managerEmail
    reminder = payload.reminder
    
    if not manager_email:
        return {"status": "skipped", "reason": "No manager email provided"}

    # Extract lead details
    prop_code = lead.get("property_code", "Unknown")
    phone = lead.get("phone", "Unknown")
    payment = lead.get("payment", "Unknown")
    availability = lead.get("availability", "Unknown")
    source = lead.get("source", "Unknown")
    
    metadata = lead.get("metadata", {})
    name = metadata.get("name", "Unknown Buyer")
    
    subject = f"{'REMINDER: ' if reminder else ''}Hot Lead — {prop_code} — {name}"
    
    body = f"""
    Lead Lead Details:
    ----------------
    Property: {prop_code}
    Buyer Name: {name}
    Phone: +{phone}
    Payment: {payment}
    Availability: {availability}
    Source: {source}

    Please follow up immediately.
    """

    smtp_server = os.environ.get("SMTP_SERVER")
    smtp_port = os.environ.get("SMTP_PORT", 587)
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASSWORD")

    if smtp_server and smtp_user and smtp_pass:
        try:
            msg = MIMEMultipart()
            msg['From'] = smtp_user
            msg['To'] = manager_email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))
            
            server = smtplib.SMTP(smtp_server, int(smtp_port))
            server.starttls()
            server.login(smtp_user, smtp_pass)
            text = msg.as_string()
            server.sendmail(smtp_user, manager_email, text)
            server.quit()
            print(f"✅ Hot-lead email sent to {manager_email}")
        except Exception as e:
            print(f"❌ Failed to send hot-lead email: {e}")
            return {"status": "error", "detail": str(e)}
    else:
        print(f"⚠️ SMTP not configured. Would send email to {manager_email}:")
        print(f"Subject: {subject}\nBody: {body}")
        
    return {"status": "success"}
