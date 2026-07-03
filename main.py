import os
import sys
import traceback

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

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from logic import send_whatsapp_message

# load our local environment from .env
load_dotenv()

app = FastAPI(title="WhatsApp Emergency Triage Bot")

# here you would set Wbot api url and your number from environment variables or defaults
WBOT_API_URL = os.getenv("WBOT_API_URL", "http://localhost:3001").rstrip("/")
PLUMBER_NUMBER = os.getenv("PLUMBER_WHATSAPP_NUMBER")

# health check 
@app.get("/")
async def root():
    print("DEBUG: Root health check hit!")
    return {"status": "running", "service": "Plumbing Triage Bot"}

# webhook endpoint, accepts slashes at the end
@app.post("/webhook")
@app.post("/webhook/") 
async def whatsapp_webhook(request: Request):
    print(f"\n=================== WEBHOOK INBOUND ===================")
    print(f"DEBUG: Method: {request.method} | URL: {request.url}")
    
    try:
        # Diagnostic check on incoming content types
        content_type = request.headers.get("content-type", "")
        print(f"DEBUG: Content-Type Header: {content_type}")
        
        customer_phone = None
        body_raw = ""
        form_data = None

        # Robust multi-format parser with explicit debugging
        if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            form_data = await request.form()
            print(f"DEBUG: Form data keys found: {list(form_data.keys())}")
            customer_phone = form_data.get("From")
            body_raw = form_data.get("Body", "").strip()
        else:
            try:
                json_data = await request.json()
                print(f"DEBUG: JSON payload found: {json_data}")
                customer_phone = json_data.get("From")
                body_raw = json_data.get("Body", "").strip()
            except Exception as json_err:
                print(f"DEBUG: Failed parsing as JSON: {str(json_err)}")

        if not customer_phone:
            print("❌ ERROR: Request received but no sender phone ('From') could be resolved.")
            return JSONResponse({"status": "error", "message": "No sender phone found"}, status_code=400)
            
        body_upper = body_raw.upper().replace(" ", "_").strip()
        print(f"📥 Processing text from {customer_phone}: '{body_raw}' (Normalized: {body_upper})")

        # 1. Handle Commands (Filtering in WhatsApp Chat)
        filter_keywords = ["URGENT", "NOT_URGENT", "ALL_TASKS", "EMERGENCY", "NON_EMERGENCY", "NO_EMERGENCY", "FILTER", "MID", "ALL"]
        
        if body_upper in filter_keywords:
            print(f"DEBUG: Keyword identified: {body_upper}. Executing database query.")
            from database import get_incidents
            incidents = get_incidents()
            print(f"DEBUG: Total database incidents retrieved: {len(incidents)}")

            # defining emergency emojis
            if body_upper in ["URGENT", "EMERGENCY"]:
                print("🚨 Routing to High-Priority Alert Pipeline")
                filtered = [i for i in incidents if i['urgency'] == "HIGH"][:5]
                title = "*🚨 Recent Urgent Tasks*"
            elif body_upper in ["NOT_URGENT", "NON_EMERGENCY", "NO_EMERGENCY"]:
                print("✅ Routing to Standard Log Archive")
                filtered = [i for i in incidents if i['urgency'] != "HIGH"][:5]
                title = "*✅ Non-Urgent Tasks*"
            else:  
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

            # FIXED: Payload type flipped to 'text' using our unbreakable keyword-based menus
            print(f"DEBUG: Dispatching compiled list back to {customer_phone} via wbot API")
            await send_whatsapp_message(
                to=customer_phone,
                payload_type="text",
                content={
                    "body": msg_text,
                    "buttons": ["Emergency", "No Emergency", "All"] # Left for fallback metadata compatibility
                }
            )
            print("✅ Webhook response processed clean.")
            return JSONResponse({"status": "ok"})

        # 2. Handle New Incidents
        print(f"延 Handling as conversational input. Sending to AI engine...")
        
        incoming_media = form_data.get("MediaUrl0") if form_data else None
        sender_override = form_data.get("FromNumber") if form_data else None
        plumber_override = form_data.get("PlumberNumber") if form_data else None
        image_file = form_data.get("MediaFile") if form_data else None
        
        image_bytes = None
        if image_file:
            try:
                image_bytes = await image_file.read()
                print(f"DEBUG: Attached image binary loaded size: {len(image_bytes)} bytes")
            except Exception as e:
                print(f"⚠️ Error reading file data streaming layers: {e}")

        from logic import process_incoming_incident
        print("DEBUG: Executing process_incoming_incident pipeline...")
        
        triage_result, _ = await process_incoming_incident(
            customer_phone, body_raw, incoming_media, 
            sender_override=sender_override,
            plumber_override=plumber_override,
            image_bytes=image_bytes
        )
        
        urgency = triage_result.get("urgency", "MEDIUM")
        summary = triage_result.get("summary", "")
        print(f"DEBUG: AI Processing Complete. Urgency: {urgency} | Summary length: {len(summary)}")

        # message to customer

        if urgency == "HIGH":
            reply_msg = f"🚨 *EMERGENCY DETECTED*\n\nWe've flagged this as high priority: {summary}\n\nA plumber is being paged now."
        else:
            reply_msg = f"✅ *Request Received*\n\nSummary: {summary}\n\nThis has been logged. We will contact you shortly."

        await send_whatsapp_message(
            to=customer_phone,
            payload_type="text",
            content={"body": reply_msg}
        )
        print("✅ New incident tracked and confirmed successfully.")
        return JSONResponse({"status": "ok"})

    except Exception as global_err:
        print(f"❌ CRITICAL WEBHOOK EXCEPTION CRASH:")
        print("".join(traceback.format_exception(type(global_err), global_err, global_err.__traceback__)))
        sys.stdout.flush() # Force log buffer write immediately into Render's stream
        return JSONResponse({"status": "internal_error", "detail": str(global_err)}, status_code=500)

