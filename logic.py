import os
import sys
from ai_engine import analyze_triage, analyze_property_lead
from database import log_incident
from dotenv import load_dotenv
import urllib.parse
from twilio.rest import Client
import os
from database import log_property_lead 

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

load_dotenv()

import json
import httpx

# WBOT Config
WBOT_API_URL = os.getenv("WBOT_API_URL", "http://localhost:3001").rstrip("/")
PLUMBER_NUMBER = os.getenv("PLUMBER_WHATSAPP_NUMBER", "").strip()





# twillio implementation for sending messages to plumbers (for later)
def send_dispatch_alert(target_plumber, full_summary, static_map_url=None):

    client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

    # Try WhatsApp first
    try:
        message = client.messages.create(
            from_=f"whatsapp:{os.getenv('TWILIO_WHATSAPP_NUMBER')}",
            to=f"whatsapp:{target_plumber}",
            body=full_summary,
            media_url=[static_map_url] if static_map_url else None
        )
        return {"channel": "whatsapp", "sid": message.sid, "status": message.status}

    except Exception as whatsapp_error:
        print(f"WhatsApp send failed: {whatsapp_error}")

        # Fallback to SMS/MMS
        try:
            message = client.messages.create(
                from_=os.getenv("TWILIO_SMS_NUMBER"),  # plain E.164 number, no 'whatsapp:' prefix
                to=target_plumber,
                body=full_summary,
                media_url=[static_map_url] if static_map_url else None
            )
            return {"channel": "sms", "sid": message.sid, "status": message.status}

        except Exception as sms_error:
            print(f"SMS fallback also failed: {sms_error}")
            return {"channel": None, "error": str(sms_error)}
    
    
    

async def upload_to_tmp(image_bytes: bytes) -> str:
    """Uploads bytes to a temporary public URL so Twilio can fetch it."""
    try:
        async with httpx.AsyncClient() as client:
            files = {'file': ('incident.jpg', image_bytes, 'image/jpeg')}
            response = await client.post("https://tmpfiles.org/api/v1/upload", files=files)
            if response.status_code == 200:
                data = response.json()
                url = data['data']['url']
                # Convert view URL to download URL for Twilio
                return url.replace("https://tmpfiles.org/", "https://tmpfiles.org/dl/")
    except Exception as e:
        print(f"Temporary upload failed: {e}")
    return None

def clean_whatsapp_number(number: str) -> str:
    if not number:
        return number
    # Remove prefix formatting
    number = str(number).strip()
    number = number.replace("whatsapp:", "").replace("+", "").replace(" ", "").replace("-", "")
    return number

