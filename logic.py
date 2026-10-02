import os
import sys
from ai_engine import analyze_triage, analyze_property_lead
from database import log_incident
from dotenv import load_dotenv
import urllib.parse
from twilio.rest import Client
from database import log_property_lead 
import asyncio
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

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

load_dotenv()

import json
import httpx

# WBOT Config
WBOT_API_URL = os.getenv("WBOT_API_URL", "http://localhost:3001").rstrip("/")
CONTRACTOR_NUMBER = os.getenv("CONTRACTOR_WHATSAPP_NUMBER", "").strip()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# SMTP Configurations
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER", "your-email@gmail.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "your-app-password")


# 1. Blocking Synchronous Worker Function
def _send_email_sync(
    to_email: str, 
    subject: str, 
    body_text: str, 
    body_html: Optional[str] = None
) -> bool:
    """Executes the synchronous, blocking SMTP network call."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Emergency Dispatch <{SMTP_USER}>"
    msg["To"] = to_email

    msg.attach(MIMEText(body_text, "plain"))
    if body_html:
        msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        logger.info(f"Email successfully sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False


# 2. Non-blocking Async Wrapper
async def send_email_async(
    to_email: str, 
    subject: str, 
    body_text: str, 
    body_html: Optional[str] = None
) -> bool:
    """Offloads the synchronous SMTP function to a background thread pool."""
    return await asyncio.to_thread(
        _send_email_sync, 
        to_email, 
        subject, 
        body_text, 
        body_html
    )

def clean_whatsapp_number(number: str) -> str:
    if not number:
        return number
    # Remove prefix formatting
    number = str(number).strip()
    number = number.replace("whatsapp:", "").replace("+", "").replace(" ", "").replace("-", "")
    return number


# twillio implementation for sending messages to contractors (for later)
def send_dispatch_alert(target_contractor, full_summary, static_map_url=None):

    client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

    # Try WhatsApp first
    try:
        message = client.messages.create(
            from_=f"whatsapp:{os.getenv('TWILIO_WHATSAPP_NUMBER')}",
            to=f"whatsapp:{target_contractor}",
            body=full_summary,
            media_url=[static_map_url] if static_map_url else None
        )
        return {"channel": "whatsapp", "sid": message.sid, "status": message.status}

    except Exception as whatsapp_error:
        print(f"WhatsApp send failed: {whatsapp_error}")

        # Fallback to SMS/MMS
        try:
            message = client.messages.create(
                from_=os.getenv("TWILIO_SMS_NUMBER"),  # plain E.164 number, no 'whatsapp:' prefix
                to=target_contractor,
                body=full_summary,
                media_url=[static_map_url] if static_map_url else None
            )
            return {"channel": "sms", "sid": message.sid, "status": message.status}

        except Exception as sms_error:
            print(f"SMS fallback also failed: {sms_error}")
            return {"channel": None, "error": str(sms_error)}
       

async def upload_to_tmp(image_bytes: bytes) -> str:
    """Uploads bytes to a temporary public URL so Twilio can fetch it."""
    try:
        async with httpx.AsyncClient() as client:
            files = {'file': ('incident.jpg', image_bytes, 'image/jpeg')}
            response = await client.post("https://tmpfiles.org/api/v1/upload", files=files)
            if response.status_code == 200:
                data = response.json()
                url = data['data']['url']
                # Convert view URL to download URL for Twilio
                return url.replace("https://tmpfiles.org/", "https://tmpfiles.org/dl/")
    except Exception as e:
        print(f"Temporary upload failed: {e}")
    return None


async def send_whatsapp_message(to: str, payload_type: str = "text", content: dict = None, sender_override: str = None, wbot_url: str = None, raw_jid: str = None):
    """
    Helper to send messages via local wbot API.
    raw_jid: the original Baileys JID (e.g. 15015860002951@lid) for routing to linked-device contacts.
    """
    to_number = clean_whatsapp_number(to)
    
    print(f"DEBUG: send_whatsapp_message called. to_number={to_number}, raw_jid={raw_jid}, type={payload_type}, content={content}")
    
    headers = {
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            data = {"number": to_number}
            # Pass rawJid so server.js can send to @lid contacts directly
            if raw_jid:
                data["rawJid"] = raw_jid
            
            if payload_type == "text":
                data["text"] = content.get("body", "")
            elif payload_type == "image":
                data["imageUrl"] = content.get("link", "")
                data["caption"] = content.get("caption", "")
            elif payload_type == "template":
                data["text"] = content.get("body", f"Plumbing Emergency Alert for {to_number}")
            elif payload_type == "buttons":
                data["text"] = content.get("body", "")
                data["buttons"] = content.get("buttons", [])
            else:
                print("DEBUG: Invalid payload_type specified.")
                return False

            target_api_url = wbot_url.rstrip("/") if wbot_url else WBOT_API_URL
            print(f"DEBUG: Target API endpoint determined: {target_api_url}/send")

            # Retry loop for Render cold starts
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    print(f"  → Attempt {attempt}/{max_retries}: POST {target_api_url}/send with data: {data}")
                    response = await client.post(f"{target_api_url}/send", headers=headers, json=data)

                    if response.status_code in [200, 201]:
                        print(f"✅ wbot Send Success: {response.status_code}")
                        return True
                    elif response.status_code in [429, 503, 403, 400]:
                        # CRITICAL: Do NOT retry on rate limit, service unavailable, forbidden, or bad request.
                        # Retrying just hammers the server and creates log spam.
                        print(f"🚫 wbot API rejected (no retry): {response.status_code} - {response.text[:200]}")
                        return False
                    else:
                        print(f"⚠️ wbot API Error: {response.status_code} - {response.text[:200]}")
                        if attempt < max_retries:
                            import asyncio
                            await asyncio.sleep(5)
                except Exception as retry_err:
                    print(f"⚠️ Attempt {attempt} failed: {retry_err}")
                    if attempt < max_retries:
                        import asyncio
                        await asyncio.sleep(5)
            
            print("❌ All retry attempts to wbot /send failed.")
            return False
                
    except Exception as e:
        print(f"❌ wbot Send Error: {e}")
        return False

# CHANGED: Added location parameter to the function signature
# CHANGED: Added customer_name parameter to the signature logic block
async def process_incoming_incident(
    customer_phone: str, 
    body: str, 
    location: str = None, 
    customer_name: str = None,
    media_url: str = None, 
    sender_override: str = None, 
    image_bytes: bytes = None, 
    contractor_override: str = None,
    demo: bool = False,
    professional_type: str = 'contractor',      # ← カンマ追加！
    dispatcher_email: str = None,             # ← 追加
    dispatcher_name: str = None              # ← 追加
):
    """
    Core logic to handle an incoming plumbing request.
    """
    print(f"Processing incident from {customer_name or 'Unknown'} ({customer_phone}) | Demo Mode: {demo}")
    
    # 0. Contractor Lookup
    target_contractor = None
    contractor_obj = None                       # ← 追加：後でメール送るときに使う
    if contractor_override:
        print(f"contractor override is: {contractor_override}")
        if str(contractor_override).startswith("+") or str(contractor_override).startswith("whatsapp:"):
            target_contractor = contractor_override
        else:
            from database import get_contractor_by_id
            contractor_obj = get_contractor_by_id(contractor_override)
            print(f"contractor override from DB is : {contractor_obj}")
            if contractor_obj:
                target_contractor = contractor_obj.plumber_phone
                print(f"📍 Routed to Contractor: {contractor_obj.name} ({target_contractor})")
            else:
                print(f"⚠️ Contractor ID '{contractor_override}' not found in DB.")
    
    if not target_contractor:
        target_contractor = CONTRACTOR_NUMBER
        print(f"contractor number from env is : {target_contractor}")
        if not target_contractor:
            target_contractor = "385919293138" 
            print(f"contractor number from fallback is : {target_contractor}")
        print(f"ℹ️ Routing to target contractor: {target_contractor}")

    # Guard: never let the contractor alert route back to the customer's own number
    def _digits_only(n):
        return "".join(c for c in str(n) if c.isdigit()) if n else ""

    if target_contractor and _digits_only(target_contractor) == _digits_only(customer_phone):
        print(f"⚠️ target_contractor resolved to the same number as customer_phone "
              f"({target_contractor}) — check contractor_id / CONTRACTOR_WHATSAPP_NUMBER config. "
              f"Skipping contractor notification for this incident.")
        target_contractor = None
    
    contractor_language = "English"
    if contractor_obj and hasattr(contractor_obj, 'language') and contractor_obj.language:
        contractor_language = contractor_obj.language

    # 1. AI Triage
    triage_result = await analyze_triage(body, media_url, image_bytes, demo=demo, professional_type=professional_type, language=contractor_language)
    urgency = triage_result.get("urgency", "MEDIUM")
    summary = triage_result.get("summary", "No summary available")

    # Safety gate
    if not demo and (not customer_name and not location):
        print("🔕 Notification suppressed: no customer_name or location and not demo")
        return triage_result, False
    
    # 2. Log to Database
    ai_engine_used = triage_result.get("ai_engine", "Unknown")
    
    gear_data = triage_result.get("gear", "Standard diagnostic kit")
    if isinstance(gear_data, list):
        gear_str = ", ".join(str(item) for item in gear_data)
    else:
        gear_str = str(gear_data) if gear_data else "Standard diagnostic kit"

    log_incident(
        customer_phone=customer_phone,
        plumber_phone=target_contractor,
        urgency=urgency,
        summary=summary,
        raw_message=body,
        location=location,
        customer_name=customer_name,  
        image_url=media_url,
        ai_engine=ai_engine_used,
        gear=gear_str
    )

    # 3. Notification to Contractor
    notification_sent = False
    try:
        temp_url = None
        if image_bytes and not media_url:
            print("Encoding image to base64 for direct WhatsApp transfer...")
            import base64
            base64_str = base64.b64encode(image_bytes).decode('utf-8')
            temp_url = f"data:image/jpeg;base64,{base64_str}"
        
        target_media_url = media_url or temp_url

        urgency_emoji = "🚨" if urgency == "HIGH" else "⚠️" if urgency == "MEDIUM" else "🟢"
        
        location_text = location if location else "Not provided"
        name_text = customer_name if customer_name else "Not provided"
        encoded_address = urllib.parse.quote_plus(location_text)

        google_maps_link = f"https://maps.google.com/?q={encoded_address}"
        apple_maps_link = f"https://maps.apple.com/?q={encoded_address}"
        
        phone_number = customer_phone if customer_phone.startswith("+") else f"+{customer_phone}"
        if gear_str and gear_str.strip():
            gear_items = [item.strip() for item in gear_str.split(",") if item.strip()]
            formatted_gear = "\n".join([f"• {item}" for item in gear_items])
        else:
            formatted_gear = "• None specified"

        lines = [
            f"{urgency_emoji} *{urgency.upper()} URGENCY ALERT*",
            "",
            "*CLIENT DETAILS*",
            f"> *Name:* {name_text}",
            f"> *Phone:* {phone_number}",
            f"> *Location:* {location_text}",
            "",
            "📍 *ONE-TAP NAVIGATION*",
            f"Google Maps: _{google_maps_link}_",
            f"Apple Maps: _{apple_maps_link}_",
            "",
            "*JOB OVERVIEW*",
            f"{summary}",
            "",
            "*RECOMMENDED GEAR*",
            formatted_gear
        ]

        full_summary = "\n".join(lines)

        # ============================================
        # 🔥 EMAIL DISPATCH（WhatsAppとは独立させる）
        # ============================================
        if dispatcher_email:
            subject = f"NEW INCIDENT: {professional_type.capitalize()} Required"
            text_content = f"""New issue reported by {customer_name or 'Customer'}.
