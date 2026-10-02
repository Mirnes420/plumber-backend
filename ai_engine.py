import os
import sys
import json
import httpx
import base64
import asyncio
from google import genai
from google.genai import types
from dotenv import load_dotenv
import time
import re
# ==============================================================================
# 1. RUNTIME ENVIRONMENT CONFIGURATION
# ==============================================================================
# Force standard output channels to process UTF-8 characters properly. This prevents
# terminal rendering crashes on Windows machines when handling emojis or symbols.
if sys.platform.startswith("win"):
    if hasattr(sys.stdout, "reconfigure"):
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    if hasattr(sys.stderr, "reconfigure"):
        try: sys.stderr.reconfigure(encoding="utf-8")
        except Exception: pass
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY must be set in .env")
# Initialize the stateful Google GenAI client layer
client = genai.Client(api_key=GOOGLE_API_KEY)
# ==============================================================================
# 2. MODEL ROUTING & FAILOVER TOPOLOGY
# ==============================================================================
# Multi-tiered fallback array. If an upstream low-latency model experiences rate limits 
# (HTTP 429) or transient server errors (HTTP 5xx), the execution circuit backs off 
# exponentially before shifting load down to alternative cluster targets.
MODEL_TIERS = [
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash"
]
# ==============================================================================
# 3. DOMAIN-SPECIFIC EXPERT PROMPTS
# ==============================================================================
SYSTEM_PROMPTS = {
    "plumber": """You are an emergency plumbing dispatcher. Analyze the customer's plumbing issue.
URGENCY CRITERIA:
- HIGH: Burst pipes, active flooding, sewage backup into living areas, no water supply to the building, or water heater failure causing scalding/flooding risk.
- MEDIUM: Slow leaks, clogged drains without overflow, running toilets, dripping faucets with moderate water loss, low water pressure.
- LOW: Minor cosmetic drips, routine inspections, non-critical slow drains, or maintenance requests.
GEAR LOGIC: Deduce the specific plumbing tools, pipe fittings, and replacement parts required from the customer symptoms. Return them as a flat text string.
JSON OUTPUT ONLY. DO NOT INCLUDE ANY COMMENTS (e.g., // or /* */) inside the JSON data.
{
    "urgency": "HIGH|MEDIUM|LOW",
    "summary": "1-sentence plumbing-specific symptom statement.",
    "gear": "Specific plumbing tools, pipe fittings, or parts to bring as a single comma-separated text string.",
    "action": true|false,
    "img_verify": true|false
}""",
    "hvac": """You are an emergency HVAC dispatcher. Analyze the customer's heating, ventilation, or air conditioning issue.
URGENCY CRITERIA:
- HIGH: Complete heating failure in freezing temperatures, suspected gas leak or burning smell from unit, CO alarm triggered, smoke from HVAC system, or refrigerant line rupture.
- MEDIUM: AC not cooling adequately in hot weather, furnace cycling but not reaching set temperature, unusual noises, water dripping from indoor unit, or thermostat malfunction.
- LOW: Minor airflow imbalance, filter replacement, routine seasonal tune-up requests, or mildly noisy operation.
GEAR LOGIC: Deduce the specific HVAC tools, refrigerants, filters, ignitors, capacitors, or components required. Return them as a flat text string.
JSON OUTPUT ONLY. DO NOT INCLUDE ANY COMMENTS (e.g., // or /* */) inside the JSON data.
{
    "urgency": "HIGH|MEDIUM|LOW",
    "summary": "1-sentence HVAC-specific symptom statement.",
    "gear": "Specific HVAC tools, refrigerant, or replacement components to bring as a single comma-separated text string.",
    "action": true|false,
    "img_verify": true|false
}""",
    "electrician": """You are an emergency electrical dispatcher. Analyze the customer's electrical issue.
URGENCY CRITERIA:
- HIGH: Sparking outlets or panels, burning smell from electrical sources, complete power loss to the building, exposed live wiring, arc flashes, or suspected electrical fire risk.
- MEDIUM: Frequently tripping breakers, flickering lights throughout home, partial power loss to rooms, malfunctioning outlets, or GFCI failures.
- LOW: Single non-critical outlet not working, dimmer switch issues, routine panel inspection, minor lighting faults, or cosmetic electrical concerns.
GEAR LOGIC: Deduce the specific electrical tools, breakers, wire gauges, testers, or replacement components required. Return them as a flat text string.
JSON OUTPUT ONLY. DO NOT INCLUDE ANY COMMENTS (e.g., // or /* */) inside the JSON data.
{
    "urgency": "HIGH|MEDIUM|LOW",
    "summary": "1-sentence electrical-specific symptom statement.",
    "gear": "Specific electrical tools, testers, breakers, or wiring components to bring as a single comma-separated text string.",
    "action": true|false,
    "img_verify": true|false
}"""
}
SYSTEM_PROMPTS["property_lead"] = """You are an expert real estate lead qualification analyst. Your job is to read the collected Q&A from a WhatsApp conversation between a prospective buyer and a real estate bot, then produce a structured lead intelligence report.
LEAD TEMPERATURE CRITERIA:
- HOT: Buyer is ready to view immediately, pre-approved for mortgage or paying cash, has specific property requirements that match, and is actively comparing options.
- WARM: Buyer is interested and responsive but has a vague timeline (1-3 months), unclear financing, or is "just looking" with some genuine interest signals.
- COLD: Buyer is browsing casually, very long timeline (6+ months), no financing clarity, vague responses, or low engagement.
FINANCING SIGNALS:
- "cash" or "cash buyer" → Strong buyer signal (HOT indicator).
- "pre-approved", "mortgage ready", "bank approval" → Solid buyer signal.
- "looking into financing", "not sure yet" → Warm signal.
- No mention of financing → Flag as unknown.
YOUR TASK:
Analyze the conversation transcript and return a JSON object with a lead intelligence report. Be concise, specific, and professional.
JSON OUTPUT ONLY. Return exactly this structure:
{
    "lead_temperature": "HOT|WARM|COLD",
    "confidence_score": 0-100,
    "buyer_summary": "2-3 sentence human-readable summary of who this buyer is and what they need.",
    "financing_status": "cash|pre-approved|exploring|unknown",
    "urgency": "immediate|1-3 months|5-10 months|just browsing",
    "red_flags": "Any concerns or signals of low seriousness. Empty string if none.",
    "recommended_action": "Specific next step the agent should take (e.g., 'Book a viewing call within 24h', 'Send comparable listings', 'Add to newsletter, low priority').",
    "ai_engine": "property_lead_analyzer"
}"""
# Legacy backward-compatibility mapping
SYSTEM_PROMPT = SYSTEM_PROMPTS["plumber"]
# ==============================================================================
# 6. PROPERTY LEAD INTELLIGENCE ANALYZER
# ==============================================================================
async def analyze_property_lead(
    customer_phone: str,
    property_id: str,
    property_title: str,
    viewing_answer: str,
    mortgage_answer: str,
    budget_range: str = "",
) -> dict:
    """
    Runs AI analysis on the collected property inquiry Q&A to classify lead
    temperature, summarize buyer intent, and recommend next agent actions.
    This is completely separate from the emergency dispatch pipeline.
    """
    import time
    print(f"\n{'='*60}")
    print(f"🏠 PROPERTY LEAD AI: Analyzing lead for {customer_phone} on {property_id}")
    print(f"{'='*60}")
    timer_start = time.time()
    system_prompt = SYSTEM_PROMPTS["property_lead"]
    conversation_text = (
        f"Property Inquiry Conversation Transcript\n"
        f"---\n"
        f"Property: {property_title} ({property_id})\n"
        f"Budget Range Listed: {budget_range or 'Not specified'}\n"
        f"---\n"
        f"Bot: When are you available for a viewing?\n"
        f"Buyer: {viewing_answer or '(no response)'}\n"
        f"---\n"
        f"Bot: Are you pre-approved for a mortgage, or are you buying in cash?\n"
        f"Buyer: {mortgage_answer or '(no response)'}\n"
    )
    print(f"   Transcript:\n{conversation_text}")
    base_delay = 1.5
    for attempt_idx, model_name in enumerate(MODEL_TIERS):
        try:
            print(f"   Attempting lead analysis with {model_name}...")
            response = client.models.generate_content(
                model=model_name,
                contents=[conversation_text],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json"
                )
            )
            if response.text:
                parsed = json.loads(response.text)
                parsed["ai_engine"] = f"property_lead/{model_name}"
                elapsed = time.time() - timer_start
                print(f"   ✅ Lead analysis succeeded in {elapsed:.2f}s: temp={parsed.get('lead_temperature')} confidence={parsed.get('confidence_score')}")
                return parsed
        except Exception as e:
            calculated_delay = base_delay * (2 ** attempt_idx)
            print(f"   ⚠️ {model_name} failed: {e}")
            if model_name != MODEL_TIERS[-1]:
                await asyncio.sleep(calculated_delay)
            continue
    # Fallback if all models fail
    print("   ❌ All models failed for property lead analysis. Using safe defaults.")
    return {
        "lead_temperature": "WARM",
        "confidence_score": 30,
        "buyer_summary": "AI analysis unavailable. Manual review required.",
        "financing_status": "unknown",
        "urgency": "unknown",
        "red_flags": "AI engine timeout — data may be incomplete.",
        "recommended_action": "Contact buyer manually and qualify by phone.",
        "ai_engine": "property_lead/fallback"
    }
