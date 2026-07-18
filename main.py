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

from fastapi import FastAPI, Request, Form, UploadFile, File, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
from logic import send_whatsapp_message
import jwt as pyjwt
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel

# 🔐 Quantum‑safe hashing imports
from mlkem.ml_kem import ML_KEM
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.hashes import SHA256

# load our local environment from .env
load_dotenv()

# Initialize ML‑KEM engine
kem_engine = ML_KEM()
ENCRYPTION_KEY, DECRYPTION_KEY = kem_engine.key_gen()

app = FastAPI(title="Coherzo")

# here you would set Wbot api url and your number from environment variables or defaults
WBOT_API_URL = os.getenv("WBOT_API_URL", "http://localhost:3001").rstrip("/")
PLUMBER_NUMBER = os.getenv("PLUMBER_WHATSAPP_NUMBER")

# 🔐 Quantum‑safe password hashing helpers
def pqc_hash_password(password: str) -> str:
    """
    Hash a password using ML‑KEM + HKDF + AES‑GCM.
    Returns a hex string containing: ciphertext_key_packet | nonce | encrypted_hash
    """
    # 1. Encapsulate shared secret
    shared_secret, ciphertext_key_packet = kem_engine.encaps(ENCRYPTION_KEY)
    
    # 2. Derive AES key
    hkdf = HKDF(
        algorithm=SHA256(),
        length=32,
        salt=None,
        info=b"Coherzo-Password-Hash"
    )
    aes_key = hkdf.derive(shared_secret)
    
    # 3. Encrypt password bytes
    nonce = os.urandom(12)
    aesgcm = AESGCM(aes_key)
    encrypted_password = aesgcm.encrypt(nonce, password.encode('utf-8'), None)
    
    # 4. Combine into a single storage string
    combined = ciphertext_key_packet + nonce + encrypted_password
    return combined.hex()