async def send_whatsapp_message(to: str, payload_type: str = "text", content: dict = None, sender_override: str = None, wbot_url: str = None, raw_jid: str = None):
    """
    Helper to send messages via local wbot API.
    raw_jid: the original Baileys JID (e.g. 15015860002951@lid) for routing to linked-device contacts.
    """
    to_number = clean_whatsapp_number(to)
    
    print(f"DEBUG: send_whatsapp_message called. to_number={to_number}, raw_jid={raw_jid}, type={payload_type}, content={content}")
    
    headers = {
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            data = {"number": to_number}
            # Pass rawJid so server.js can send to @lid contacts directly
            if raw_jid:
                data["rawJid"] = raw_jid
            
            if payload_type == "text":
                data["text"] = content.get("body", "")
            elif payload_type == "image":
                data["imageUrl"] = content.get("link", "")
                data["caption"] = content.get("caption", "")
            elif payload_type == "template":
                data["text"] = content.get("body", f"Plumbing Emergency Alert for {to_number}")
            elif payload_type == "buttons":
                data["text"] = content.get("body", "")
                data["buttons"] = content.get("buttons", [])
            else:
                print("DEBUG: Invalid payload_type specified.")
                return False

            target_api_url = wbot_url.rstrip("/") if wbot_url else WBOT_API_URL
            print(f"DEBUG: Target API endpoint determined: {target_api_url}/send")

            # Retry loop for Render cold starts
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    print(f"  → Attempt {attempt}/{max_retries}: POST {target_api_url}/send with data: {data}")
                    response = await client.post(f"{target_api_url}/send", headers=headers, json=data)

                    if response.status_code in [200, 201]:
                        print(f"✅ wbot Send Success: {response.status_code}")
                        return True
                    elif response.status_code in [429, 503, 403, 400]:
                        # CRITICAL: Do NOT retry on rate limit, service unavailable, forbidden, or bad request.
                        # Retrying just hammers the server and creates log spam.
                        print(f"🚫 wbot API rejected (no retry): {response.status_code} - {response.text[:200]}")
                        return False
                    else:
                        print(f"⚠️ wbot API Error: {response.status_code} - {response.text[:200]}")
                        if attempt < max_retries:
                            import asyncio
                            await asyncio.sleep(5)
                except Exception as retry_err:
                    print(f"⚠️ Attempt {attempt} failed: {retry_err}")
                    if attempt < max_retries:
                        import asyncio
                        await asyncio.sleep(5)
            
            print("❌ All retry attempts to wbot /send failed.")
            return False
                
    except Exception as e:
        print(f"❌ wbot Send Error: {e}")
        return False

# CHANGED: Added location parameter to the function signature
# CHANGED: Added customer_name parameter to the signature logic block
async def process_incoming_incident(
    customer_phone: str, 
    body: str, 
    location: str = None, 
    customer_name: str = None,
    media_url: str = None, 
    sender_override: str = None, 
    image_bytes: bytes = None, 
    plumber_override: str = None,
    demo: bool = False,
    professional_type: str = 'plumber'
):
    """
    Core logic to handle an incoming plumbing request.
    """
    print(f"Processing incident from {customer_name or 'Unknown'} ({customer_phone}) | Demo Mode: {demo}")
    
    # 0. Plumber Lookup
    target_plumber = None
    if plumber_override:
        if str(plumber_override).startswith("+") or str(plumber_override).startswith("whatsapp:"):
            target_plumber = plumber_override
        else:
            from database import get_plumber_by_id
            plumber_obj = get_plumber_by_id(plumber_override)
            if plumber_obj:
                target_plumber = plumber_obj.plumber_phone
                print(f"📍 Routed to Plumber: {plumber_obj.name} ({target_plumber})")
            else:
                print(f"⚠️ Plumber ID '{plumber_override}' not found in DB.")
    
    if not target_plumber:
        target_plumber = PLUMBER_NUMBER
        if not target_plumber:
            target_plumber = "385919293138" 
        print(f"ℹ️ Routing to target plumber: {target_plumber}")
    
    # 1. AI Triage
    triage_result = await analyze_triage(body, media_url, image_bytes, demo=demo, professional_type=professional_type)
    urgency = triage_result.get("urgency", "MEDIUM")
    summary = triage_result.get("summary", "No summary available")

    # Safety gate: if this is NOT a demo submission and we have no customer
    # identifying info (name or location), do not dispatch or notify the
    # plumber. This prevents operator/QA WhatsApp messages like "will it
    # work?" from being fed to the AI and generating false alerts.
    if not demo and (not customer_name and not location):
        print("🔕 Notification suppressed: no customer_name or location and not demo")
        return triage_result, False
    
    # 2. Log to Database
    ai_engine_used = triage_result.get("ai_engine", "Unknown")
    
    # 🔥 SANITIZATION SCRUBBER: Force gear data into a clean, flat string
    gear_data = triage_result.get("gear", "Standard diagnostic kit")
    if isinstance(gear_data, list):
        gear_str = ", ".join(str(item) for item in gear_data)
    else:
        gear_str = str(gear_data) if gear_data else "Standard diagnostic kit"

    log_incident(
        customer_phone=customer_phone,
        plumber_phone=target_plumber,
        urgency=urgency,
        summary=summary,
        raw_message=body,
        location=location,
        customer_name=customer_name,  
        image_url=media_url,
        ai_engine=ai_engine_used,
        gear=gear_str  # 🔥 Pass the clean string version here
    )

    # 3. Notification to Plumber
    notification_sent = False
    try:
            temp_url = None
            if image_bytes and not media_url:
                print("Encoding image to base64 for direct WhatsApp transfer...")
                import base64
                base64_str = base64.b64encode(image_bytes).decode('utf-8')
                temp_url = f"data:image/jpeg;base64,{base64_str}"
            
            target_media_url = media_url or temp_url

            urgency_emoji = "🚨" if urgency == "HIGH" else "⚠️" if urgency == "MEDIUM" else "🟢"
            
            # CHANGED: Formatted template strings to include name natively inside notifications
            location_text = location if location else "Not provided"
            name_text = customer_name if customer_name else "Not provided"
            encoded_address = urllib.parse.quote_plus(location_text)

            # 2. Construct cross-platform universal links
            google_maps_link = f"https://maps.google.com/?q={encoded_address}"
            apple_maps_link = f"https://maps.apple.com/?q={encoded_address}"
            
            full_summary = (
                "\n"
                "\n"
                f" *{urgency_emoji}NEW EMERGENCY ALERT* [{urgency}]\n\n"
                f"*Customer Name:* {name_text}\n"
                f"*Address:* {location_text}\n\n"
                
                f"*Navigate (Google Maps):* {google_maps_link}\n"
                f"*Navigate (Apple Maps):* {apple_maps_link}\n\n"

                f"*Issue:* {summary}\n\n"
                f"*Recommended Tools/Parts:* {gear_str}\n\n"
                
                f"*Phone:* {customer_phone if customer_phone.startswith('+') else f'+{customer_phone}'}"
                "\n"
                "\n"
            )

            if target_media_url:
                await send_whatsapp_message(
                    to=target_plumber,
                    payload_type="image",
                    content={"link": target_media_url, "caption": full_summary},
                    sender_override=sender_override
                )
            else:
                await send_whatsapp_message(
                    to=target_plumber,
                    payload_type="text",
                    content={"body": full_summary},
                    sender_override=sender_override
                )
            notification_sent = True
    except Exception as e:
        print(f"Failed to notify plumber: {e}")

    return triage_result, notification_sent



# --- PROPERTY LEAD DISPATCH LOGIC ---

async def process_property_lead(
    customer_phone: str,
    customer_name: str,
    property_id: str,
    budget: str,
    timeline: str,
    marketer_phone: str = None,
    language: str = None,
    raw_message: str = None
):
    target_marketer = clean_whatsapp_number(marketer_phone or "385919293138")
    clean_client_phone = clean_whatsapp_number(customer_phone)

    # 1. Send immediate engagement message to the buyer
    buyer_msg = (
        f"👋 Hi *{customer_name}*!\n\n"
        f"Thank you for inquiring about property *{property_id}*.\n"
        f"We've logged your preferences (Budget: {budget} | Timeline: {timeline}).\n\n"
        f"An agent will reach out to you on WhatsApp in under 60 seconds with the complete brochure and tour details!"
    )
    await send_whatsapp_message(to=clean_client_phone, payload_type="text", content={"body": buyer_msg})

    # 2. Format Marketer WhatsApp Action Card
    formatted_client_phone = f"+{clean_client_phone}" if not clean_client_phone.startswith("+") else clean_client_phone
    wa_direct_link = f"https://wa.me/{clean_client_phone}?text=Hi%20{urllib.parse.quote(customer_name)},%20I%20saw%20your%20inquiry%20for%20property%20{property_id}!"

    marketer_card = (
        f"\n🚨 *NEW HIGH-INTENT PROPERTY LEAD*\n\n"
        f"👤 *Buyer Name:* {customer_name}\n"
        f"🏢 *Property ID:* {property_id}\n"
        f"💰 *Budget:* {budget}\n"
        f"⏳ *Timeline:* {timeline}\n"
        f"📱 *Phone:* {formatted_client_phone}\n\n"
        f"⚡ *1-Tap Instant Connect:* {wa_direct_link}\n"
    )

    # 3. Dispatch to Marketer
    notification_sent = await send_whatsapp_message(
        to=target_marketer, payload_type="text", content={"body": marketer_card}
    )

    # 4. Log to Supabase
    log_property_lead(
        customer_phone=formatted_client_phone,
        customer_name=customer_name,
        property_id=property_id,
        budget=budget,
        timeline=timeline,
        marketer_phone=target_marketer,
        language=language,
        raw_message=raw_message,
        notification_sent=bool(notification_sent)
    )

    return {"status": "ok", "lead_summary": marketer_card}


# ─── REQUIREMENT 0: DYNAMIC PDF BROCHURE BUILDER ───
def build_property_pdf(property_id: str, title: str, address: str, description: str, budget_range: str, image_url: Optional[str] = None) -> str:
    """
    Builds a clean PDF brochure on the fly for registered properties.
    Saves the PDF to local uploads directory and returns the relative path.
    """
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors
    except ImportError:
        print("⚠️ reportlab is not installed. Skipping PDF generation.")
        return ""

    upload_dir = os.environ.get("UPLOAD_DIR", "/tmp/uploads")
    os.makedirs(upload_dir, exist_ok=True)
    pdf_filename = f"{property_id.lower()}_brochure.pdf"
    pdf_path = os.path.join(upload_dir, pdf_filename)

    try:
        doc = SimpleDocTemplate(pdf_path, pagesize=letter, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
        styles = getSampleStyleSheet()
        
        title_style = ParagraphStyle(
            'BrochureTitle', parent=styles['Heading1'],
            fontSize=24, leading=28, textColor=colors.HexColor('#0F172A'),
            spaceAfter=12
        )
        body_style = ParagraphStyle(
            'BrochureBody', parent=styles['Normal'],
            fontSize=11, leading=16, textColor=colors.HexColor('#334155'),
            spaceAfter=8
        )
        tag_style = ParagraphStyle(
            'BrochureTag', parent=styles['Normal'],
            fontSize=12, leading=14, textColor=colors.HexColor('#2563EB'),
            spaceAfter=15
        )

        story = []
        story.append(Paragraph(f"Property ID: {property_id.upper()}", tag_style))
        story.append(Paragraph(title, title_style))
        story.append(Paragraph(f"<b>Location:</b> {address}", body_style))
        story.append(Paragraph(f"<b>Budget/Price Range:</b> {budget_range or 'Contact Agent'}", body_style))
        story.append(Spacer(1, 15))

        if image_url:
            local_img_path = image_url
            if image_url.startswith("/uploads/"):
                local_img_path = os.path.join(upload_dir, image_url.replace("/uploads/", ""))
            
            if os.path.exists(local_img_path):
                try:
                    story.append(Image(local_img_path, width=450, height=250))
                    story.append(Spacer(1, 15))
                except Exception as img_err:
                    print(f"Error appending image to PDF: {img_err}")

        story.append(Paragraph("<b>Overview:</b>", styles['Heading3']))
        story.append(Paragraph(description or "No description provided.", body_style))
        story.append(Spacer(1, 40))
        story.append(Paragraph("<i>Dispatched instantly by Coherzo Lead System</i>", styles['Italic']))

        doc.build(story)
        return f"/uploads/{pdf_filename}"
    except Exception as e:
        print(f"❌ Error compiling PDF: {e}")
        return ""


# ─── REQUIREMENT 3: WHATSAPP BOT CHAT FLOW STATE MACHINE ───
import re
from typing import Optional
from database import SessionLocal, Property, PropertyManager, PropertyChatState

async def process_incoming_property_message(customer_phone: str, message_text: str, wbot_url: Optional[str] = None, from_jid: Optional[str] = None) -> bool:
    """
    Detects if an incoming message is a property inquiry.
    Sends dynamic PDF brochures, images, and drives conversational state questionnaire.
    from_jid: raw Baileys JID (e.g. 15015860002951@lid) used to correctly reply to linked-device contacts.
    """
    db = SessionLocal()
    resolved_wbot_url = wbot_url or os.getenv("NGROK_WBOT_URL") or WBOT_API_URL
    print(f"\n{'='*60}")
    print(f"📲 PROPERTY FLOW: Incoming from {customer_phone}")
    print(f"   message_text : {repr(message_text)}")
    print(f"   from_jid     : {from_jid}")
    print(f"   wbot_url     : {wbot_url}")
    print(f"   resolved_wbot: {resolved_wbot_url}")
    print(f"{'='*60}")

    try:
        clean_phone = clean_whatsapp_number(customer_phone)
        message_upper = message_text.upper()
        print(f"   clean_phone  : {clean_phone}")

        # Helper to send with jid routing
        async def _send(to, payload_type, content, is_customer=True):
            jid = from_jid if is_customer else None
            return await send_whatsapp_message(
                to=to,
                payload_type=payload_type,
                content=content,
                wbot_url=resolved_wbot_url,
                raw_jid=jid
            )

        # Helper to find property by ID or title keyword
        def find_property(message_upper_text):
            # 1. Strict ID regex: ATH-39
            m = re.search(r'[A-Z]{2,5}-\d{1,5}', message_upper_text)
            if m:
                pid = m.group(0)
                print(f"   Regex match: {pid} — querying DB by ID")
                p = db.query(Property).filter(Property.id == pid).first()
                if p:
                    return p, pid

            # 2. Try to match any word in the message against property IDs
            words = re.findall(r'[A-Z0-9]+', message_upper_text)
            for word in words:
                if len(word) >= 3:
                    p = db.query(Property).filter(Property.id == word).first()
                    if p:
                        print(f"   Word match by ID: {word}")
                        return p, word

            # 3. Fuzzy title match: check if any word appears in property titles
            all_props = db.query(Property).all()
            for word in words:
                if len(word) >= 4:
                    for p in all_props:
                        if word in p.title.upper():
                            print(f"   Title match: word={word} matched title='{p.title}'")
                            return p, p.id

            print(f"   No property match found for words: {words}")
            return None, None

        # Require exact trigger phrase to prevent hallucinations/false positives on random words
        is_initial = "INTERESTED IN PROPERTY" in message_upper
        prop, property_id = find_property(message_upper) if is_initial else (None, None)

        if prop:
            print(f"   ✅ Property found: id={prop.id} title={prop.title} | image={prop.image_url} | pdf={prop.pdf_url}")
            manager = db.query(PropertyManager).filter(PropertyManager.id == prop.manager_id).first()
            agent_phone = manager.phone if manager else os.getenv("DEFAULT_AGENT_PHONE", "385919293138")
            print(f"   Agent phone: {agent_phone}")

            # Save user's state in DB
            state = db.query(PropertyChatState).filter(PropertyChatState.phone == clean_phone).first()
            if not state:
                state = PropertyChatState(phone=clean_phone, current_property_id=property_id, state="awaiting_viewing")
                db.add(state)
            else:
                state.current_property_id = property_id
                state.state = "awaiting_viewing"
                state.viewing_answer = None
            db.commit()

            # Build absolute links for WhatsApp
            base_url = os.getenv("BACKEND_HOST_URL", "https://plumber-backend-fnh6.onrender.com").rstrip("/")

            pdf_link = prop.pdf_url
            if pdf_link and not pdf_link.startswith("http"):
                pdf_link = f"{base_url}{pdf_link if pdf_link.startswith('/') else '/' + pdf_link}"

            img_link = prop.image_url
            if img_link:
                first_img = img_link.split(",")[0].strip()
                if not first_img.startswith("http"):
                    img_link = f"{base_url}{first_img if first_img.startswith('/') else '/' + first_img}"

            print(f"   pdf_link: {pdf_link}")
            print(f"   img_link: {img_link}")
            print(f"   Sending image + welcome message to {clean_phone} (jid={from_jid})...")

            if img_link:
                img_ok = await _send(clean_phone, "image", {"link": img_link, "caption": f"Photos of {prop.title}"})
                print(f"   Image send result: {img_ok}")

            brochure_text = ""
            if pdf_link:
                brochure_text = f"\n📄 Download brochure: {pdf_link}"

            welcome_msg = (
                f"Hello! 👋 Thanks for your interest in *{prop.title}* ({property_id}).{brochure_text}\n\n"
                f"When are you available for a viewing?"
            )

            print(f"   Sending welcome message: {repr(welcome_msg)}")
            msg_ok = await _send(clean_phone, "text", {"body": welcome_msg})
            print(f"   Welcome message send result: {msg_ok}")
            return True

        # Check if there's an active state for this phone (they replied to a follow-up question)
        state = db.query(PropertyChatState).filter(PropertyChatState.phone == clean_phone).first()
        if state and state.state not in ("complete", None):
            print(f"   State machine: phone={clean_phone} state={state.state} property={state.current_property_id}")
            prop_id = state.current_property_id
            prop = db.query(Property).filter(Property.id == prop_id).first()
            manager = db.query(PropertyManager).filter(PropertyManager.id == prop.manager_id).first() if prop else None
            agent_phone = manager.phone if manager else os.getenv("DEFAULT_AGENT_PHONE", "385919293138")

            if state.state == "awaiting_viewing":
                print(f"   → Storing viewing answer and transitioning to awaiting_mortgage...")
                state.viewing_answer = message_text   # persist buyer's viewing availability
                state.state = "awaiting_mortgage"
                db.commit()
                ok = await _send(clean_phone, "text", {"body": "Great, noted! 👍 Are you pre-approved for a mortgage, or are you buying in cash?"})
                print(f"   Mortgage question send result: {ok}")
                return True

            elif state.state == "awaiting_mortgage":
                print(f"   → Conversation complete. Running lead AI analysis...")
                viewing_answer = getattr(state, 'viewing_answer', None) or "(not captured)"
                mortgage_answer = message_text

                state.state = "complete"
                db.commit()

                # Run real-estate-specific AI analysis (separate from plumbing engine)
                lead_intel = {}
                try:
                    lead_intel = await analyze_property_lead(
                        customer_phone=f"+{clean_phone}",
                        property_id=prop_id,
                        property_title=prop.title if prop else prop_id,
                        viewing_answer=viewing_answer,
                        mortgage_answer=mortgage_answer,
                        budget_range=prop.budget_range if prop else "",
                    )
                except Exception as ai_err:
                    print(f"   ⚠️ Lead AI analysis failed (non-fatal): {ai_err}")

                log_property_lead(
                    customer_phone=f"+{clean_phone}",
                    customer_name="WhatsApp Client",
                    property_id=prop_id,
                    budget=prop.budget_range if prop else "N/A",
                    timeline=lead_intel.get("urgency", "Conversational"),
                    marketer_phone=agent_phone,
                    raw_message=f"Viewing: {viewing_answer} | Mortgage: {mortgage_answer}",
                    notification_sent=True
                )

                # Build agent notification with AI lead intel
                temp = lead_intel.get("lead_temperature", "WARM")
                temp_emoji = {"HOT": "🔥", "WARM": "🟡", "COLD": "🧊"}.get(temp, "❓")
                score = lead_intel.get("confidence_score", "N/A")
                summary = lead_intel.get("buyer_summary", "No AI summary available.")
                action = lead_intel.get("recommended_action", "Review manually.")
                red_flags = lead_intel.get("red_flags", "")

                wa_direct_link = f"https://wa.me/{clean_phone}"
                agent_card = (
                    f"{temp_emoji} *NEW PROPERTY LEAD — {temp}* (confidence: {score}/100)\n\n"
                    f"👤 *Phone:* +{clean_phone}\n"
                    f"🏢 *Property:* {prop_id}\n"
                    f"💰 *Financing:* {lead_intel.get('financing_status', 'unknown')}\n"
                    f"📅 *Timeline:* {lead_intel.get('urgency', 'unknown')}\n\n"
                    f"🤖 *AI Summary:* {summary}\n"
                    f"⚡ *Next Step:* {action}\n"
                )
                if red_flags:
                    agent_card += f"\n⚠️ *Flags:* {red_flags}\n"
                agent_card += f"\n📲 *Reply instantly:* {wa_direct_link}"

                await send_whatsapp_message(to=agent_phone, payload_type="text", content={"body": agent_card}, wbot_url=resolved_wbot_url)
                ok = await _send(clean_phone, "text", {"body": "Thank you! 🙏 The listing agent has been notified and will be in touch shortly."})
                print(f"   Closing message send result: {ok}")
                return True
        else:
            print(f"   No active property state for {clean_phone}. Passing through to plumber flow.")

        return False
    except Exception as e:
        import traceback
        print(f"❌ EXCEPTION in property webhook flow: {e}")
        print(traceback.format_exc())
        return False
    finally:
        db.close()