import os
from fastapi import FastAPI, Request, Response, HTTPException
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from dotenv import load_dotenv
import json
from ai_engine import analyze_triage
from database import log_incident

load_dotenv()

app = FastAPI(title="WhatsApp Emergency Triage Bot")

# Twilio Config
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER") # e.g., whatsapp:+14155238886
PLUMBER_NUMBER = os.getenv("PLUMBER_WHATSAPP_NUMBER") # The plumber's real number

twilio_client = TwilioClient(TWILIO_SID, TWILIO_AUTH_TOKEN)

@app.get("/")
async def root():
    return {"status": "running", "service": "Plumbing Triage Bot"}

@app.post("/webhook")
@app.post("/webhook/") # Support both
async def whatsapp_webhook(request: Request):
    print(f"DEBUG: Webhook received! Method: {request.method}, URL: {request.url}")
    form_data = await request.form()
    customer_phone = form_data.get("From")
    body_raw = form_data.get("Body", "").strip()
    
    if not customer_phone:
        print("DEBUG: No 'From' field in form data!")
        # Try to see if it's JSON
        try:
            json_data = await request.json()
            print(f"DEBUG: Received JSON instead: {json_data}")
        except:
            pass
    body_upper = body_raw.upper()

    twiml_resp = MessagingResponse()

    # Handle Commands (Filtering in WhatsApp Chat)
    if body_upper in ["URGENT", "NOT_URGENT", "ALL_TASKS"]:
        from database import get_incidents
        incidents = get_incidents()

        if body_upper == "URGENT":
            filtered = [i for i in incidents if i['urgency'] == "HIGH"][:5]
            title = "*🚨 Recent Urgent Tasks*"
        elif body_upper == "NOT_URGENT":
            filtered = [i for i in incidents if i['urgency'] != "HIGH"][:5]
            title = "*✅ Non-Urgent Tasks*"
        else:  # ALL_TASKS
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

        # Build interactive Quick Reply buttons
        interactive_data = {
            "type": "button",
            "body": {"text": msg_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "URGENT", "title": "Urgent"}},
                    {"type": "reply", "reply": {"id": "NOT_URGENT", "title": "Not Urgent"}},
                    {"type": "reply", "reply": {"id": "ALL_TASKS", "title": "All Tasks"}}
                ]
            }
        }

        # Send interactive message with dynamic variables
        payload = {
            "from_": TWILIO_NUMBER,
            "to": customer_phone,
            "interactive": interactive_data,
            "content_variables": json.dumps({
                "customer_phone": customer_phone
            })
        }

        twilio_client.messages.create(**payload)

        return Response(status_code=200)

    # 2. Handle New Incidents
    incoming_media = form_data.get("MediaUrl0")
    sender_override = form_data.get("FromNumber")
    plumber_override = form_data.get("PlumberNumber")
    image_file = form_data.get("MediaFile")
    
    image_bytes = None
    if image_file:
        try:
            image_bytes = await image_file.read()
        except Exception as e:
            print(f"Error reading uploaded file: {e}")

    from shared_logic import process_incoming_incident
    triage_result, _ = await process_incoming_incident(
        customer_phone, body_raw, incoming_media, 
        sender_override=sender_override,
        plumber_override=plumber_override,
        image_bytes=image_bytes
    )
    
    urgency = triage_result.get("urgency", "MEDIUM")
    summary = triage_result.get("summary", "")

    msg = twiml_resp.message()
    if urgency == "HIGH":
        msg.body(f"🚨 *EMERGENCY DETECTED*\n\nWe've flagged this as high priority: {summary}\n\nA plumber is being paged now.")
    else:
        msg.body(f"✅ *Request Received*\n\nSummary: {summary}\n\nThis has been logged. We will contact you shortly.")

    return Response(content=str(twiml_resp), media_type="application/xml")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
