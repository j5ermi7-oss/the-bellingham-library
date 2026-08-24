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
You are an automatic grammar, spelling, and punctuation corrector.
The admin has written a quick draft message to a member.
Your ONLY job is to fix any spelling, grammar, or punctuation errors.

RULES:
1. DO NOT change the tone, style, or vocabulary of the message. 
2. DO NOT use "fancy" or complex English. Keep the exact words the admin used, just fix the errors.
3. DO NOT use ANY emojis under any circumstances unless they were in the original draft.
4. Output ONLY the corrected message, with no introductory or concluding notes.

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