# ==============================================================================
# 4. NETWORK INGESTION GATEWAYS (OLLAMA & HTTPX)
# ==============================================================================
OLLAMA_ENDPOINTS = []
primary_env_url = os.getenv("LOCAL_OLLAMA_API") or os.getenv("OLLAMA_CHAT_URL")
if primary_env_url:
    OLLAMA_ENDPOINTS.append(primary_env_url)
public_fallback = "https://ai.gentlemansolutions.com/api/chat"
if public_fallback not in OLLAMA_ENDPOINTS:
    OLLAMA_ENDPOINTS.append(public_fallback)
async def download_image_async(image_url: str) -> bytes:
    """Non-blocking asynchronous task execution to download raw image binary data."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(follow_redirects=True, headers=headers, timeout=10.0) as client_httpx:
            response = await client_httpx.get(image_url)
            if response.status_code == 200:
                return response.content
    except Exception as e:
        print(f"Background image download error: {e}")
    return None
async def query_ollama_stream(url: str, payload: dict) -> str:
    """Streams and builds text inference payloads out of local model layers."""
    assistant_content = ""
    async with httpx.AsyncClient(timeout=30.0) as client_httpx:
        async with client_httpx.stream("POST", url, json=payload) as response:
            if response.status_code == 200:
                async for line in response.aiter_lines():
                    if line:
                        line_str = line.decode("utf-8") if isinstance(line, bytes) else line
                        line_str = line_str.strip()
                        if not line_str:
                            continue
                        for part in line_str.split("\n"):
                            part = part.strip()
                            if part:
                                try:
                                    data = json.loads(part)
                                    if "message" in data:
                                        assistant_content += data["message"]["content"]
                                except json.JSONDecodeError:
                                    pass
            else:
                raise Exception(f"HTTP {response.status_code}")
    return assistant_content.strip()
# ==============================================================================
# 5. CORE TRIAGE ENGINE WITH EXPONENTIAL BACKOFF RETRY CIRCUITS
# ==============================================================================

async def analyze_triage(text: str, image_url: str = None, image_bytes: bytes = None, demo: bool = False, professional_type: str = 'plumber', language: str = 'English'):
    """
    Orchestrates automated incoming tickets. Attempts rapid classification via
    local models, falling back to a cloud-based Gemini cluster wrapped with 
    asynchronous exponential backoff mechanics.
    """
    print(f"DEBUG: Starting triage analysis for text: '{text[:50]}...' | professional_type: {professional_type} | language: {language} | demo mode: {demo}")
    print("Starting the timer")
    timer_start = time.time()
    
    system_prompt = SYSTEM_PROMPTS.get(professional_type, SYSTEM_PROMPTS["plumber"])
    system_prompt += f"\n\nIMPORTANT: Generate the 'summary' and 'gear' fields strictly in {language}."
    print(f"DEBUG: Using system prompt for professional type: '{professional_type}'")
    
    # Non-blocking image fetching thread startup
    if image_url:
        image_bytes = await download_image_async(image_url)
    
    # Local Ollama fallback loop (rapid failover for low-latency inference)
    if not demo:
        print("DEBUG: Attempting local Ollama endpoints first")
        for endpoint in OLLAMA_ENDPOINTS:
            print(f"DEBUG: Querying Ollama endpoint: {endpoint}")
            try:
                payload = {
                    "model": "llama3",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": text}
                    ],
                    "stream": True
                }
                
                if image_bytes:
                    encoded_image = base64.b64encode(image_bytes).decode("utf-8")
                    payload["messages"].append({
                        "role": "user",
                        "content": "The user attached an image. Please analyze it.",
                        "images": [encoded_image]
                    })

                print(f"DEBUG: Streaming from Ollama URL: {endpoint}")
                result_text = await query_ollama_stream(endpoint, payload)
                
                if result_text:
                    print("DEBUG: Received response from Ollama")
                    # Basic cleanup for "hi there" artifacts
                    if "hi there" in result_text.lower():
                        result_text = result_text.lower().replace("hi there", "").strip()
                    
                    try:
                        response_data = json.loads(result_text)
                        # Map toexpected field names
                        # Ollama returns: 'message' -> nested dict with 'content' or keys like 'urgency', 'summary', 'gear'
                        # Check common Ollama response structures first
                        if isinstance(response_data, dict):
                            if "message" in response_data and "content" in response_data["message"]:
                                content_dict = response_data["message"]["content"]
                                if isinstance(content_dict, dict):
                                    # Success: extract fields directly
                                    parsed_json = content_dict
                                elif isinstance(content_dict, str):
                                    try:
                                        # Fallback: parse string content
                                        parsed_json = json.loads(content_dict)
                                    except Exception:
                                        # If it's already a string of fields
                                        if ":" in content_dict:
                                            parsed_json = {
                                                "urgency": "MEDIUM",
                                                "summary": content_dict,
                                                "gear": "Unknown"
                                            }
                                        else:
                                            raise
                            elif "urgency" in response_data and "summary" in response_data:
                                parsed_json = response_data
                            else:
                                raise ValueError("Unknown Ollama response format")
                        else:
                            raise ValueError("Invalid JSON structure")
                        
                        parsed_json["ai_engine"] = f"{professional_type}/{endpoint}"
                        elapsed = time.time() - timer_start
                        print(f"\n{'='*60}")
                        print(f"✅ SUCCESS: Ollama triage completed in {elapsed:.2f} seconds")
                        print(f"   AI Engine Used: {parsed_json['ai_engine']}")
                        print(f"   Urgency: {parsed_json.get('urgency', 'N/A')}")
                        print(f"   Summary: {parsed_json.get('summary', 'N/A')}")
                        print(f"   Gear Required: {parsed_json.get('gear', 'N/A')}")
                        print(f"   Action Required: {parsed_json.get('action', 'N/A')}")
                        print(f"   Image Verify: {parsed_json.get('img_verify', 'N/A')}")
                        print(f"{'='*60}\n")
                        return parsed_json
                    except Exception as parse_error:
                        print(f"Failed to parse JSON from Ollama response: {parse_error}")
                        continue
            except Exception as e:
                print(f"DEBUG: Ollama endpoint {endpoint} failed: {e}")
                continue
    
    # Cloud-based Gemini wrapper with exponential backoff
    print("\n⚠️ Local Ollama unavailable or failed. Attempting cloud Gemini API...")
    base_delay = 2.0
    max_attempts = 4
    
    for attempt_idx, model_name in enumerate(MODEL_TIERS):
        try:
            print(f"   Attempt {attempt_idx + 1}/{max_attempts}: Using {model_name}...")
            
            # Prepare content with safety settings
            content = [
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            text=f"You are an expert {professional_type} dispatcher. {system_prompt}",
                        )
                    ]
                ),
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            text=text,
                        )
                    ]
                ),
            ]

            if image_bytes:
                # Use bytes directly, no base64 encoding needed for Gemini native
                content.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                mime_type="image/jpeg",
                                data=image_bytes
                            )
                        ]
                    )
                )

            # Add structured output configuration
            config = types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json"
            )

            response = client.models.generate_content(
                model=model_name,
                contents=content,
                config=config,
            )
            
            # Extract summary and other fields from response
            summary = response.text.strip()
            parsed_json = {}
            
            try:
                parsed_json = json.loads(summary)
            except json.JSONDecodeError:
                print(f"   ⚠️ Invalid JSON from {model_name}. Parsing as string...")
                parsed_json["urgency"] = "MEDIUM"
                parsed_json["summary"] = summary
                parsed_json["gear"] = "Unknown"
            
            # Apply demo mode transformation if enabled
            if demo:
                print("   🔄 Applying demo mode transformations...")
                parsed_json["urgency"] = "LOW"
                parsed_json["summary"] = f"DEMO MODE: {parsed_json.get('summary', text)}"
                parsed_json["gear"] = "Basic toolkit (demo)"
                parsed_json["action"] = False
                parsed_json["img_verify"] = False
                parsed_json["ai_engine"] = "Demo Mode"
            else:
                parsed_json["ai_engine"] = f"gemini/{model_name}"

            # Update demo logic for property leads (add more detail)
            if professional_type == "property_lead":
                if demo:
                    print("   🔄 Applying demo mode for property lead...")
                    parsed_json["urgency"] = "LOW"
                    parsed_json["summary"] = "DEMO MODE: Just browsing, not serious yet."
                    parsed_json["gear"] = "None"
                    parsed_json["action"] = False
                    parsed_json["img_verify"] = False
                    parsed_json["ai_engine"] = "Demo Mode"
                else:
                    # Clean up any extra LLM conversational filler
                    if "analysis" in parsed_json:
                        print("   🧹 Cleaning up 'analysis' field from Gemini response...")
                        parsed_json["summary"] = parsed_json["analysis"]
                        del parsed_json["analysis"]
                    # Ensure required fields exist
                    required_fields = ["urgency", "summary", "gear", "action", "img_verify", "ai_engine"]
                    for field in required_fields:
                        if field not in parsed_json:
                            parsed_json[field] = "N/A" if field != "action" else False
            
            elapsed = time.time() - timer_start
            print(f"   ✅ Success! {model_name} completed in {elapsed:.2f} seconds")
            print(f"   Summary: {parsed_json.get('summary', 'N/A')}")
            print(f"   Action Required: {parsed_json.get('action', 'N/A')}\n")
            return parsed_json
        
        except Exception as e:
            print(f"   ⚠️ {model_name} failed: {e}")
            delay = base_delay * (2 ** attempt_idx)  # Exponential backoff
            print(f"   Retrying in {delay} seconds...")
            await asyncio.sleep(delay)
    
    print("❌ All attempts failed. Returning offline fallback.")
    return {
        "urgency": "UNKNOWN",
        "summary": "AI service unavailable. Please check internet connection or Ollama server.",
        "gear": "Unknown",
        "action": False,
        "img_verify": False,
        "ai_engine": "Offline Fallback"
    }
