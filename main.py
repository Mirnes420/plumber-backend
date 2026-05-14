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
    body = form_data.get("Body", "")
    # Twilio receives images as MediaUrl0
    incoming_media = form_data.get("MediaUrl0") 

    from shared_logic import process_incoming_incident
    triage_result, _ = await process_incoming_incident(customer_phone, body, incoming_media)
    
    urgency = triage_result.get("urgency", "MEDIUM")
    summary = triage_result.get("summary", "")

    twiml_resp = MessagingResponse()
    msg = twiml_resp.message()

    if urgency == "HIGH":
        msg.body(f"🚨 *EMERGENCY DETECTED*\n\nWe've flagged this as high priority: {summary}\n\nA plumber is being paged now.")
        # If you want to send an image BACK to them (e.g., a map or plumber photo):
        # msg.media("https://your-public-image-url.com/image.png") 
    else:
        msg.body(f"✅ *Request Received*\n\nSummary: {summary}\n\nThis has been logged as standard maintenance. We will contact you during business hours.")

    return Response(content=str(twiml_resp), media_type="application/xml")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
