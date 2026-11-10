---
name: web-browser
description: Use this skill whenever the user asks for real-time information, weather, current documentation, or recent news that requires a live internet search.
---

# Web Browsing Protocol (Local Engine)
You are a local 9B model and do not have a built-in live internet connection inside your native context window. To find accurate, real-time facts, you must run our Python bridge script.

## Execution Pattern
When a user asks a question about recent events or code libraries that require documentation lookups, execute a terminal command using this exact structure:

python .cline/skills/web-browser/search.py "<your_search_query_here>"

## Output Rules
1. Run the command to retrieve the web snippets.
2. Read the script output context carefully.
3. Formulate your final response to the user based strictly on the factual data returned by the terminal script.
