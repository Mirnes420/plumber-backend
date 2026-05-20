import os
import sys
import json
import httpx
import base64
from google import genai
from google.genai import types
from dotenv import load_dotenv

# Force UTF-8 encoding for standard output and error on Windows to avoid UnicodeEncodeErrors when printing emojis
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

# THE REQUIRED HIERARCHY
MODEL_TIERS = [
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash"
]

SYSTEM_PROMPT = """You are an expert plumbing emergency dispatcher. 
Analyze the customer's message and any attached images to determine the urgency of the plumbing issue.

URGENCY CATEGORIES:
- HIGH: Immediate danger or severe damage (e.g., flooding, burst main pipe, sewage backup inside, gas leak smell).
- MEDIUM: Significant issue but not immediate catastrophe (e.g., slow leak, broken water heater, clogged drain that isn't overflowing).
- LOW: Minor repairs or maintenance (e.g., dripping faucet, running toilet, scheduling a quote).

OUTPUT FORMAT:
You MUST respond with a valid JSON object only:
{
    "urgency": "HIGH" | "MEDIUM" | "LOW",
    "summary": "Short 1-sentence summary of the issue",
    "action_required": true | false
}"""

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
    Analyzes text and optionally an image using Ollama (with Gemini as fallback).
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

    # 2. Try Ollama (Primary & Fallbacks)
    ollama_success = False
    parsed_json = None
    
    for ollama_url in OLLAMA_ENDPOINTS:
        try:
            print(f"Attempting analysis with Ollama endpoint: {ollama_url}...")
            model_name = "phi3:mini"
            
            # Prepare messages
            user_content = f"{SYSTEM_PROMPT}\n\nCustomer Message: {text}"
            user_msg = {"role": "user", "content": user_content}
            
            if img_data:
                base64_str = base64.b64encode(img_data).decode('utf-8')
                user_msg["images"] = [base64_str]
                
            payload = {
                "model": model_name,
                "messages": [user_msg],
                "stream": True
            }
            
            print("Sending streaming request to Ollama...")
            assistant_content = ""
            # Infinite timeout ensures it never aborts early due to loading/processing speed
            async with httpx.AsyncClient(timeout=None) as client_httpx:
                async with client_httpx.stream("POST", ollama_url, json=payload) as response:
                    if response.status_code == 200:
                        async for line in response.aiter_lines():
                            if line:
                                try:
                                    # Ensure we have a string representation of the line
                                    line_str = line.decode("utf-8") if isinstance(line, bytes) else line
                                    line_str = line_str.strip()
                                    if not line_str:
                                        continue
                                    
                                    # Split by newline in case multiple JSON chunks are delivered in a single packet
                                    for part in line_str.split("\n"):
                                        part = part.strip()
                                        if part:
                                            try:
                                                data = json.loads(part)
                                                if "message" in data:
                                                    chunk = data["message"]["content"]
                                                    assistant_content += chunk
                                            except json.JSONDecodeError:
                                                pass
                                except Exception as e:
                                    print(f"Error parsing streaming packet: {e}")
                    else:
                        print(f"⚠️ Ollama returned status code: {response.status_code}")
                        raise Exception(f"HTTP {response.status_code}")

            assistant_content = assistant_content.strip()
            print(f"Ollama response received: {assistant_content}")
            
            # Parse the response as JSON (extract from markdown codeblock if present)
            cleaned_content = assistant_content
            if "```json" in cleaned_content:
                cleaned_content = cleaned_content.split("```json")[1].split("```")[0].strip()
            elif "```" in cleaned_content:
                cleaned_content = cleaned_content.split("```")[1].split("```")[0].strip()
            
            parsed_json = json.loads(cleaned_content)
            
            # Validate required keys
            if "urgency" in parsed_json and "summary" in parsed_json:
                print("✅ Ollama analysis succeeded!")
                parsed_json["ai_engine"] = f"Ollama ({model_name} @ {ollama_url})"
                ollama_success = True
                break
            else:
                print("⚠️ Ollama response was missing required JSON keys.")
                
        except Exception as ollama_err:
            print(f"⚠️ Ollama endpoint failed: {ollama_err}")

    if ollama_success and parsed_json:
        return parsed_json
    else:
        print("⚠️ All Ollama endpoints failed. Falling back to Gemini...")

    # 3. Fallback to Gemini (Sequential Tiers)
    print("🚀 Running Gemini fallback tiers...")
    
    contents = [f"Customer Message: {text}"]
    if img_data:
        image_part = types.Part.from_bytes(
            data=img_data,
            mime_type="image/jpeg"
        )
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
                print(f"✅ Gemini {model_name} fallback succeeded!")
                parsed["ai_engine"] = f"Gemini ({model_name})"
                return parsed
        
        except Exception as e:
            print(f"Model {model_name} failed: {e}. Moving to next tier...")
            continue

    # 4. Final Fallback if everything fails
    print("❌ All AI systems failed. Returning default values.")
    return {
        "urgency": "MEDIUM",
        "summary": "AI system failure (Ollama & Gemini offline). Manual triage required.",
        "action_required": True,
        "ai_engine": "Offline Fallback"
    }