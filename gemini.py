import os
import google.generativeai as genai
# Configure API key safely
api_key = os.getenv("GEMINI_API_KEY")
if api_key:
    genai.configure(api_key=api_key)
def enhance_text_to_ai_persona(draft_text):
    if not api_key:
        return "⚠️ Error: GEMINI_API_KEY is not set in Render Environment Variables."
    
    try:
        # Using a "lite" model ensures you don't hit the free-tier quota limits
        target_model = 'gemini-flash-lite-latest'
        model = genai.GenerativeModel(target_model)
        
        prompt = f"""
You are a friendly, helpful human assistant in a Telegram group chat.
The admin has written a quick draft message to a member.
Your job is to rewrite this draft into simple, natural, everyday human English so that anyone can easily understand it.

RULES:
1. Use simple words and short, clear sentences. Avoid big, complicated, robotic, or overly formal words.
2. Sound like a real, friendly person chatting online—warm, natural, and helpful.
3. DO NOT use ANY emojis under any circumstances. Keep it text-only.
4. Keep the exact same meaning and facts from the admin's draft.
5. Output ONLY the rewritten message, with no introductory or concluding notes.

Admin's Draft:
{draft_text}
"""
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        try:
            available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
            models_str = ", ".join(available_models)
        except Exception:
            models_str = "Could not fetch list."
            
        return f"⚠️ Error: {e}\n\nAvailable models for your API key: {models_str}"