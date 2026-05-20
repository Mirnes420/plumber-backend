import os
import sys
import json
import httpx
import base64
from google import genai
from google.genai import types
from dotenv import load_dotenv

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

SYSTEM_PROMPT = """You are an expert plumbing emergency dispatcher. 
Analyze the customer's message to determine the urgency of the plumbing issue.

URGENCY CATEGORIES:
- HIGH: Immediate danger or severe damage (e.g., flooding, burst main pipe, sewage backup inside, gas leak smell).
- MEDIUM: Significant issue but not immediate catastrophe (e.g., slow leak, broken water heater, clogged drain that isn't overflowing).
- LOW: Minor repairs or maintenance (e.g., dripping faucet, running toilet, scheduling a quote).

If an image is available, evaluate if seeing it is strictly necessary to correctly triage this issue (e.g., an abstract description like "it looks like this" vs a clear text description like "my kitchen faucet is dripping").

OUTPUT FORMAT:
You MUST respond with a valid JSON object only:
{
    "urgency": "HIGH" | "MEDIUM" | "LOW",
    "summary": "Short 1-sentence summary of the issue",
    "action_required": true | false,
    "image_needed": true | false
}"""

OLLAMA_ENDPOINTS = []
primary_env_url = os.getenv("LOCAL_OLLAMA_API") or os.getenv("OLLAMA_CHAT_URL")
if primary_env_url:
    OLLAMA_ENDPOINTS.append(primary_env_url)

public_fallback = "https://ai.gentlemansolutions.com/api/chat"
if public_fallback not in OLLAMA_ENDPOINTS:
    OLLAMA_ENDPOINTS.append(public_fallback)

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
    
    img_data = None
    if image_bytes:
        img_data = image_bytes
    elif image_url:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            async with httpx.AsyncClient(follow_redirects=True, headers=headers) as client_httpx:
                response = await client_httpx.get(image_url)
                if response.status_code == 200:
                    img_data = response.content
                else:
                    print(f"Failed to download image: HTTP {response.status_code}")
        except Exception as e:
            print(f"Image processing error: {e}")

    ollama_success = False
    parsed_json = None
    
    for ollama_url in OLLAMA_ENDPOINTS:
        try:
            print(f"Attempting text-based filtering with Ollama endpoint: {ollama_url}...")
            model_name = "phi3:mini"
            
            # Formulate prompt indicating if an image is hovering in context
            image_presence_context = "An image was attached by the user." if img_data else "No image was attached."
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
            
            parsed_json = json.loads(cleaned_content)
            
            if "urgency" in parsed_json and "summary" in parsed_json:
                # ROUTING INTERCEPTION
                if img_data and parsed_json.get("image_needed", False):
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
        return parsed_json

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
                return parsed
        
        except Exception as e:
            print(f"Model {model_name} failed: {e}. Advancing down the cluster...")
            continue

    print("❌ All AI endpoints down. Running failsafe defaults.")
    return {
        "urgency": "MEDIUM",
        "summary": "AI network timeout or fatal parsing error. Handing over to dispatcher.",
        "action_required": True,
        "image_needed": False,
        "ai_engine": "Offline Fallback"
    }