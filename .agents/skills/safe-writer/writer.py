import sys
import os

if len(sys.argv) < 2:
    print("ERROR: Missing target file path.")
    sys.exit(1)

filepath = sys.argv[1]

print(f"Reading file content from standard input stream for: {filepath}...")
# Read the entire text block streaming from stdin
content = sys.stdin.read()

if not content.strip():
    print("ERROR: No content received over standard input.")
    sys.exit(1)

try:
    # Ensure directory path exists
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"SUCCESS: Successfully wrote {len(content)} characters to {filepath}")
except Exception as e:
    print(f"ERROR: Failed to write file. {str(e)}")
    sys.exit(1)
