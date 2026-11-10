---
name: safe-writer
description: Use this skill whenever you need to create a new file or completely rewrite an existing file. Do NOT use PowerShell echo or standard argument parameters
---

# Safe File System Writing Protocol
You are a local 9B model. Large code blocks truncate when passed as raw parameters. You MUST stream code via a terminal pipeline block instead.

## Action Protocol
To write a file, you must output your code inside a standard terminal command block using a PowerShell Here-String template. 

### Execution Command Template
Execute a terminal command using this exact structure to pipe your code cleanly into the script:

@'
[INSERT YOUR COMPLETE, UNTRUNCATED CODE HERE]
'@ | python .cline/skills/safe-file-writer/writer.py "relative/path/to/file.py"

## Execution Rules
1. Wrap the target code exactly between the `@'` and `'@` symbols (PowerShell Here-String).
2. Pipe it using the `|` symbol straight into the Python writer utility.
3. This prevents all token truncation and command line buffer limitations.