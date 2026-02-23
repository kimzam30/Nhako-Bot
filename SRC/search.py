from duckduckgo_search import DDGS

def search_web(query, max_results=3):
    """Searches DuckDuckGo and returns the top snippets."""
    print(f"🌐 Searching the web for: '{query}'...")
    
    try:
        # DDGS().text() searches the standard web results
        results = DDGS().text(query, max_results=max_results)
        
        if not results:
            return "No results found on the web."
            
        # Format the snippets into a clean string for the AI to read
        formatted_results = "Here is the latest information from the web:\n"
        for i, res in enumerate(results):
            formatted_results += f"{i+1}. {res['title']}: {res['body']}\n"
            
        print("✅ Web search complete!")
        return formatted_results
        
    except Exception as e:
        print(f"❌ Web search error: {e}")
        return "Sorry, I couldn't reach the internet right now."