def pqc_verify_password(password: str, stored_hash_hex: str) -> bool:
    try:
        combined = bytes.fromhex(stored_hash_hex)
        
        # 1. Extract ciphertext_key_packet: use the length from kem_engine.encaps
        #    (or store the length alongside the hash if the lib doesn’t expose it)
        #    For now, assume ML‑KEM‑1024: 1568 bytes
        ciphertext_len = 1568  # adjust if your ML‑KEM parameter set differs
        ciphertext_key_packet = combined[:ciphertext_len]
        nonce = combined[ciphertext_len:ciphertext_len+12]
        encrypted_password = combined[ciphertext_len+12:]
        
        # 2. Decapsulate shared secret
        shared_secret = kem_engine.decaps(DECRYPTION_KEY, ciphertext_key_packet)
        
        # 3. Derive AES key
        hkdf = HKDF(
            algorithm=SHA256(),
            length=32,
            salt=None,
            info=b"Coherzo-Password-Hash"
        )
        aes_key = hkdf.derive(shared_secret)
        
        # 4. Decrypt
        aesgcm = AESGCM(aes_key)
        decrypted_password = aesgcm.decrypt(nonce, encrypted_password, None)
        
        return decrypted_password.decode('utf-8') == password
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

        # Robust multi-format parser with explicit debugging
        if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            form_data = await request.form()
            print(f"DEBUG: Form data keys found: {list(form_data.keys())}")
            customer_phone = form_data.get("From")
            body_raw = form_data.get("Body", "").strip()
        else:
            try:
                json_data = await request.json()
                print(f"DEBUG: JSON payload found: {json_data}")
                customer_phone = json_data.get("From")
                body_raw = json_data.get("Body", "").strip()
            except Exception as json_err:
                print(f"DEBUG: Failed parsing as JSON: {str(json_err)}")

        if not customer_phone:
            print("❌ ERROR: Request received but no sender phone ('From') could be resolved.")
            return JSONResponse({"status": "error", "message": "No sender phone found"}, status_code=400)
            
        body_upper = body_raw.upper().replace(" ", "_").strip()
        print(f"📥 Processing text from {customer_phone}: '{body_raw}' (Normalized: {body_upper})")

        # 1. Handle Commands (Filtering in WhatsApp Chat)
        filter_keywords = ["URGENT", "NOT_URGENT", "ALL_TASKS", "EMERGENCY", "NON_EMERGENCY", "NO_EMERGENCY", "FILTER", "MID", "ALL"]
        
        if body_upper in filter_keywords:
            print(f"DEBUG: Keyword identified: {body_upper}. Executing database query.")
            from database import get_incidents
            incidents = get_incidents()
            print(f"DEBUG: Total database incidents retrieved: {len(incidents)}")

            # defining emergency emojis
            if body_upper in ["URGENT", "EMERGENCY"]:
                print("🚨 Routing to High-Priority Alert Pipeline")
                filtered = [i for i in incidents if i['urgency'] == "HIGH"][:5]
                title = "*🚨 Recent Urgent Tasks*"
            elif body_upper in ["NOT_URGENT", "NON_EMERGENCY", "NO_EMERGENCY"]:
                print("✅ Routing to Standard Log Archive")
                filtered = [i for i in incidents if i['urgency'] != "HIGH"][:5]
                title = "*✅ Non-Urgent Tasks*"
            else:  
                print(f"⚡ Routing to Context Filter: {body_upper}")
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

            # FIXED: Payload type flipped to 'text' using our unbreakable keyword-based menus
            print(f"DEBUG: Dispatching compiled list back to {customer_phone} via wbot API")
            await send_whatsapp_message(
                to=customer_phone,
                payload_type="text",
                content={
                    "body": msg_text,
                    "buttons": ["Emergency", "No Emergency", "All"] # Left for fallback metadata compatibility
                }
            )
            print("✅ Webhook response processed clean.")
            return JSONResponse({"status": "ok"})

        # 2. Handle New Incidents
        print(f"延 Handling as conversational input. Sending to AI engine...")
        
        incoming_media = form_data.get("MediaUrl0") if form_data else None
        sender_override = form_data.get("FromNumber") if form_data else None
        plumber_override = form_data.get("PlumberNumber") if form_data else None
        image_file = form_data.get("MediaFile") if form_data else None
        
        image_bytes = None
        if image_file:
            try:
                image_bytes = await image_file.read()
                print(f"DEBUG: Attached image binary loaded size: {len(image_bytes)} bytes")
            except Exception as e:
                print(f"⚠️ Error reading file data streaming layers: {e}")

        from logic import process_incoming_incident
        print("DEBUG: Executing process_incoming_incident pipeline...")
        
        triage_result, _ = await process_incoming_incident(
            customer_phone, body_raw, incoming_media, 
            sender_override=sender_override,
            plumber_override=plumber_override,
            image_bytes=image_bytes
        )
        
        urgency = triage_result.get("urgency", "MEDIUM")
        summary = triage_result.get("summary", "")
        print(f"DEBUG: AI Processing Complete. Urgency: {urgency} | Summary length: {len(summary)}")

        # message to customer

        if urgency == "HIGH":
            reply_msg = f"🚨 *EMERGENCY DETECTED*\n\nWe've flagged this as high priority: {summary}\n\nA plumber is being paged now."
        else:
            reply_msg = f"✅ *Request Received*\n\nSummary: {summary}\n\nThis has been logged. We will contact you shortly."

        await send_whatsapp_message(
            to=customer_phone,
            payload_type="text",
            content={"body": reply_msg}
        )
        print("✅ New incident tracked and confirmed successfully.")
        return JSONResponse({"status": "ok"})

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
    is_demo = (demo == "true")
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
        # 🔐 Replace bcrypt with quantum‑safe hash
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

    # Master admin bypass
    if body.phone.strip().lower() == "admin":
        if not master_pwd:
            raise HTTPException(status_code=401, detail="ADMIN_MASTER_PASSWORD is not set. Add it to your environment variables.")
        if body.password != master_pwd:
            raise HTTPException(status_code=401, detail="Incorrect master password.")
        token = _issue_admin_token({"id": "master", "name": "Master Admin", "phone": "ALL", "isMaster": True})
        response = JSONResponse({"success": True, "name": "Master Admin"})
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
        # 🔐 Replace bcrypt with quantum‑safe verification
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