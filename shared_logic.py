import os
from twilio.rest import Client as TwilioClient
from ai_engine import analyze_triage
from database import log_incident
from dotenv import load_dotenv

load_dotenv()

# Twilio Config
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
PLUMBER_NUMBER = os.getenv("PLUMBER_WHATSAPP_NUMBER")

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

    # 3. Notification Logic (Now for ALL incidents)
    icon = "🚨 EMERGENCY" if urgency == "HIGH" else "📅 MAINTENANCE"
    
    # We add "Buttons" as keywords that the plumber can reply with
    alert_msg = (
        f"{icon} ALERT\n\n"
        f"Priority: *{urgency}*\n"
        f"Summary: {summary}\n"
        f"From: {customer_phone}\n\n"
        f"--- Quick Actions ---\n"
        f"Reply *URGENT* to see emergencies\n"
        f"Reply *ALL* to see recent tasks\n\n"
        f"🔗 Dashboard: https://your-dashboard.com/?urgency=HIGH"
    )
    
    notification_sent = False
    try:
        # Pass the media_url in a list to Twilio
        # Note: Twilio's incoming media URLs work best when sent back as media_url
        payload = {"from_": TWILIO_NUMBER, "body": alert_msg, "to": PLUMBER_NUMBER}
        if media_url:
            payload["media_url"] = [media_url]
            
        twilio_client.messages.create(**payload)
        notification_sent = True
    except Exception as e:
        print(f"Failed to notify plumber: {e}")

    return triage_result, notification_sent
