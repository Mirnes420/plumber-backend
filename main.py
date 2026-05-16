import os
from fastapi import FastAPI, Request, Response, HTTPException, Form, UploadFile, File
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
import json
from ai_engine import analyze_triage
from database import log_incident

from shared_logic import send_whatsapp_message, process_incoming_incident

load_dotenv()

app = FastAPI(title="WhatsApp Emergency Triage Bot")

# WBOT Config
WBOT_API_URL = os.getenv("WBOT_API_URL", "http://localhost:3001").rstrip("/")
PLUMBER_NUMBER = os.getenv("PLUMBER_WHATSAPP_NUMBER")

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
        try:
            json_data = await request.json()
            customer_phone = json_data.get("From")
            body_raw = json_data.get("Body", "").strip()
        except:
            pass
            
    if not customer_phone:
        return JSONResponse({"status": "error", "message": "No sender phone found"})
        
    body_upper = body_raw.upper().replace(" ", "_").strip()
    print(f"📥 Received from {customer_phone}: {body_upper}")

    # Handle Commands (Filtering in WhatsApp Chat)
    filter_keywords = ["URGENT", "NOT_URGENT", "ALL_TASKS", "EMERGENCY", "NON_EMERGENCY", "NO_EMERGENCY", "FILTER", "MID", "ALL"]
    
    if body_upper in filter_keywords:
        from database import get_incidents
        incidents = get_incidents()

        if body_upper in ["URGENT", "EMERGENCY"]:
            print("🚨 Routing to High-Priority Alert Pipeline")
            filtered = [i for i in incidents if i['urgency'] == "HIGH"][:5]
            title = "*🚨 Recent Urgent Tasks*"
        elif body_upper in ["NOT_URGENT", "NON_EMERGENCY", "NO_EMERGENCY"]:
            print("✅ Routing to Standard Log Archive")
            filtered = [i for i in incidents if i['urgency'] != "HIGH"][:5]
            title = "*✅ Non-Urgent Tasks*"
        else:  # ALL_TASKS, FILTER, MID, ALL
            print(f"⚡ Routing to Context Filter: {body_upper}")
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

        # Send message with dynamic variables and buttons
        await send_whatsapp_message(
            to=customer_phone,
            payload_type="buttons",
            content={
                "body": msg_text,
                "buttons": ["Emergency", "No Emergency", "All"]
            }
        )

        return JSONResponse({"status": "ok"})

    # 2. Handle New Incidents
    print(f"💬 Standard message or unmapped keyword: {body_upper}. Routing to AI Triage.")
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

    if urgency == "HIGH":
        reply_msg = f"🚨 *EMERGENCY DETECTED*\n\nWe've flagged this as high priority: {summary}\n\nA plumber is being paged now."
    else:
        reply_msg = f"✅ *Request Received*\n\nSummary: {summary}\n\nThis has been logged. We will contact you shortly."

    await send_whatsapp_message(
        to=customer_phone,
        payload_type="text",
        content={"body": reply_msg}
    )

    return JSONResponse({"status": "ok"})

@app.post("/api/incident")
async def api_incident(
    phone: str = Form(...),
    description: str = Form(...),
    image: UploadFile = File(None)
):
    print(f"🌐 Web Form Submission from {phone}: {description}")
    
    image_bytes = None
    if image and image.filename:
        image_bytes = await image.read()
        
    from shared_logic import process_incoming_incident
    triage_result, _ = await process_incoming_incident(
        phone, description, None, 
        sender_override=None,
        plumber_override=None,
        image_bytes=image_bytes
    )
    
    urgency = triage_result.get("urgency", "MEDIUM")
    summary = triage_result.get("summary", "")

    if urgency == "HIGH":
        reply_msg = f"🚨 *EMERGENCY DETECTED*\n\nWe received your web request. We've flagged this as high priority: {summary}\n\nA plumber is being paged now!"
    else:
        reply_msg = f"✅ *Request Received*\n\nSummary: {summary}\n\nThis has been logged from the web form. We will contact you shortly."

    # Automatically send the confirmation back to the user on WhatsApp!
    await send_whatsapp_message(
        to=phone,
        payload_type="text",
        content={"body": reply_msg}
    )

    return JSONResponse({"status": "success", "urgency": urgency, "summary": summary})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
