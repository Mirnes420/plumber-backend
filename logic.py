import os
import sys
from ai_engine import analyze_triage
from database import log_incident
from dotenv import load_dotenv
import urllib.parse

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
        async with httpx.AsyncClient(timeout=60.0) as client:
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

            # Retry loop for Render cold starts
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    print(f"  → Attempt {attempt}/{max_retries}: POST {WBOT_API_URL}/send")
                    response = await client.post(f"{WBOT_API_URL}/send", headers=headers, json=data)

                    if response.status_code in [200, 201]:
                        print(f"✅ wbot Send Success: {response.status_code}")
                        return True
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
    plumber_override: str = None,
    demo: bool = False,
    professional_type: str = 'plumber'
):
    """
    Core logic to handle an incoming plumbing request.
    """
    print(f"Processing incident from {customer_name or 'Unknown'} ({customer_phone}) | Demo Mode: {demo}")
    
    # 0. Plumber Lookup
    target_plumber = None
    if plumber_override:
        if str(plumber_override).startswith("+") or str(plumber_override).startswith("whatsapp:"):
            target_plumber = plumber_override
        else:
            from database import get_plumber_by_id
            plumber_obj = get_plumber_by_id(plumber_override)
            if plumber_obj:
                target_plumber = plumber_obj.plumber_phone
                print(f"📍 Routed to Plumber: {plumber_obj.name} ({target_plumber})")
            else:
                print(f"⚠️ Plumber ID '{plumber_override}' not found in DB.")
    
    if not target_plumber:
        target_plumber = PLUMBER_NUMBER
        if not target_plumber:
            target_plumber = "385919293138" 
        print(f"ℹ️ Routing to target plumber: {target_plumber}")
    
    # 1. AI Triage
    triage_result = await analyze_triage(body, media_url, image_bytes, demo=demo, professional_type=professional_type)
    urgency = triage_result.get("urgency", "MEDIUM")
    summary = triage_result.get("summary", "No summary available")
    
    # 2. Log to Database
    ai_engine_used = triage_result.get("ai_engine", "Unknown")
    
    # 🔥 SANITIZATION SCRUBBER: Force gear data into a clean, flat string
    gear_data = triage_result.get("gear", "Standard diagnostic kit")
    if isinstance(gear_data, list):
        gear_str = ", ".join(str(item) for item in gear_data)
    else:
        gear_str = str(gear_data) if gear_data else "Standard diagnostic kit"

    log_incident(
        customer_phone=customer_phone,
        plumber_phone=target_plumber,
        urgency=urgency,
        summary=summary,
        raw_message=body,
        location=location,
        customer_name=customer_name,  
        image_url=media_url,
        ai_engine=ai_engine_used,
        gear=gear_str  # 🔥 Pass the clean string version here
    )

    # 3. Notification to Plumber
    notification_sent = False
    try:
        temp_url = None
        if image_bytes and not media_url:
            print("Encoding image to base64 for direct WhatsApp transfer...")
            import base64
            base64_str = base64.b64encode(image_bytes).decode('utf-8')
            temp_url = f"data:image/jpeg;base64,{base64_str}"
        
        target_media_url = media_url or temp_url

        urgency_emoji = "🚨🚨🚨🚨🚨" if urgency == "HIGH" else "⚠️⚠️⚠️" if urgency == "MEDIUM" else "🟢"
        
        # CHANGED: Formatted template strings to include name natively inside notifications
        location_text = location if location else "Not provided"
        name_text = customer_name if customer_name else "Not provided"
        encoded_address = urllib.parse.quote_plus(location_text)

        # 2. Construct cross-platform universal links
        google_maps_link = f"https://maps.google.com/?q={encoded_address}"
        apple_maps_link = f"https://maps.apple.com/?q={encoded_address}"
        
        full_summary = (
            f"{urgency_emoji} \n"
            f" *NEW EMERGENCY ALERT* [{urgency}]\n\n"
            f"👤 *Customer Name:* {name_text}\n"
            f"🏠 *Address:* {location_text}\n\n"
            
            f"📍 *Navigate (Google Maps):* {google_maps_link}\n"
            f"🍎 *Navigate (Apple Maps):* {apple_maps_link}\n\n"

            f"🛠️ *Issue:* {summary}\n\n"
            f"🔧🧰 *Recommended Tools/Parts:* {gear_str}\n\n"
            
            f"📞 *Phone:* {customer_phone if customer_phone.startswith('+') else f'+{customer_phone}'}"
        )

        if target_media_url:
            await send_whatsapp_message(
                to=target_plumber,
                payload_type="image",
                content={"link": target_media_url, "caption": full_summary},
                sender_override=sender_override
            )
        else:
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