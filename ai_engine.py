import os
import sys
import json
import httpx
import base64
import traceback  # Added to see the exact error strings in logs
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from dotenv import load_dotenv

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

# Configure Gemini
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY must be set in .env")

client = genai.Client(api_key=GOOGLE_API_KEY)

# THE REQUIRED HIERARCHY FOR VISION FALLBACK
MODEL_TIERS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash"
]

SYSTEM_PROMPT = """You are an expert plumbing emergency dispatcher. 
Analyze the customer's message and any attached images to determine the urgency of the plumbing issue.

URGENCY CATEGORIES:
- HIGH: Immediate danger or severe damage (e.g., flooding, burst main pipe, sewage backup inside, gas leak smell).
- MEDIUM: Significant issue but not immediate catastrophe (e.g., slow leak, broken water heater, clogged drain that isn't overflowing).
- LOW: Minor repairs or maintenance (e.g., dripping faucet, running toilet, scheduling a quote).

If the text clearly states a severe issue (flooding, leaks, sewage, gas), categorize it immediately.
If the text is vague (e.g., "look at this", "is this normal?", "see attached") and an image is present, set "needs_vision": true."""


# Define a strict Pydantic schema to eliminate JSON structure failures
class TriageResponse(BaseModel):
    urgency: str = Field(description="Must be HIGH, MEDIUM, LOW, or UNKNOWN")
    summary: str = Field(description="1-sentence summary of the plumbing problem")
    action_required: bool = Field(description="True if dispatcher dispatch required immediately")
    needs_vision: bool = Field(description="True if text is too vague and explicitly requires visual analysis")


# List of Ollama endpoints to try in order
OLLAMA_ENDPOINTS = []
primary_env_url = os.getenv("LOCAL_OLLAMA_API") or os.getenv("OLLAMA_CHAT_URL")
if primary_env_url:
    OLLAMA_ENDPOINTS.append(primary_env_url)

public_fallback = "https://ai.gentlemansolutions.com/api/chat"
if public_fallback not in OLLAMA_ENDPOINTS:
    OLLAMA_ENDPOINTS.append(public_fallback)


async def analyze_triage(text: str, image_url: str = None, image_bytes: bytes = None):
    """
    Analyzes plumbing tickets using a hyper-fast local text pre-filter (Ollama)
    and selectively routing to cloud vision (Gemini) only when images are necessary.
    """
    print(f"DEBUG: Starting triage analysis for text: '{text[:50]}...'")
    
    # 1. Download/Process Image if provided
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

    # 2. Step 1: Execute Fast Text-Only Triage via Ollama
    parsed_json = None
    ollama_success = False
    fast_text_model = "phi3:mini"
    
    # Inject format helper context directly into Ollama's prompt tracking
    ollama_format_prompt = f"""{SYSTEM_PROMPT}
    
    Respond ONLY with this JSON structure:
    {{
        "urgency": "HIGH" | "MEDIUM" | "LOW" | "UNKNOWN",
        "summary": "1-sentence summary",
        "action_required": true | false,
        "needs_vision": true | false
    }}"""

    for ollama_url in OLLAMA_ENDPOINTS:
        try:
            print(f"Attempting fast text analysis with Ollama endpoint: {ollama_url}...")
            user_content = f"{ollama_format_prompt}\n\nCustomer Message: {text}"
            
            payload = {
                "model": fast_text_model,
                "messages": [{"role": "user", "content": user_content}],
                "stream": True,
                "options": {
                    "temperature": 0.0,
                    "keep_alive": 0
                }
            }
            
            assistant_content = ""
            async with httpx.AsyncClient(timeout=15.0) as client_httpx: # Prevent hanging indefinitely
                async with client_httpx.stream("POST", ollama_url, json=payload) as response:
                    if response.status_code == 200:
                        async for line in response.aiter_lines():
                            if line:
                                try:
                                    line_str = line.decode("utf-8") if isinstance(line, bytes) else line
                                    line_str = line_str.strip()
                                    if not line_str:
                                        continue
                                    
                                    for part in line_str.split("\n"):
                                        part = part.strip()
                                        if part:
                                            data = json.loads(part)
                                            if "message" in data and "content" in data["message"]:
                                                assistant_content += data["message"]["content"]
                                except json.JSONDecodeError:
                                    pass
                    else:
                        raise Exception(f"HTTP {response.status_code}")

            assistant_content = assistant_content.strip()
            
            cleaned_content = assistant_content
            if "```json" in cleaned_content:
                cleaned_content = cleaned_content.split("```json")[1].split("```")[0].strip()
            elif "```" in cleaned_content:
                cleaned_content = cleaned_content.split("```")[1].split("```")[0].strip()
                
            parsed_json = json.loads(cleaned_content)
            
            if "urgency" in parsed_json and "summary" in parsed_json:
                parsed_json["ai_engine"] = f"Ollama Fast-Text ({fast_text_model})"
                ollama_success = True
                break
                
        except Exception as ollama_err:
            print(f"[Ollama Error Logged]: {ollama_err}")

    # Decision Node Check
    if ollama_success and parsed_json:
        if not parsed_json.get("needs_vision") and parsed_json.get("urgency") != "UNKNOWN":
            print("[Success] Text analysis sufficient. Bypassing cloud vision pipeline completely.")
            return parsed_json
        else:
            print("Text was ambiguous or explicitly flagged the need for visual context.")
    else:
        print("⚠️ Local triage pipeline was unreachable or timed out. Routing request entirely to Gemini cloud.")

    # 3. Step 2: Gemini Vision Fallback
    if not img_data:
        print("Vision analysis requested but no valid image payload exists. Returning text metadata.")
        if parsed_json:
            return parsed_json
    
    print("Running Gemini vision fallback tiers to analyze the image...")
    
    # Properly separate Text and Byte entities into individual Parts for the content array
    contents = [
        types.Part.from_text(text=f"Customer Message: {text}"),
        types.Part.from_bytes(data=img_data, mime_type="image/jpeg")
    ]

    for model_name in MODEL_TIERS:
        try:
            print(f"Attempting analysis with {model_name}...")
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=TriageResponse, # Forces Gemini to return the exact clean schema structure
                    temperature=0.0
                )
            )
            
            if response.text:
                parsed = json.loads(response.text)
                print(f"Gemini {model_name} vision analysis succeeded!")
                parsed["ai_engine"] = f"Gemini Vision Tier ({model_name})"
                return parsed
        
        except Exception as gemini_err:
            print(f"[Gemini Error on {model_name}]: {gemini_err}")
            traceback.print_exc() # This will output the precise API crash detail in your terminal logs
            continue

    # 4. Final Hard Recovery Block
    print("All AI subsystems failed or timed out. Returning default triage values.")
    return {
        "urgency": "HIGH" if parsed_json and parsed_json.get("urgency") == "HIGH" else "MEDIUM",
        "summary": "AI pipeline routing exception. Defaulted for safety metrics.",
        "action_required": True,
        "needs_vision": False,
        "ai_engine": "Offline Recovery Engine"
    }