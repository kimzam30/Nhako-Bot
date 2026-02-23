import ollama

print("🧠 Pinging the local Ollama brain...")

try:
    response = ollama.chat(model='llama3.2', messages=[
        {'role': 'user', 'content': 'Say "I am alive" and nothing else.'}
    ])
    
    print("✅ Brain is ALIVE!")
    print("🤖 Reply:", response['message']['content'])

except Exception as e:
    print("❌ Connection Failed. Is Ollama running in the background?")
    print("Error Details:", e)