import psycopg2
from pgvector.psycopg2 import register_vector
import ollama

# --- DATABASE SETTINGS ---
DB_HOST = "localhost" 
DB_PORT = "5432"
DB_NAME = "bot_memory"
DB_USER = "nhako"
DB_PASS = "ur_secure_password_here" 

# The translator model we just downloaded
EMBEDDING_MODEL = "nomic-embed-text"

def get_db_connection():
    """Connects to the PostgreSQL database."""
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )
    
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    conn.commit()
    cur.close()    
    register_vector(conn)
    return conn

def init_db():
    """Creates the database table and vector extension if they don't exist."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. Enable the pgvector extension inside Postgres
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        
        # 2. Create the memory table
        # nomic-embed-text produces vectors with exactly 768 dimensions!
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_memory (
                id SERIAL PRIMARY KEY,
                role VARCHAR(50),
                content TEXT,
                embedding VECTOR(768),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Postgres Database connected and initialized!")
    except Exception as e:
        print(f"❌ Database error: {e}")

def get_embedding(text):
    """Asks Ollama to mathematically translate text into a 768-number coordinate."""
    response = ollama.embeddings(model=EMBEDDING_MODEL, prompt=text)
    return response["embedding"]

def add_interaction(user_text, ai_response):
    """Saves the conversation to the long-term database."""
    try:
        # Turn the text into mathematical vectors
        user_vector = get_embedding(user_text)
        ai_vector = get_embedding(ai_response)
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Save User Message
        cur.execute(
            "INSERT INTO chat_memory (role, content, embedding) VALUES (%s, %s, %s)",
            ("user", user_text, user_vector)
        )
        
        # Save AI Response
        cur.execute(
            "INSERT INTO chat_memory (role, content, embedding) VALUES (%s, %s, %s)",
            ("assistant", ai_response, ai_vector)
        )
        
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ Error saving memory: {e}")

def get_history():
    """Gets the last 10 messages for immediate short-term context."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Fetch the 10 most recent messages, ordered properly
        cur.execute("""
            SELECT role, content FROM (
                SELECT id, role, content FROM chat_memory ORDER BY id DESC LIMIT 10
            ) sub ORDER BY id ASC;
        """)
        
        rows = cur.fetchall()
        cur.close()
        conn.close()
        
        # Format exactly how Ollama expects it
        history = [{"role": row[0], "content": row[1]} for row in rows]
        return history
        
    except Exception as e:
        print(f"❌ Error reading history: {e}")
        return []

def search_memory(query, limit=3):
    """Searches the database for past messages mathematically similar to the query."""
    try:
        query_vector = get_embedding(query)
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # The <=> operator tells pgvector to find the closest vectors (Cosine Distance)
        cur.execute("""
            SELECT role, content 
            FROM chat_memory 
            ORDER BY embedding <=> %s::vector
            LIMIT %s;
        """, (query_vector, limit))
        
        results = cur.fetchall()
        cur.close()
        conn.close()
        
        return results
        
    except Exception as e:
        print(f"❌ Error searching memory: {e}")
        return []

# Run initialization immediately when the file loads!
init_db()