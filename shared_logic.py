import os
from twilio.rest import Client as TwilioClient
from ai_engine import analyze_triage
from database import log_incident
from dotenv import load_dotenv

load_dotenv()

import json
import httpx

# Twilio Config
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID")
PLUMBER_NUMBER = os.getenv("PLUMBER_WHATSAPP_NUMBER", "").strip()

twilio_client = TwilioClient(TWILIO_SID, TWILIO_AUTH_TOKEN) if TWILIO_SID else None

# Meta Config
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()

META_API_URL = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_ID}/messages"

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

def ensure_whatsapp_prefix(number: str) -> str:
    if not number:
        return number
    
    # URL decoding often turns '+' into ' ', so we restore it
    number = str(number).strip().replace(" ", "+")
    
    if ":" in number:
        prefix, phone = number.split(":", 1)
        phone = phone.strip()
        if not phone.startswith("+"):
            phone = f"+{phone}"
        return f"whatsapp:{phone}"
    else:
        if not number.startswith("+"):
            number = f"+{number}"
        return f"whatsapp:{number}"

async def send_whatsapp_message(to: str, payload_type: str = "text", content: dict = None, sender_override: str = None):
    """
    Helper to send messages via Twilio (Priority) or Meta Cloud API.
    """
    from_number = ensure_whatsapp_prefix(sender_override or TWILIO_NUMBER)
    to_number = ensure_whatsapp_prefix(to)
    
    print(f"Attempting to send WhatsApp to {to_number} from {from_number} via {'Twilio' if twilio_client else 'Meta'}")
    
    # Try Twilio first if configured
    if twilio_client:
        try:
            if payload_type == "text":
                if MESSAGING_SERVICE_SID:
                    res = twilio_client.messages.create(
                        body=content.get("body", ""),
                        messaging_service_sid=MESSAGING_SERVICE_SID,
                        to=to_number
                    )
                else:
                    res = twilio_client.messages.create(
                        body=content.get("body", ""),
                        from_=from_number,
                        to=to_number
                    )
            elif payload_type == "image":
                if MESSAGING_SERVICE_SID:
                    res = twilio_client.messages.create(
                        body=content.get("caption", ""),
                        messaging_service_sid=MESSAGING_SERVICE_SID,
                        media_url=[content.get("link", "")] if content.get("link") else None,
                        to=to_number
                    )
                else:
                    res = twilio_client.messages.create(
                        body=content.get("caption", ""),
                        from_=from_number,
                        media_url=[content.get("link", "")] if content.get("link") else None,
                        to=to_number
                    )
            elif payload_type == "template":
                # Twilio Content Template API
                params = {
                    "to": to_number,
                    "content_sid": content.get("template", {}).get("name"), # We'll map 'name' to sid for compatibility
                    "content_variables": json.dumps(content.get("template", {}).get("variables", {}))
                }
                if MESSAGING_SERVICE_SID:
                    params["messaging_service_sid"] = MESSAGING_SERVICE_SID
                else:
                    params["from_"] = from_number
                
                res = twilio_client.messages.create(**params)
                print(f"Twilio Template Sent: {res.sid}")
                return True
            print(f"Twilio Message Sent (via Service: {MESSAGING_SERVICE_SID or from_number}): {res.sid}")
            return True
        except Exception as e:
            print(f"Twilio Send Error: {e}")

    # Fallback to Meta if Twilio fails or isn't configured
    if not WHATSAPP_PHONE_ID:
        return None

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": payload_type,
    }
    
    if payload_type == "text":
        data["text"] = {"body": content.get("body", "")}
    elif payload_type == "template":
        data["template"] = content.get("template", {})
    elif payload_type == "interactive":
        data["interactive"] = content.get("interactive", {})
    elif payload_type == "image":
        data["image"] = {"link": content.get("link", "")}
        if content.get("caption"):
            data["image"]["caption"] = content.get("caption")

    async with httpx.AsyncClient() as client:
        response = await client.post(META_API_URL, headers=headers, json=data)
        if response.status_code not in [200, 201]:
            print(f"Meta API Error: {response.status_code} - {response.text}")
        return response

async def process_incoming_incident(customer_phone: str, body: str, media_url: str = None, sender_override: str = None, image_bytes: bytes = None, plumber_override: str = None):
    """
    Core logic to handle an incoming plumbing request.
    """
    print(f"Processing incident from {customer_phone} for plumber {plumber_override or 'DEFAULT'}")
    
    # 0. Plumber Lookup
    target_plumber = None
    
    if plumber_override:
        # If it's already a phone number, use it
        if str(plumber_override).startswith("+") or str(plumber_override).startswith("whatsapp:"):
            target_plumber = plumber_override
        else:
            # Otherwise, lookup by ID/Slug in DB
            from database import get_plumber_by_id
            plumber_obj = get_plumber_by_id(plumber_override)
            if plumber_obj:
                target_plumber = plumber_obj.plumber_phone
                print(f"📍 Routed to Plumber: {plumber_obj.name} ({target_plumber})")
            else:
                print(f"⚠️ Plumber ID '{plumber_override}' not found in DB.")
    
    # Fallback to default if no valid plumber found yet
    if not target_plumber:
        target_plumber = PLUMBER_NUMBER
        print(f"ℹ️ Using default plumber number: {target_plumber}")
    
    # 1. AI Triage
    triage_result = await analyze_triage(body, media_url, image_bytes)
    urgency = triage_result.get("urgency", "MEDIUM")
    summary = triage_result.get("summary", "No summary available")
    
    # 2. Log to Database
    log_incident(
        customer_phone=customer_phone,
        plumber_phone=target_plumber,
        urgency=urgency,
        summary=summary,
        raw_message=body,
        image_url=media_url
    )

    # 3. Notification to Plumber
    notification_sent = False
    try:
        # If we have bytes but no URL (Customer App), upload it temporarily for Twilio
        temp_url = None
        if image_bytes and not media_url:
            print("Uploading local image for WhatsApp notification...")
            temp_url = await upload_to_tmp(image_bytes)
        
        target_media_url = media_url or temp_url
        
        # Select emoji based on urgency
        urgency_emoji = "🚨" if urgency == "HIGH" else "⚠️" if urgency == "MEDIUM" else "✅"
        full_summary = f"{urgency_emoji} NEW INCIDENT [{urgency}]: {summary}\nCustomer: {customer_phone}"

        if target_media_url:
            # Send ONE message with Image + Caption
            await send_whatsapp_message(
                to=target_plumber,
                payload_type="image",
                content={"link": target_media_url, "caption": full_summary},
                sender_override=sender_override
            )
        else:
            # Send ONE text message
            await send_whatsapp_message(
                to=target_plumber,
                payload_type="text" if twilio_client else "template",
                content={"body": full_summary} if twilio_client else {
                    "template": {
                        "name": os.getenv("CONTACT_CUSTOMER_TEMPLATE_NAME", "contact_customer"),
                        "language": {"code": "en_US"},
                        "components": [
                            {
                                "type": "button",
                                "sub_type": "url",
                                "index": "0",
                                "parameters": [{"type": "text", "text": customer_phone}]
                            }
                        ]
                    }
                },
                sender_override=sender_override
            )
        notification_sent = True
    except Exception as e:
        print(f"Failed to notify plumber: {e}")

    return triage_result, notification_sent
