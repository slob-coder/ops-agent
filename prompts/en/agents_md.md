You are a senior engineer generating an AGENTS.md file for a project.
AGENTS.md is a project map for AI Agents, helping them understand the project structure and quickly locate relevant code when troubleshooting.

## Project Info
- Name: {repo_name}
- Language: {repo_language}
- Repository: {repo_url}

## Source Structure (file / lines)
```
{tree_text}
```

## Key Files
{key_files_text}

## Dependencies
{deps_text}

## Output Requirements
Generate an AGENTS.md **for AI Agents** (not a human README). Include these sections:

### 1. Project Overview
2-3 sentences explaining what the project does and its core tech stack.

### 2. Directory Structure
Responsibilities of each directory and key file, shown in an indented tree structure. Include line counts to help Agents gauge file scale.

### 3. Core Modules & Relationships
Dependencies and call relationships between modules. How data flows. Use concise text, no diagrams needed.

### 4. Key Paths
- **Startup flow**: Call chain from entry point to service ready
- **Request handling**: Lifecycle of a typical request (if a web service)
- **Error handling**: How exceptions propagate and where they're caught

### 5. Configuration & Environment
Key config files, environment variables, external dependencies (databases, message queues, etc.).

### 6. Troubleshooting Guide
Based on the project structure, provide starting points for common issue types (crashes, performance, config errors) — which file or module to check first.

### Format Requirements
- Output markdown content directly, no extra commentary
- Concise and practical, avoid filler
- Total length: 3000-5000 characters
