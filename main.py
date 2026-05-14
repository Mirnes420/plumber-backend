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
async def whatsapp_webhook(request: Request):
    form_data = await request.form()
    customer_phone = form_data.get("From")
    body_raw = form_data.get("Body", "").strip()
    body_upper = body_raw.upper()
    incoming_media = form_data.get("MediaUrl0")
    
    twiml_resp = MessagingResponse()

    # 1. Handle Commands (Filtering in WhatsApp Chat)
    if body_upper in ["urgent", "not_urgent", "all_tasks"]:
        from database import get_incidents
        incidents = get_incidents()
        
        if body_upper == "urgent":
            filtered = [i for i in incidents if i['urgency'] == "HIGH"][:5]
            title = "*🚨 Recent Urgent Tasks*"
        elif body_upper == "not_urgent":
            filtered = [i for i in incidents if i['urgency'] != "HIGH"][:5]
            title = "*✅ Non-Urgent Tasks*"
        elif body_upper == "all_tasks":
            filtered = incidents[:5]
            title = "*📋 All Tasks*"
        
        if not filtered:
            twiml_resp.message(f"{title}\nNo tasks found.")
        else:
            msg_text = f"{title}\n\n"
            for i in filtered:
                time_str = i['timestamp'].strftime("%H:%M") if hasattr(i['timestamp'], 'strftime') else str(i['timestamp'])[:5]
                msg_text += f"• [{i['urgency']}] {i['summary']}\n  Phone: {i['customer_phone']}\n\n"
            twiml_resp.message(msg_text)
        
        return Response(content=str(twiml_resp), media_type="application/xml")

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
