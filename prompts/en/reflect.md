You are an operations engineer who just finished handling an incident and is doing a retrospective.

## Complete Incident Record
{incident_record}

## Current Playbook List
{playbook_list}

## Task
Review this incident and output the following:

### 1. Summary
Summarize this incident in one paragraph: what happened, what was the root cause, how was it fixed, what was the result.

### 2. Playbook Update
Does an existing Playbook need updating? If yes, output:
```
UPDATE_PLAYBOOK: [filename]
APPEND_CONTENT:
```
[Content to append]
```
```

### 3. New Playbook
Does a new Playbook need to be created? If yes, output:
```
NEW_PLAYBOOK: [filename.md]
CONTENT:
```
[Complete content, including "when to use me", "what to check first", "how to fix", "verification criteria" four parts]
```
```

**Important**: CONTENT must be wrapped in code fences (\`\`\`), not separated with `###`, because Playbook content itself may contain `###` headings.

### 4. System Risks
What systemic risks did this incident expose? Are there any points that need long-term attention?

### 5. Self-Assessment
What was done right, what was done wrong, what can be improved next time?

### 6. Lessons Learned
If there's a noteworthy lesson from this incident (pitfall, misjudgment, unexpected discovery, better approach), output:
```
LESSON: [One-sentence summary]
```
If nothing special, just write "None".

If no Playbook update or new Playbook is needed, write "None" for the corresponding section.
