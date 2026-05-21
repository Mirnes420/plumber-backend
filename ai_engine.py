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

client = genai.Client(api_key=GOOGLE_API_KEY)

MODEL_TIERS = [
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash"
]

SYSTEM_PROMPT = """You are a plumbing emergency dispatcher. Analyze the customer's message.

URGENCY: HIGH (flooding, burst main, backup), MEDIUM (slow leak, broken heater), LOW (drip, running toilet).
GEAR LOGIC: Deduce required tools from the symptoms as a flat text string.

JSON OUTPUT ONLY. DO NOT INCLUDE ANY COMMENTS (e.g., // or /* */) inside the JSON data.

{
    "urgency": "HIGH|MEDIUM|LOW",
    "summary": "1-sentence symptom statement.",
    "gear": "Specific tools/parts to bring as a single comma-separated text string.",
    "action": true|false,
    "img_verify": true|false
}"""

OLLAMA_ENDPOINTS = []
primary_env_url = os.getenv("LOCAL_OLLAMA_API") or os.getenv("OLLAMA_CHAT_URL")
if primary_env_url:
    OLLAMA_ENDPOINTS.append(primary_env_url)

public_fallback = "https://ai.gentlemansolutions.com/api/chat"
if public_fallback not in OLLAMA_ENDPOINTS:
    OLLAMA_ENDPOINTS.append(public_fallback)


async def download_image_async(image_url: str) -> bytes:
    """Asynchronously download image bytes down a standalone thread."""
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

async def analyze_triage(text: str, image_url: str = None, image_bytes: bytes = None):
    print(f"DEBUG: Starting triage analysis for text: '{text[:50]}...'")
    print("Starting the timer")
    timer_start = time.time()
    
    # Fire off background image fetch task instantly without blocking execution
    image_download_task = None
    img_data = image_bytes

    if not img_data and image_url:
        image_download_task = asyncio.create_task(download_image_async(image_url))

    ollama_success = False
    parsed_json = None
    
    for ollama_url in OLLAMA_ENDPOINTS:
        try:
            print(f"Attempting text-based filtering with Ollama endpoint: {ollama_url}...")
            model_name = "phi3:mini"
            
            # Use presence indicators since background download hasn't finished yet
            has_image_attached = True if (img_data or image_url) else False
            image_presence_context = "An image was attached by the user." if has_image_attached else "No image was attached."
            user_content = f"{SYSTEM_PROMPT}\n\nContext: {image_presence_context}\nCustomer Message: {text}"
            
            payload = {
                "model": model_name,
                "messages": [{"role": "user", "content": user_content}],
                "stream": True
            }
            
            print("Sending text payload to Phi3...")
            raw_response = await query_ollama_stream(ollama_url, payload)
            print(f"Ollama raw output: {raw_response}")
            
            cleaned_content = raw_response
            if "```json" in cleaned_content:
                cleaned_content = cleaned_content.split("```json")[1].split("```")[0].strip()
            elif "```" in cleaned_content:
                cleaned_content = cleaned_content.split("```")[1].split("```")[0].strip()
            
            # 🔥 CRITICAL SCRUBBER: Remove JavaScript-style comments before loading JSON
            cleaned_content = re.sub(r'//.*$', '', cleaned_content, flags=re.MULTILINE)
            
            parsed_json = json.loads(cleaned_content)
            
            if "urgency" in parsed_json and "summary" in parsed_json:
                # ROUTING INTERCEPTION
                if has_image_attached and parsed_json.get("img_verify", False):
                    print("🔄 Phi3 indicated that image evaluation is REQUIRED. Aborting Ollama cascade to run Gemini Vision...")
                    break
                
                print("✅ Ollama triage successful (Image analysis skipped or unneeded).")
                parsed_json["ai_engine"] = f"Ollama ({model_name} @ {ollama_url})"
                ollama_success = True
                break
            else:
                print("⚠️ Ollama response structure was invalid.")
                
        except Exception as ollama_err:
            print(f"⚠️ Ollama endpoint failed: {ollama_err}")

    if ollama_success and parsed_json:
        # Cancel live background task if Ollama processed everything via text alone
        if image_download_task and not image_download_task.done():
            image_download_task.cancel()
        print(f"\nTTF {time.time() - timer_start:.2f} seconds.")
        return parsed_json

    # Await image download resolution only when Gemini Vision route is forced
    if image_download_task:
        print("Waiting for pending background image download to resolve...")
        img_data = await image_download_task

    print("🚀 Triggering Gemini Engine (Primary pipeline execution or visual triage fallback)...")
    
    contents = [f"Customer Message: {text}"]
    if img_data:
        image_part = types.Part.from_bytes(data=img_data, mime_type="image/jpeg")
        contents.append(image_part)

    for model_name in MODEL_TIERS:
        try:
            print(f"Attempting analysis with {model_name}...")
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json"
                )
            )
            
            if response.text:
                parsed = json.loads(response.text)
                print(f"✅ Gemini {model_name} execution succeeded!")
                parsed["ai_engine"] = f"Gemini ({model_name})"
                print(f"\nTTF {time.time() - timer_start:.2f} seconds.")
                return parsed
        
        except Exception as e:
            print(f"Model {model_name} failed: {e}. Advancing down the cluster...")
            continue

    print("❌ All AI endpoints down. Running failsafe defaults.")
    print(f"\nTTF {time.time() - timer_start:.2f} seconds.")
    return {
        "urgency": "HIGH",
        "summary": "AI network timeout. High volume emergency fluid breach assumed.",
        "gear": "Bring emergency line isolation kit, hydraulic pipe crimpers, and 3/4 inch coupling patches.",
        "action": True,
        "img_verify": False,
        "ai_engine": "Offline Fallback"
    }