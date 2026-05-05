# OpsAgent Usage Scenarios Guide

English | **[中文](./SCENARIOS.md)**

> Version: v2.3.0

---

## Table of Contents

1. [Issue Discovery (Read-Only Patrol Mode)](#1-issue-discovery-read-only-patrol-mode)
2. [Simple Issue Self-Healing](#2-simple-issue-self-healing)
3. [Complex Issue Diagnosis (Collaboration Mode)](#3-complex-issue-diagnosis-collaboration-mode)
4. [Large-Scale Project Issue Diagnosis](#4-large-scale-project-issue-diagnosis)
5. [Making OpsAgent Smarter (Notebook Extension)](#5-making-opsagent-smarter-notebook-extension)
6. [Multi-Target Monitoring](#6-multi-target-monitoring)
7. [Notifications and Escalation](#7-notifications-and-escalation)
8. [Emergency Handling](#8-emergency-handling)

---

## 1. Issue Discovery (Read-Only Patrol Mode)

### Scenario Description

You just deployed OpsAgent and aren't sure what it can or will do. You want it to observe the system, detect anomalies, and notify you — but **never execute any remediation actions**.

### When to Use

- Initial deployment, exploring the system's capabilities
- Cautious about production, disallowing automatic operations
- Only need monitoring and alerting, with manual remediation

### Configuration Steps

Start with the `--readonly` flag:

```bash
ops-agent --readonly
```

The agent runs in read-only mode: all patrol, observation, and diagnosis work normally, but no write operations (restarts, config changes, code changes, etc.) are executed.

### Switching at Runtime

No restart needed — enter commands in the interactive interface:

```
readonly on    # Switch to read-only mode
readonly off   # Switch to writable mode, allow auto-remediation
```

### Example

```bash
# Start in read-only mode
ops-agent --readonly --notebook /path/to/notebook

# Check current status
status
# Output includes: Read-only mode: Yes

# After confirming capabilities, switch to writable
readonly off
```

### Notes

- In read-only mode, the agent still consumes LLM tokens for diagnosis and analysis
- Detected anomalies will only trigger notifications, not remediation; close read-only mode to enable auto-healing
- `readonly on/off` takes effect immediately without restarting

---

## 2. Simple Issue Self-Healing

### Scenario Description

The system encounters a common fault (e.g., service OOM, configuration error, code bug). You want the agent to automatically locate the issue, generate a patch, submit a PR, verify the fix — all without human intervention.

### When to Use

- Agent deployed with read-only mode off
- Target system has accessible source code repositories
- The issue is relatively straightforward for the agent to locate and fix

### Configuration Steps

**Core: Configure `source_repos` in `notebook/config/targets.yaml`**

```yaml
targets:
  - name: web-prod
    type: ssh
    host: ubuntu@10.0.0.10
    key_file: ~/.ssh/id_rsa

    source_repos:
      - name: backend
        path: /opt/sources/backend           # Local source clone path on the agent workstation
        repo_url: git@github.com:mycompany/backend.git
        branch: main
        language: java
        build_cmd: mvn clean compile          # Compilation verification
        test_cmd: mvn test                    # Test verification
        git_host: github                      # PR platform: github | gitlab | noop | ""
        base_branch: main                     # PR target branch
        deploy_cmd: systemctl restart backend # Deployment command
        runtime_service: backend              # Corresponding runtime service name
        log_path: /opt/backend/logs/app.log
        # Path prefix mapping (handles container vs. host path differences)
        path_prefix_runtime: /app             # Container path prefix
        path_prefix_local: ""                 # Relative prefix in local clone
```

**GitHub PR Authentication (for automatic PRs):**

```bash
# Install gh CLI
brew install gh    # macOS
# Or: sudo apt install gh   # Ubuntu

# Authenticate (requires Personal Access Token with repo + pull_request permissions)
gh auth login --with-token <<< "ghp_xxxxxxxxxxxxxxxxxxxx"

# Verify
gh auth status
```

### Complete Self-Healing Flow

After detecting an anomaly, the agent automatically executes:

```
observe → diagnose → plan → patch → PR → deploy → verify
```

1. **observe**: Collect system metrics, logs, process status
2. **diagnose**: LLM analyzes root cause, locates source code position (via `source_repos` mapping)
3. **plan**: Generate remediation plan and verification steps
4. **patch**: Generate and apply a patch on the local source clone
5. **PR**: Commit code changes and create a Pull Request (if `git_host` is configured)
6. **deploy**: Execute `deploy_cmd` to deploy the patch
7. **verify**: Verify the fix (immediate verification + continuous observation)

**Patch Retry Mechanism**: `patch_loop` attempts up to `max_patch_attempts` times (default 3, configured in `notebook/config/limits.yaml`). Each retry adjusts the patch based on previous failure information.

### Auto-Merge and Production Observation

- After a PR is created and verification passes, the agent can auto-merge (limited by `max_auto_merges_per_day`, default 5 per day)
- After deployment, enters a **continuous observation period** (`watch_required_consecutive` default 2 consecutive passes, interval `watch_default_interval` default 60 seconds)

### Example

```bash
# Start normally (non-read-only mode)
ops-agent --notebook /path/to/notebook

# The agent detects anomalies and enters the healing flow automatically
# Or trigger self-healing manually:
self-fix backend service memory keeps growing after startup, suspected OOM

# Check healing status
status

# View incident records
show incidents/active/
```

### Notes

- `source_repos.path` is the path on the agent's workstation, **not** on the target server
- If the target runs in Docker/K8s, configure `path_prefix_runtime` / `path_prefix_local` to map between container and host paths
- `build_cmd` and `test_cmd` are required — after patch application, compilation and testing run before deployment proceeds
- The current default is the **push + deploy fast path**, skipping PR review; for strict PR mode, additional configuration is needed

---

## 3. Complex Issue Diagnosis (Collaboration Mode)

### Scenario Description

A complex system issue arises where the agent in automatic mode cannot pinpoint the root cause after multiple diagnostic rounds. You need to collaborate with the agent, providing human expertise and domain knowledge.

### When to Use

- Multiple diagnostic rounds yield no clear conclusion
- Root cause involves interactions across multiple systems/services
- Human experience is needed (business logic, historical context)
- Symptoms and root cause are far apart, requiring directional guidance

### Key Insight: Context Compression in Automatic Mode

⚠️ **This is the core reason to switch to collaboration mode.**

In automatic diagnostic mode, each `diagnose` and `plan` round's context is compressed by the `context_limits` mechanism:

- `diagnosis_json_chars` (default 700 chars): diagnostic conclusions are truncated when passed to plan
- `prev_summary_chars` (default 1000 chars): previous observation summaries are compressed
- `max_observations_chars` (default 8000 chars): all observation data is truncated when passed to LLM

This compression is sufficient for simple issues, but **critical clues for complex problems may be lost during compression**, causing the agent to "forget" earlier findings and fall into loops.

**Collaboration mode does not compress context** — the full conversation history is preserved in `collab_history`, and every LLM call can see all previous analysis. The trade-off is significantly higher token consumption.

### Entering Collaboration Mode

Enter in the interactive interface:

```
collab
```

Or in Chinese:

```
协作
```

### Interaction in Collaboration Mode

Collaboration mode uses an **intelligent turn-taking** mechanism:

1. **Agent autonomously runs read-only operations**: Information-gathering commands (`cat`, `grep`, `ps`, `kubectl logs`, etc. — 60+ types) are executed automatically without asking
2. **Pauses for human confirmation at key decisions**: Write operations, restarts, or uncertain directions trigger `[CONFIRM]` to wait for human approval
3. **Human can interject anytime**: Even during autonomous execution, you can input new information or instructions
4. **Intent markers control flow**: The agent uses three markers — `[CONTINUE]` (keep going autonomously), `[CONFIRM]` (wait for human approval), `[WAIT]` (wait for human input)

```
# Collaboration mode interaction example
[You] collab
[Agent] Entering collaboration mode. Describe the issue or let me analyze the current incident.
[You] Database connection pool keeps timing out, suspect it's related to last week's config change
[Agent] Let me check the database connection config and recent changes...
[Agent] $ cat /etc/app/db.yml
[Agent] $ journalctl -u backend --since "7 days ago" | grep -i "config"
[Agent] [CONTINUE] I see connection pool max_size changed from 50 to 10, investigating impact...
[You] Wait, that change was intentional. Check the slow query log first
[Agent] Understood, switching direction to slow query log...
[Agent] $ cat /var/log/mysql/slow.log | tail -100
[Agent] [CONFIRM] Found 3 queries taking >10s. Suggest killing these connections and adding indexes. Execute?
[You] Add indexes first, hold off on killing connections
[Agent] OK, executing only the index addition...
```

### When to Switch to Collaboration Mode

| Signal | Description |
|--------|-------------|
| Multiple rounds with no conclusion | `max_diagnose_rounds` exhausted, still COLLECT_MORE |
| Root cause spans multiple systems | Single-system observation can't explain the anomaly |
| Human experience needed | Business logic, historical background, ops expertise |
| Diagnostic context truncated | "truncated" markers in logs, key info may be lost |
| Agent repeatedly tries the same fix | Stuck in a loop, needs human guidance |

### Notes

- Collaboration mode token consumption is much higher than automatic mode — watch your `llm_tokens_per_hour` budget
- Consecutive autonomous rounds are capped at `max_collab_auto_rounds` (default 30), after which the agent pauses for human confirmation
- To exit collaboration mode: enter `exit`, `quit`, or similar keywords

---

## 4. Large-Scale Project Issue Diagnosis

### Scenario Description

Your project is large with many services and high traffic. Default parameter limits are insufficient for the agent to work effectively. You need to increase various limits.

### When to Use

- More than 5 services running simultaneously
- Large log volumes, default context window is insufficient
- Need to handle multiple incidents concurrently
- Diagnosis requires multiple rounds of deep analysis

### Configuration Adjustments

Edit `notebook/config/limits.yaml`:

```yaml
# ── Increase action rate limits ──
# Default 20/h, recommend 50-80 for large projects
max_actions_per_hour: 60

# ── Allow more concurrent incidents ──
# Default 2, recommend 5-8 for large projects
max_concurrent_incidents: 5

# ── Increase token budget ──
# Default 200k/h, recommend 500k-1M for large projects
llm_tokens_per_hour: 500000
llm_tokens_per_day: 3000000

# ── Increase diagnostic rounds ──
# Default 4 (code default), 25 in yaml example
# Recommend 8-12 for complex issues
max_diagnose_rounds: 10

# ── Increase total round limit ──
# Default 40, can go up to 60-80 for large projects
max_total_rounds: 60

# ── Increase fix attempt count ──
# Default 2 (code default), 3 in yaml example
max_fix_attempts: 4

# ── Patch generation retry count ──
max_patch_attempts: 5
```

Edit `notebook/config/context_limits.yaml`:

```yaml
# ── Enlarge context windows (when using 128k+ large-context models) ──

# Max characters of observation data passed to LLM diagnostic prompt
# Default 8000, recommend 16000-32000 for large projects
max_observations_chars: 16000

# Max characters of diagnostic conclusions passed to plan
# Default 700, recommend 1500-2000 for complex issues
diagnosis_json_chars: 1500

# Max characters of previous observation summary
# Default 1000, recommend 2000-3000 for multi-round diagnosis
prev_summary_chars: 2000

# Max characters of source context trace
# Default 2000, recommend 4000-6000 for large codebases
source_context_trace_chars: 4000

# Max characters of historical incident content
# Default 1000, recommend 2000-3000
incident_history_chars: 2000

# Max characters of playbook content
# Default 1500, recommend 3000 for experienced projects
playbook_content_chars: 3000
```

### Multi-Target Polling Interval Adjustment

When monitoring multiple targets, you can reduce resource consumption or improve response time by adjusting patrol intervals. Polling logic is controlled by the main loop, which visits each target sequentially. With many targets, adjust `criticality` in `targets.yaml` to influence patrol priority:

```yaml
targets:
  - name: core-api
    criticality: critical    # High priority, more frequent patrols
  - name: monitoring
    criticality: low         # Low priority, less frequent patrols
```

### Example

```bash
# After modifying config, the agent auto-reloads — no restart needed
# Check current limit status
limits
# Output:
# Hourly actions: 12/60
# Active incidents: 3/5
# Token budget: 120000/500000
```

### Notes

- Increasing limits means a **larger blast radius** — ensure you can quickly roll back if the agent makes mistakes
- Setting `max_concurrent_incidents` too high may overwhelm the agent and reduce per-incident quality
- When increasing `max_observations_chars`, verify your token budget is sufficient
- Config changes take effect without restarting the agent — the next main loop iteration auto-reloads

---

## 5. Making OpsAgent Smarter (Notebook Extension)

### Scenario Description

The built-in Basic Notebook only provides file storage and simple retrieval — the agent's "memory" is limited. You want the agent to have knowledge graphs, intelligent perception, and growth learning capabilities.

### When to Use

- Long-running deployment with accumulated incidents and playbooks
- High false-positive rate, need intelligent filtering
- Want the agent to learn and grow from historical experience

### Built-in Basic Notebook Limitations

- **No knowledge graph**: Incidents are isolated, no cross-service root cause pattern detection
- **No intelligent perception**: Cannot proactively recommend relevant playbooks or historical experience based on context
- **No growth engine**: Doesn't learn from successes/failures, starts from scratch each time
- **No trust evaluation**: Cannot adjust operation permissions based on historical performance

### Installing the Notebook Extension

Install the `smart-notebook` extension:

```bash
# Install in the notebook directory
cd /path/to/notebook
pip install ops-agent-smart-notebook

# Configure in notebook/config/notebook.yaml
```

Edit `notebook/config/notebook.yaml`:

```yaml
# Enable Smart Notebook extension
type: smart

# If not configured, defaults to basic
# type: basic
```

### Capabilities Added by the Extension

| Capability | Description | Feature |
|-----------|-------------|---------|
| **Knowledge Graph** | Auto-link incidents, playbooks, and service relationships | Linker engine |
| **Intelligent Perception** | Recommend relevant historical experience and playbooks based on context | Perception engine |
| **Growth Engine** | Learn from fix successes/failures, continuously improve | Scorecard + Trust evaluation |
| **False Positive Filtering** | Record and manage false-positive patterns, avoid repeated processing | FP Tracker |
| **Trust Evaluation** | Auto-adjust operation permissions based on historical performance | Trust Level |

### Extended Interactive Commands

```
# View growth scorecard
scorecard

# View current trust level
trust

# Mark false positive
fp <pattern description>
# Example: fp Memory usage 90% is normal during business peak

# View smart stats (auto-displayed in status command)
status
```

### Detailed Documentation

For the complete Notebook extension guide, see: [docs/notebook-extension.md](./docs/notebook-extension.md)

### Notes

- Smart Notebook token consumption is higher than Basic (knowledge graph queries and association analysis require additional LLM calls)
- Migration from Basic to Smart is seamless — existing data is auto-indexed
- To revert to Basic, change `type` back to `basic` in `notebook.yaml`

---

## 6. Multi-Target Monitoring

### Scenario Description

You need to simultaneously monitor multiple servers, Docker hosts, or K8s clusters, with the agent patrolling across all of them.

### When to Use

- Managing multiple environments (dev, staging, production)
- Monitoring different target types (SSH, Docker, K8s) simultaneously
- Need different patrol strategies for different targets

### Configuration Steps

Edit `notebook/config/targets.yaml`:

```yaml
targets:
  # Remote SSH server
  - name: web-prod
    type: ssh
    description: "Production web server"
    criticality: high
    host: ubuntu@10.0.0.10
    key_file: ~/.ssh/id_rsa

  # Local Docker
  - name: local-docker
    type: docker
    description: "Local docker-compose project"
    criticality: normal
    docker_host: ""
    compose_file: ./docker-compose.yaml

  # K8s cluster
  - name: prod-k8s
    type: k8s
    description: "Production K8s cluster"
    criticality: critical
    kubeconfig: ~/.kube/config
    context: prod-cluster
    namespace: default
```

### Target Switching

Use the `switch` command in the interactive interface:

```
# List all targets
targets

# Switch to a specific target
switch web-prod

# After switching, subsequent operations focus on that target
```

### Different Patrol Strategies per Target

The agent patrols all targets in a round-robin fashion. Use `criticality` to influence patrol priority:

- `critical`: Highest priority, immediate notification on issues
- `high`: High priority, fast response
- `normal`: Standard patrol frequency
- `low`: Low priority, reduced patrol frequency

### Notes

- All targets share the same Notebook and LLM instance
- When monitoring multiple targets, ensure `max_concurrent_incidents` is sufficient
- SSH targets require network connectivity and correct authentication
- K8s targets require a valid kubeconfig

---

## 7. Notifications and Escalation

### Scenario Description

You want the agent to proactively notify you of critical events and automatically escalate when human intervention is needed.

### When to Use

- Don't want to constantly watch the agent console
- Need mobile alerts
- Want automatic human notification when the agent can't handle an issue

### Configuration Steps

Copy and edit the notification config:

```bash
cp notebook/config/notifier.yaml.example notebook/config/notifier.yaml
```

Edit `notebook/config/notifier.yaml`:

```yaml
# Notification type: slack | dingtalk | feishu | feishu_app | none
type: feishu_app

# Webhook URL (for slack/dingtalk/feishu)
# Can also be set via OPS_NOTIFIER_WEBHOOK_URL environment variable
webhook_url: ""

# Feishu app bot configuration
feishu_app:
  app_id: "cli_xxx"
  app_secret: "xxx"
  chat_id: "oc_xxx"
  # Enable bidirectional interaction
  interactive:
    enabled: true
    callback_port: 9877
    encrypt_key: ""
    verification_token: ""

# Event types that trigger notifications
notify_on:
  - incident_opened       # New incident created
  - incident_closed       # Incident resolved
  - pr_merged             # PR merged
  - revert_triggered      # Rollback triggered
  - critical_failure      # Critical failure
  - llm_degraded          # LLM degradation
  - daily_report          # Daily report

# Quiet hours (critical notifications are unaffected)
quiet_hours:
  start: "22:00"
  end: "08:00"
  except_urgency:
    - critical
```

### Notification Policy

| Event | Default Notification | Description |
|-------|---------------------|-------------|
| `incident_opened` | ✅ | New issue detected |
| `incident_closed` | ✅ | Issue resolved |
| `pr_merged` | ✅ | Auto-fix PR merged |
| `revert_triggered` | ✅ | Fix failed, rollback triggered |
| `critical_failure` | ✅ | Critical failure, human needed |
| `llm_degraded` | ✅ | LLM service degraded |
| `daily_report` | ✅ | Daily patrol summary |

### Human Intervention on ESCALATE

The agent automatically escalates in these situations:

- Exceeded action rate limits
- Exceeded concurrent incident limits
- Re-triggered during cooldown period
- Consecutive fix failures
- Unrecognized anomalies

On escalation, the agent:

1. Sends an alert through the configured notification channel
2. Enters a waiting state, no longer attempting auto-remediation
3. Waits for human confirmation or guidance

### Feishu Bidirectional Interaction

With `interactive.enabled: true`, Feishu becomes a bidirectional interaction channel:

- **Receive notifications**: Agent pushes messages to the Feishu group
- **Send commands**: Reply to the agent in the Feishu group (e.g., `readonly on`, `status`)
- **Approve operations**: When the agent requests confirmation, reply "approve" or "reject" in Feishu

⚠️ Feishu interaction requires a publicly accessible callback port (`callback_port`).

### Notes

- Sensitive info like Webhook URLs should be passed via environment variables, not stored in plaintext config
- `quiet_hours` does not affect `critical`-level notifications — ensure urgent alerts are never silenced
- DingTalk and Slack use `webhook_url`; Feishu recommends `feishu_app` mode for bidirectional interaction

---

## 8. Emergency Handling

### Scenario Description

The agent executed a wrong action, or a system emergency has occurred. You need to immediately stop all agent operations.

### When to Use

- Agent is executing an incorrect fix, must be stopped immediately
- System has a severe failure, agent must not continue operating
- Agent behavior is abnormal, needs emergency pause

### Three Emergency Stop Methods

#### Method 1: CLI `freeze` Command

Enter in the interactive interface:

```
freeze
```

Effect: Triggers emergency stop and automatically enables read-only mode. The agent stops all operations but the process keeps running.

To unfreeze:

```
unfreeze
```

#### Method 2: File Marker

Create a marker file in the Notebook directory:

```bash
touch /path/to/notebook/EMERGENCY_STOP_SELF_MODIFY
```

Effect: Prevents the agent from executing self-repair operations (the `self-fix` command checks for this file and refuses to execute).

To remove:

```bash
rm /path/to/notebook/EMERGENCY_STOP_SELF_MODIFY
```

#### Method 3: Signal

Send a signal to the agent process:

```bash
# Find the agent process
ps aux | grep ops-agent

# Send SIGUSR1 to trigger emergency stop
kill -USR1 <pid>

# Send SIGTERM for graceful shutdown
kill <pid>

# Force kill (last resort)
kill -9 <pid>
```

### Recovery After Emergency Stop

1. **Assess impact**: Check records in `notebook/incidents/active/` to understand what the agent did
2. **Clear the stop**: Use `unfreeze` or delete the marker file
3. **Resume in read-only mode**: First restore observation in read-only mode, then enable remediation after confirming the system is stable

```
# Recovery steps
unfreeze              # Clear emergency stop
status                # Check current status
readonly on           # Start with read-only observation
# ... confirm system is stable ...
readonly off          # Re-enable auto-remediation
```

### Revert Mechanism After Misoperation

Every fix operation by the agent is recorded and reversible:

- **Git revert**: If the fix was submitted via PR, use `git revert` to roll back
- **Config rollback**: The agent backs up original files before modifying configs (`.bak` suffix)
- **Deployment rollback**:
  - Docker: `docker-compose down && docker-compose up -d --build` with the original image
  - K8s: `kubectl rollout undo deployment/<name>` to roll back to the previous version
  - Systemd: `systemctl restart <service>` with the rolled-back config

Manual rollback trigger:

```
# View fix operations in the incident record
show incidents/active/<incident-id>

# If a pre_tag (pre-fix git tag) exists, roll back to that version
git checkout <pre_tag>
```

### Notes

- `freeze` is the safest emergency stop — it preserves the agent process and context for investigation
- File marker method is useful when you can't access the interactive interface (e.g., remote SSH)
- `kill -9` is a last resort and may cause data inconsistency
- Before each self-repair, the agent creates a `pre_tag` (git tag) as a rollback anchor point
