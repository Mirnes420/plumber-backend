import os
from fastapi import FastAPI, Request, Response, HTTPException
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from dotenv import load_dotenv

from ai_engine import analyze_triage
from database import log_incident

load_dotenv()

app = FastAPI(title="WhatsApp Emergency Triage Bot")

# Twilio Config (Strip to avoid issues with trailing spaces in .env)
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "").strip()
PLUMBER_NUMBER = os.getenv("PLUMBER_WHATSAPP_NUMBER", "").strip()

twilio_client = TwilioClient(TWILIO_SID, TWILIO_AUTH_TOKEN)

@app.get("/")
async def root():
    return {"status": "running", "service": "Plumbing Triage Bot"}

@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    form_data = await request.form()
    customer_phone = form_data.get("From")
    body_raw = form_data.get("Body", "").strip()
    body_upper = body_raw.upper()
    incoming_media = form_data.get("MediaUrl0")
    
    twiml_resp = MessagingResponse()

    # 1. Handle Commands (Filtering in WhatsApp Chat)
    if body_upper in ["URGENT", "NOT_URGENT", "ALL_TASKS"]:
        from database import get_incidents
        incidents = get_incidents()
        
        if body_upper == "URGENT":
            filtered = [i for i in incidents if i['urgency'] == "HIGH"][:5]
            title = "*🚨 Recent Urgent Tasks*"
        elif body_upper == "NOT_URGENT":
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
                msg_text += f"• [{i['urgency']}] {i['summary']}\n  Phone: {i['customer_phone']}\n\n"
        
        # Send interactive message via REST API
        import json
        content_sid = os.getenv("TWILIO_CONTENT_SID")
        
        payload = {
            "from_": TWILIO_NUMBER,
            "to": customer_phone,
        }

        if content_sid:
            payload["content_sid"] = content_sid
            # payload["content_variables"] = json.dumps({"1": msg_text})
        else:
            payload["body"] = msg_text if msg_text.strip() else "No tasks found."
            interactive_data = {
                "type": "button",
                "header": {"type": "text", "text": title},
                "body": {"text": payload["body"]},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": "urgent", "title": "Urgent"}},
                        {"type": "reply", "reply": {"id": "not_urgent", "title": "Not Urgent"}},
                        {"type": "reply", "reply": {"id": "all_tasks", "title": "All Tasks"}}
                    ]
                }
            }
            payload["persistent_action"] = [json.dumps(interactive_data)]

        twilio_client.messages.create(**payload)
        
        return Response(content="", media_type="application/xml")

    # 2. Handle New Incidents
    from shared_logic import process_incoming_incident
    triage_result, _ = await process_incoming_incident(customer_phone, body_raw, incoming_media)
    
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
