import os
from faster_whisper import WhisperModel

# --- CONFIGURATION ---
MODEL_SIZE = "base"

print(f"🎧 Loading AI Acoustic Engine ({MODEL_SIZE} model)...")
model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
print("✅ Ears module is awake and listening!")

def transcribe_audio(file_path):
    """Takes an audio file path, transcribes it, and returns the spoken text."""
    print(f"🎙️ Processing audio file: {file_path}")
    
    try:
        segments, info = model.transcribe(file_path, beam_size=5)
        
        print(f"🗣️ Detected language: '{info.language}' (Confidence: {info.language_probability:.2f})")
        
        full_text = ""
        for segment in segments:
            full_text += segment.text + " "
            
        clean_text = full_text.strip()
        print(f"📝 Transcription complete: {clean_text}")
        
        return clean_text
        
    except Exception as e:
        print(f"❌ Transcription error: {e}")
        return None