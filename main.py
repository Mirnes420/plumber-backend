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
    # Parse form data from Twilio
    form_data = await request.form()
    
    customer_phone = form_data.get("From")
    body = form_data.get("Body", "")
    media_url = form_data.get("MediaUrl0")
    
    print(f"Received message from {customer_phone}: {body}")

    # Use shared logic
    from shared_logic import process_incoming_incident
    triage_result, notification_sent = await process_incoming_incident(customer_phone, body, media_url)
    
    urgency = triage_result.get("urgency", "MEDIUM")
    summary = triage_result.get("summary", "No summary available")

    # Response Logic for Twilio
    twiml_resp = MessagingResponse()
    
    if urgency == "HIGH":
        twiml_resp.message("🚨 We have detected this is an EMERGENCY. Our plumber has been alerted and will contact you immediately.")
    else:
        twiml_resp.message(f"Thank you for reaching out. We've logged your request regarding: {summary}. A member of our team will contact you shortly.")

    return Response(content=str(twiml_resp), media_type="application/xml")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