"""
we will need something like this for line integration

from fastapi import Request
from fastapi.responses import JSONResponse
import httpx
import os

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

@app.post("/line/webhook")
async def line_webhook(request: Request):
    body = await request.json()
    print("LINE webhook event:", body)

    # LINE sends events in an array under "events"
    for event in body.get("events", []):
        if event.get("type") == "message":
            user_id = event["source"]["userId"]
            text = event["message"].get("text", "")

            # Example: echo back the message
            reply_token = event["replyToken"]
            reply_payload = {
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": f"You said: {text}"}]
            }

            headers = {
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json"
            }
            async with httpx.AsyncClient() as client:
                await client.post(
                    "https://api.line.me/v2/bot/message/reply",
                    headers=headers,
                    json=reply_payload
                )

    return JSONResponse({"status": "ok"})
"""
# here we handle new incidents
@app.post("/api/incident")
async def api_incident(
    phone: str = Form(...),
    description: str = Form(...),
    location: str = Form(None),
    customer_name: str = Form(None),
    plumber_id: str = Form(None),
    image: UploadFile = File(None),
    demo: str = Form(None),
    professional_type: str = Form(None)
):

    # check if demo mode is enabled
    print(f"\n=================== WEB FORM INBOUND ===================")
    is_demo = (demo == "true")
    print(f"🌐 Submission processing for destination endpoint: {phone} | Client: {customer_name or 'Unknown'} | Plumber: {plumber_id} | Type: {professional_type or 'plumber'} | Demo Mode: {is_demo}")
    
    try:
        image_bytes = None
        if image and image.filename:
            image_bytes = await image.read()
            print(f"DEBUG: Web form binary attachment detected: {image.filename} ({len(image_bytes)} bytes)")
            
        from logic import process_incoming_incident
        
        # CHANGED: Passed location and customer_name as keyword arguments into your processing routine
        triage_result, _ = await process_incoming_incident(
            customer_phone=phone, 
            body=description, 
            location=location,
            customer_name=customer_name,
            media_url=None, 
            sender_override=None,
            plumber_override=plumber_id,
            image_bytes=image_bytes,
            demo=is_demo,
            professional_type=professional_type or 'plumber',
        )
        
        urgency = triage_result.get("urgency", "MEDIUM")
        summary = triage_result.get("summary", "")
        print(f"DEBUG: Web form AI evaluations resolved. Status level: {urgency}")

        if urgency == "HIGH":
            reply_msg = f"🚨 *EMERGENCY DETECTED*\n\nWe received your web request. We've flagged this as high priority: {summary}\n\nA plumber is being paged now!"
        else:
            reply_msg = f"✅ *Request Received*\n\nSummary: {summary}\n\nThis has been logged from the web form. We will contact you shortly."

        await send_whatsapp_message(
            to=phone,
            payload_type="text",
            content={"body": reply_msg}
        )
        gear_info = triage_result.get("gear", "Standard kit")
        if isinstance(gear_info, list):
            gear_info = ", ".join(str(x) for x in gear_info)

        print("✅ Web form registration complete.")
        # send the structured json as a message
        return JSONResponse({
            "status": "success", 
            "urgency": urgency, 
            "summary": summary,
            "gear": gear_info
        })

    except Exception as api_err:
        print(f"❌ CRITICAL API_INCIDENT EXCEPTION CRASH:")
        print("".join(traceback.format_exception(type(api_err), api_err, api_err.__traceback__)))
        sys.stdout.flush()
        return JSONResponse({"status": "error", "detail": str(api_err)}, status_code=500)
    

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)