import sys
from ollama import web_search

if len(sys.argv) < 2:
    print("Error: Missing search query.")
    sys.exit(1)

query = " ".join(sys.argv[1:])

try:
    print(f"🔍 Searching the web for: '{query}'...\n")
    # Fetch results from the Ollama Search API
    response = web_search(query=query, max_results=3)
    
    if not response.results:
        print("No results found.")
        sys.exit(0)

    # Format the snippets cleanly for your 9B model
    for idx, result in enumerate(response.results, 1):
        print(f"[{idx}] {result.title}")
        print(f"URL: {result.url}")
        print(f"Snippet: {result.content}\n")
        print("-" * 40)

except Exception as e:
    print(f"Error during search: {e}")
    sys.exit(1)
