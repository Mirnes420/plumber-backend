import os
from twilio.rest import Client as TwilioClient
from ai_engine import analyze_triage
from database import log_incident
from dotenv import load_dotenv

load_dotenv()

# Twilio Config (Strip to avoid issues with trailing spaces in .env)
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "").strip()
PLUMBER_NUMBER = os.getenv("PLUMBER_WHATSAPP_NUMBER", "").strip()

twilio_client = TwilioClient(TWILIO_SID, TWILIO_AUTH_TOKEN)

async def process_incoming_incident(customer_phone: str, body: str, media_url: str = None):
    """
    Core logic to handle an incoming plumbing request:
    1. AI Analysis
    2. DB Logging
    3. Notification (For ALL incidents, with priority label)
    Returns: (triage_result, notification_sent)
    """
    # 1. AI Triage
    triage_result = await analyze_triage(body, media_url)
    urgency = triage_result.get("urgency", "MEDIUM")
    summary = triage_result.get("summary", "No summary available")
    
    # 2. Log to Database
    log_incident(
        customer_phone=customer_phone,
        plumber_phone=PLUMBER_NUMBER,
        urgency=urgency,
        summary=summary,
        raw_message=body,
        image_url=media_url
    )

    # 3. Notification Logic (Fixing parameter conflicts)
    icon = "🚨 EMERGENCY" if urgency == "HIGH" else "📅 MAINTENANCE"
    alert_body = f"{icon} Alert: {summary} from {customer_phone}"
    
    content_sid = os.getenv("TWILIO_CONTENT_SID")
    
    notification_sent = False
    try:
        import json
        # Base payload
        payload = {
            "from_": TWILIO_NUMBER,
            "to": PLUMBER_NUMBER,
        }
        
        # Attach media to top-level if present (helps Twilio process it)
        if media_url:
            payload["media_url"] = [media_url]
        
        # 1. Content SID Path (Templates)
        if content_sid:
            payload["content_sid"] = content_sid.strip()
            # Pass named variable as seen in screenshot: {{customer_phone}}
            payload["content_variables"] = json.dumps({
                "customer_phone": customer_phone.replace("whatsapp:", "")
            })
        
        # 2. Interactive Buttons Path (Fallback)
        else:
            payload["body"] = alert_body # Required for interactive messages
            
            interactive_data = {
                "type": "button",
                "header": {"type": "text", "text": f"{icon} Alert"},
                "body": {"text": alert_body},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": "urgent", "title": "Urgent"}},
                        {"type": "reply", "reply": {"id": "not_urgent", "title": "Not Urgent"}},
                        {"type": "reply", "reply": {"id": "all_tasks", "title": "All Tasks"}}
                    ]
                }
            }
            # Also put in interactive header for UI consistency
            if media_url:
                interactive_data["header"] = {"type": "image", "image": {"link": media_url}}
            
            payload["persistent_action"] = [json.dumps(interactive_data)]

        twilio_client.messages.create(**payload)
        notification_sent = True
    except Exception as e:
        print(f"Failed to notify plumber: {e}")

    return triage_result, notification_sent
