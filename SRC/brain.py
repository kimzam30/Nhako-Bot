import ollama
import json

AI_MODEL = 'llama3.2'

def generate_response(messages):
    """Handles normal text conversation with the AI."""
    response = ollama.chat(model=AI_MODEL, messages=messages)
    return response['message']['content']

def generate_json(prompt):
    """Forces the AI to extract data and return it strictly as a JSON dictionary."""
    response = ollama.chat(model=AI_MODEL, messages=[{"role": "user", "content": prompt}])
    clean_json = response['message']['content'].replace("```json", "").replace("```", "").strip()
    
    try:
        return json.loads(clean_json)
    except Exception as e:
        print(f"JSON Parse Error: {e} - Raw output: {clean_json}")
        return None