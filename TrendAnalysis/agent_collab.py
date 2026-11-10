import json
import requests
from ollama import web_search

# Configuration for your local Ollama instance
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL_NAME = "qwen3.5:9b"  # Replace with your exact Ollama model string if different

# Define the distinct personas and instructions for each agent
AGENT_A_SYSTEM = (
    "You are a strict Software Architect. Your job is to break down user requests "
    "into technical specifications, edge cases, and architectural blueprints. "
    "Keep your responses concise, structured, and focused purely on design, NOT writing code."
)

AGENT_B_SYSTEM = (
    "You are an expert Python Developer. Your job is to read technical specifications "
    "provided by the Software Architect and implement them into clean, production-ready Python code. "
    "Provide the code and a very brief explanation."
)

def get_web_context(query):
    """Fetches web snippets to pass as context to the agents."""
    try:
        res = web_search(query=query, max_results=2)
        context_str = "\n".join([f"Source: {r.url}\nContent: {r.content}" for r in res.results])
        return f"\n--- LIVE WEB CONTEXT FOUND ---\n{context_str}\n-----------------------------"
    except:
        return ""

def chat_with_agent(system_prompt, conversation_history):
    """Helper function to send the history and system prompt to local Ollama."""
    # Construct the payload with the system prompt injected at the start
    messages = [{"role": "system", "content": system_prompt}] + conversation_history
    
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False  # Set to True if you want to parse streaming text chunk by chunk
    }
    
    try:
        response = requests.post(OLLAMA_URL, json=payload)
        response.raise_for_status()
        return response.json()["message"]["content"]
    except Exception as e:
        print(f"\n[Error connecting to Ollama]: {e}")
        return None

def run_agent_simulation(user_goal, rounds=2):
    print(f"\n🚀 Starting Local Agent Collaboration")
    print(f"Goal: {user_goal}\n" + "="*50)
    
    # Track separate histories to mimic distinct entities
    architect_history = [{"role": "user", "content": f"Design a system for: {user_goal}"}]
    developer_history = []
    
    last_response = f"The user wants to build: {user_goal}. Provide the structural blueprint."

    for i in range(1, rounds + 1):
        web_data = get_web_context("Latest Python 2026 features")
        print(f"\n Got web context:\n{web_data}\n" + "-"*40)
        print(f"\n🤖 [ROUND {i}] --- ARCHITECT IS THINKING... ---")
        architect_history.append({"role": "user", "content": last_response})
        architect_output = chat_with_agent(AGENT_A_SYSTEM, architect_history)
        
        if not architect_output: break
        print(f"\n[Architect]:\n{architect_output}\n" + "-"*40)
        
        # Pass the Architect's design directly into the Developer's prompt
        print(f"\n💻 [ROUND {i}] --- DEVELOPER IS THINKING... ---")
        developer_history.append({"role": "user", "content": f"Implement this specification:\n{architect_output}"})
        developer_output = chat_with_agent(AGENT_B_SYSTEM, developer_history)
        
        if not developer_output: break
        print(f"\n[Developer]:\n{developer_output}\n" + "="*50)
        
        # Prepare the next round feed (Developer's feedback or roadblocks back to Architect)
        last_response = f"Here is my current implementation breakdown. Review it for gaps:\n{developer_output}"

if __name__ == "__main__":
    # Define what you want them to build
    task = "A command-line task manager application that saves tasks to a local JSON file with priority levels."
    
    # Run the simulation for 2 back-and-forth turns
    run_agent_simulation(task, rounds=2)
