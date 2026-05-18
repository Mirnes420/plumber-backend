import os
from ai_engine import analyze_triage
from database import log_incident
from dotenv import load_dotenv

load_dotenv()

import json
import httpx

# WBOT Config
WBOT_API_URL = os.getenv("WBOT_API_URL", "http://localhost:3001").rstrip("/")
PLUMBER_NUMBER = os.getenv("PLUMBER_WHATSAPP_NUMBER", "").strip()

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

def clean_whatsapp_number(number: str) -> str:
    if not number:
        return number
    # Remove prefix formatting
    number = str(number).strip()
    number = number.replace("whatsapp:", "").replace("+", "").replace(" ", "").replace("-", "")
    return number

async def send_whatsapp_message(to: str, payload_type: str = "text", content: dict = None, sender_override: str = None):
    """
    Helper to send messages via local wbot API.
    """
    to_number = clean_whatsapp_number(to)
    
    print(f"Attempting to send WhatsApp to {to_number} via wbot API")
    
    headers = {
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            data = {"number": to_number}
            
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
                return False

            response = await client.post(f"{WBOT_API_URL}/send", headers=headers, json=data)

            if response.status_code in [200, 201]:
                print(f"wbot Send Success: {response.status_code}")
                return True
            else:
                print(f"wbot API Error: {response.status_code} - {response.text}")
                return False
                
    except Exception as e:
        print(f"wbot Send Error: {e}")
        return False

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
        if not target_plumber:
            target_plumber = "me"
        print(f"ℹ️ Routing to target plumber: {target_plumber}")
    
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
        target_plumber = plumber_override or PLUMBER_NUMBER
        if not target_plumber:
            target_plumber = "me"
            
        print(f"ℹ️ Using default plumber number: {target_plumber}")

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
                payload_type="text",
                content={"body": full_summary},
                sender_override=sender_override
            )
        notification_sent = True
    except Exception as e:
        print(f"Failed to notify plumber: {e}")

    return triage_result, notification_sent
