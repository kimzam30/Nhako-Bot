# main.py
import os
import logging
import datetime
import ears
import search
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# Import our new modules and tools
import memory
import brain
from my_calendar import get_upcoming_events, create_event, delete_event, clear_events_for_day

# 1. Load Secrets
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0")) 

# 2. Setup Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_chat:
        return

    user_id = update.effective_user.id
    if user_id != ALLOWED_USER_ID:
        print(f"⚠️ Unauthorized access attempt from ID: {user_id}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⛔ Access Denied.")
        return

    # Clear memory on start
    await context.bot.send_message(
        chat_id=update.effective_chat.id, 
        text=f"👋 Identity Verified. I am running 100% LOCALLY on {brain.AI_MODEL}. No rate limits. How can I help?"
    )

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_chat or not update.message:
        return 

    user_id = update.effective_user.id
    if user_id != ALLOWED_USER_ID:
        return 

    chat_id = update.effective_chat.id
    
    # --- VOICE INTERCEPTION (NEW) ---
    if update.message.voice:
        print("🎙️ Voice message received. Downloading...")
        # Tell the user we are processing it so they don't think it froze
        await context.bot.send_chat_action(chat_id=chat_id, action='typing')
        
        # Grab the file ID from Telegram's servers
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        
        # Save it temporarily to your home lab
        file_path = f"temp_voice_{update.message.message_id}.ogg"
        await voice_file.download_to_drive(file_path)
        
        # Transcribe the audio using our new ears.py module
        user_text = ears.transcribe_audio(file_path)
        
        # Wipe the audio file from your PC so your hard drive doesn't fill up
        if os.path.exists(file_path):
            os.remove(file_path)
            
        if not user_text:
            await context.bot.send_message(chat_id=chat_id, text="❌ Sorry, the audio was too muffled for me to understand.")
            return
            
        print(f"📝 Transcribed Voice: {user_text}")
        
    # --- NORMAL TEXT MESSAGE ---
    else:
        if not update.message.text:
            return
        user_text = update.message.text
        print(f"📩 Received Text: {user_text}")

    try:
        user_lower = user_text.lower()
        
        # --- THE UNIVERSAL CLOCK & IDENTITY ---
        now_dt = datetime.datetime.now()
        now_str = now_dt.astimezone().isoformat()
        today_friendly = now_dt.strftime("%A, %B %d, %Y")
        today_iso = now_dt.strftime("%Y-%m-%d")
        live_time = now_dt.astimezone().strftime("%A, %B %d, %Y - %I:%M %p")
        
        system_msg = {
            "role": "assistant",
            "content": f"You are Nhako, a highly capable AI assistant created by Hakim(kimzam). You operate in Malaysia timezone (GMT+8). The current exact live time is {live_time}. Always use this time context if the user asks about 'now', 'today', 'tomorrow', or specific times."
        }
        
        calendar_keywords = [
            "calendar", "schedule", "agenda", "events", "plans",
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
            "today", "tomorrow", "weekend", "week", "month"
        ]
        
        # --- TOOL 1: READING Calendar ---
        if ("what" in user_lower or "do i have" in user_lower or "any" in user_lower) and any(word in user_lower for word in calendar_keywords):
            print("📅 Reading Calendar...")
            data = get_upcoming_events()
            
            prompt = f"""
            System: 
            - Today's date is {today_friendly}.
            - The numerical format for today is {today_iso}.
            
            User's Calendar Data: {data}
            User's Question: "{user_text}"
            
            Instructions:
            1. Answer the question naturally and conversationally.
            2. CRITICAL: If the user asks about "today", you MUST scan the data for ANY events containing the text '{today_iso}'.
            3. List ALL events you find for that specific day. Don't leave any out.
            4. If there are genuinely no events matching that date, say "You have nothing scheduled."
            5. Do NOT say "Based on the data" or mention the JSON data.
            """
            
            messages = memory.get_history() + [{"role": "user", "content": prompt}]
            reply_text = brain.generate_response(messages)
            
        # --- TOOL 2: DELETING from Calendar ---
        elif any(w in user_lower for w in ["delete", "remove", "cancel", "clear"]):
            print("🗑️ Deleting from Calendar...")
            
            prompt = f"""
            System: Today's date is {today_iso}.
            
            The user wants to delete something from their calendar: "{user_text}"
            
            Determine if they want to delete ONE specific event, or CLEAR an entire day.
            Return ONLY a raw JSON object with no markdown or backticks.
            
            If deleting a specific event, use format: {{"action": "specific", "query": "Name of event"}}
            If clearing a whole day, use format: {{"action": "clear_day", "date": "YYYY-MM-DD"}}
            """
            
            data = brain.generate_json(prompt)
            
            if data:
                if data.get("action") == "clear_day":
                    reply_text = clear_events_for_day(data["date"])
                else:
                    reply_text = delete_event(data["query"])
            else:
                reply_text = "I couldn't figure out exactly what to delete. Could you be more specific?"

        # --- TOOL 3: WRITING to Calendar ---
        elif any(w in user_lower for w in ["add", "remind", "schedule", "create", "set a", "put", "book", "appointment"]):
            print("✍️ Writing to Calendar...")
            
            prompt = f"""
            System Time: {now_str}.
            Extract event details from: "{user_text}"
            Return ONLY a raw JSON object with no markdown formatting, no backticks, and no explanations.
            Format: {{"summary": "Event Name", "start_time": "YYYY-MM-DDTHH:MM:00"}}
            """
            
            data = brain.generate_json(prompt)
            
            if data and "summary" in data and "start_time" in data:
                msg = create_event(data["summary"], data["start_time"]) 
                if "❌" in msg:
                    reply_text = f"Oops, something went wrong: {msg}"
                else:
                    reply_text = "It's on your calendar, Please check !" 
            else:
                reply_text = "I couldn't catch the exact time. Could you rephrase that?"

        # --- TOOL 3: WEB SEARCH ---
        elif "search" in user_lower or "look up" in user_lower:
            print("🔍 Triggering Web Search Tool...")
            await context.bot.send_chat_action(chat_id=chat_id, action='typing')
            
            # Clean up the query (e.g., turn "search for python" into "python")
            search_query = user_text.lower().replace("search for", "").replace("search", "").replace("look up", "").strip()
            
            # Go fetch the facts from DuckDuckGo
            web_facts = search.search_web(search_query)
            
            # Inject the live facts into Llama's system prompt!
            system_msg = {
                "role": "system",
                "content": f"You are NhakoBot. Use the following live web results to answer the user's question accurately and conversationally:\n\n{web_facts}"
            }
            
            messages = [system_msg] + [{"role": "user", "content": user_text}]
            reply_text = brain.generate_response(messages)
            
        # --- TOOL 5: NORMAL CHAT ---
        else:
            print("🧠 Searching Long-Term Memory...")
            past_memories = memory.search_memory(user_text, limit=3)
            await context.bot.send_chat_action(chat_id=chat_id, action='typing')
            memory_context = ""
            if past_memories:
                memory_context = "Here are some relevant past conversations with this user:\n"
                for role, content in past_memories:
                    if role == "user":
                        memory_context += f"- The user previously said: '{content}'\n"
            
            system_msg = {
                "role": "assistant",
                "content": f"You are Nhako, a highly capable AI assistant created by Hakim(kimzam). You operate in Malaysia timezone (GMT+8). The current exact live time is {live_time}. {memory_context}"
            }
            
            messages = [system_msg] + memory.get_history() + [{"role": "user", "content": user_text}]
            reply_text = brain.generate_response(messages)

        # --- SAVE HISTORY (To PostgreSQL) ---
        memory.add_interaction(user_text, reply_text)
        await context.bot.send_message(chat_id=chat_id, text=reply_text)
        
    except Exception as e:
        print(f"❌ Error in chat handler: {e}")
        await context.bot.send_message(chat_id=chat_id, text="Sorry, something went wrong with my local brain.")

if __name__ == '__main__':
    if not TELEGRAM_TOKEN:
        print("❌ ERROR: TELEGRAM_TOKEN is missing")
        exit(1)

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .read_timeout(60)     
        .write_timeout(60)
        .connect_timeout(60)
        .build()
    )
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler((filters.TEXT | filters.VOICE) & (~filters.COMMAND), chat))    
    print(f"🔒 NhakoBot SECURE LOCAL Mode running on {brain.AI_MODEL}...")
    application.run_polling()