Details: {body}
Location: {location or 'Unknown'}
Urgency: {urgency}
Gear: {gear_str}"""

            html_content = f"""
            <html>
            <body>
                <h2>New Incident Alert</h2>
                <p><strong>Dear {dispatcher_name or 'Dispatcher'},</strong></p>
                <p>You got a new incident:</p>
                <p><strong>Customer:</strong> {customer_name or 'Unknown'} ({customer_phone})</p>
                <p><strong>Type:</strong> {professional_type}</p>
                <p><strong>Location:</strong> {location or 'Not provided'}</p>
                <p><strong>Urgency:</strong> {urgency}</p>
                <p><strong>Details:</strong> {body}</p>
                <p><strong>Gear:</strong> {gear_str}</p>
                <hr>
                <p><a href="{google_maps_link}">Open in Google Maps</a></p>
            </body>
            </html>
            """

            # 非同期でメール送信（ブロックせえへんように create_task で投げる）
            asyncio.create_task(
                send_email_async(
                    to_email=dispatcher_email,
                    subject=subject,
                    body_text=text_content,
                    body_html=html_content
                )
            )
            print(f"📧 Email dispatch queued for {dispatcher_email}")
        # ============================================

        # --- WHATSAPP DISPATCH ---
        if not target_contractor:
            print("🔕 Contractor WhatsApp notification skipped (no valid contractor target).")
        elif target_media_url:
            await send_whatsapp_message(
                to=target_contractor,
                payload_type="image",
                content={"link": target_media_url, "caption": full_summary},
                sender_override=sender_override
            )
            notification_sent = True
        else:
            await send_whatsapp_message(
                to=target_contractor,
                payload_type="text",
                content={"body": full_summary},
                sender_override=sender_override
            )
            notification_sent = True
            
    except Exception as e:
        print(f"Failed to notify contractor: {e}")

    return triage_result, notification_sent
