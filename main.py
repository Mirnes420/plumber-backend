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
file_urgency_template_id = os.getenv("FILE_URGENCY_TEMPLATE_ID")
contact_customer_template_id = os.getenv("CONTACT_CUSTOMER_TEMPLATE_ID")

@app.get("/")
async def root():
    return {"status": "running", "service": "Plumbing Triage Bot"}

@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    form_data = await request.form()
    customer_phone = form_data.get("From")
    body_raw = form_data.get("Body", "").strip()
    body_upper = body_raw.upper()

    twiml_resp = MessagingResponse()

    # Handle Commands (Filtering in WhatsApp Chat)
    body_lower = body_raw.lower()
    if body_lower in ["urgent", "not_urgent", "all_tasks"]:
        from database import get_incidents
        incidents = get_incidents()

        if body_lower == "urgent":
            filtered = [i for i in incidents if i['urgency'] == "HIGH"][:5]
            title = "*🚨 Recent Urgent Tasks*"
        elif body_lower == "not_urgent":
            filtered = [i for i in incidents if i['urgency'] != "HIGH"][:5]
            title = "*✅ Non-Urgent Tasks*"
        else:  # all_tasks
            filtered = incidents[:5]
            title = "*📋 All Recent Tasks*"
        
        # ... (rest of filtering logic)
        if not filtered:
            msg_text = f"{title}\nNo tasks found."
        else:
            msg_text = f"{title}\n\n"
            for i in filtered:
                msg_text += f"• [{i['urgency']}] {i['summary']}\n  Phone: {i['customer_phone']}\n\n"

        # Send interactive message using Template ID if available
        payload = {
            "from_": TWILIO_NUMBER,
            "to": customer_phone,
        }
        
        if file_urgency_template_id:
            payload["content_sid"] = file_urgency_template_id.strip()
            # Pass variable if template body has {{1}}
            payload["content_variables"] = json.dumps({"1": msg_text})
        else:
            # Fallback to manual interactive buttons
            payload["body"] = msg_text
            payload["persistent_action"] = [json.dumps(interactive_data)]

        twilio_client.messages.create(**payload)
        return Response(status_code=200)

    # 2. Handle New Incidents
    incoming_media = form_data.get("MediaUrl0")
    from shared_logic import process_incoming_incident
    triage_result, _ = await process_incoming_incident(customer_phone, body_raw, incoming_media)
    
    urgency = triage_result.get("urgency", "MEDIUM")
    summary = triage_result.get("summary", "")

    # Notify Plumber with the "Call Customer" template (NOT the customer)
    if contact_customer_template_id:
        twilio_client.messages.create(
            from_=TWILIO_NUMBER,
            to=PLUMBER_NUMBER, # Notification for the plumber
            content_sid=contact_customer_template_id.strip(),
            content_variables=json.dumps({
                "customer_phone": customer_phone.replace("whatsapp:", "")
            })
        )
    
    # Acknowledge the Customer
    twiml_resp = MessagingResponse()
    msg = twiml_resp.message()
    if urgency == "HIGH":
        msg.body(f"🚨 *EMERGENCY DETECTED*\n\nWe've flagged this as high priority. A plumber is being paged.")
    else:
        msg.body(f"✅ *Request Received*\n\nSummary: {summary}\n\nLogged. We will contact you shortly.")
    
    return Response(content=str(twiml_resp), media_type="application/xml")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
