English | **[中文](./README.md)**

# OpsAgent — Digital Ops Employee

> An always-on, self-improving AI ops Agent that works under human supervision.
>
> It's not a monitoring system, not a log pipeline — it's a **digital colleague that uses the shell, takes notes, consults you, and fixes code on its own**.

**Current version: v2.3.0** — 6 Sprints + state machine refactoring complete, 493 tests passing.

Full capabilities: multi-target management · autonomous diagnosis & repair · source code bug auto-fix · auto PR merge & production observation · crash self-recovery · LLM degradation · audit/metrics/IM notifications/daily reports.

See [USER_GUIDE.en.md](./USER_GUIDE.en.md) for the complete usage guide.

---

## How It Works

```
┌──────────────┐         SSH / docker / kubectl         ┌──────────────────┐
│   OpsAgent   │ ──────────────────────────────────────►│   Target System  │
│  (Ops Station)│ ◄──────────────────────────────────────│  (Your Servers)  │
└──────┬───────┘                                        └──────────────────┘
       │
       ├── Observe   (tail / grep / dmesg / systemctl ...)
       ├── Assess    (LLM: normal or abnormal?)
       ├── Diagnose  (LLM: what's the root cause? ← auto-locate source code)
       ├── Fix       (restart / change config / generate patch → local build & test → push → PR → auto-merge)
       ├── Verify    (production observation 5 min → recurrence detected → auto-revert)
       └── Reflect   (write notes, update Playbook, IM notification, daily report)
```

The entire loop is **autonomous**, but all L2+ actions are constrained by blast radius limits, and anything uncertain is proactively escalated to a human.

---

## 5 Core Capabilities at a Glance

```
Core
├── Multi-target support          (SSH / Docker / K8s / Local)
├── Real-time conversational UI   (CLI, interruptible at any time)
├── Autonomous anomaly detection & repair  (observe → diagnose → act → verify → reflect)
├── Source code bug fix           (locate → generate patch → local verify → PR → production watch → auto-revert on recurrence)
└── Full blast radius limits      (frequency / concurrency / cooldown / token / auto-merge)

Reliability
├── Crash self-recovery           (state.json + recover_state)
├── LLM degradation               (retry + auto-readonly + auto-recovery)
├── Notebook integrity            (git fsck + remote backup)
└── Watchdog + systemd            (ops-agent.service + scripts/watchdog.sh)

Observability
├── Audit log                     (append-only JSONL, daily rotation)
├── Prometheus metrics            (/metrics endpoint)
├── IM notifications              (Slack / DingTalk / Feishu + notification policy)
└── Daily health report           (LLM summary + template fallback)
```

---

## Quick Start

Two ways to get started — pick what fits you:

---

### Option 1: Local Install (Recommended)

Best for: long-running deployments, monitoring real servers, production use

**Step 1 — One-line install**

```bash
curl -fsSL https://raw.githubusercontent.com/slob-coder/ops-agent/main/scripts/install-quick.sh | bash
```

The script automatically: checks Python ≥ 3.9 → clones to `~/.ops-agent` → creates an isolated venv → installs dependencies → sets up the `ops-agent` command.

> If the `ops-agent` command isn't recognized after install, open a new terminal or run `export PATH="$HOME/.ops-agent/bin:$PATH"`.
>
> Custom install directory: `OPS_AGENT_HOME=/opt/ops-agent curl -fsSL ... | bash`

**Step 2 — Initialize configuration**

```bash
ops-agent init
```

Interactive walkthrough that generates all config files and `.env`:

```
? LLM Provider (anthropic): anthropic
? API Key: sk-ant-****
? Target name: web-prod
? Target type (ssh): ssh
? SSH address (user@host): ubuntu@10.0.0.10
? SSH key path (optional, Enter to skip):
? Configure a source repo? [y/N]: n
? Notification type (none): none
✅ notebook/config/targets.yaml
✅ notebook/config/limits.yaml
✅ notebook/config/permissions.md
✅ notebook/.env
🎉 Setup complete!
```

Credentials like the LLM API key are automatically written to `.env` — no manual export needed.

**Step 3 — Launch**

```bash
ops-agent                  # Start with init-generated config (Chinese by default)
ops-agent --lang en        # Start in English mode
ops-agent --readonly       # Read-only mode (observe only, no actions)
ops-agent check            # Validate configuration
ops-agent check --test-llm # Validate + test LLM connectivity

Language priority: --lang flag > OPS_AGENT_LANG env var > config file > default (zh).
```

**Step 4 — Talk to the Agent**

```
> status                       # Check Agent status
> Any nginx errors recently?   # Natural language question
> readonly on / readonly off   # Toggle read-only mode
> stop                         # Stop current investigation
> pause / resume               # Pause/resume patrol
> quit                         # Exit
```

---

### Option 2: Docker

Best for: no Python install needed, CI/CD environments, quick tryout

**Quick Demo Mode**

Just one API key, zero config:

```bash
git clone https://github.com/slob-coder/ops-agent.git && cd ops-agent/docker
cp .env.example .env
# Edit .env — just fill in: OPS_LLM_API_KEY=sk-ant-...
docker compose run --rm ops-agent demo
```

Demo mode auto-generates mock config and monitors the container itself. Once inside, you can ask natural language questions and check status.

> Demo monitors the container internally — it's mainly a patrol demo. To monitor real servers, see "Production Deploy" below.

**Docker Production Deploy**

Step 1 — Clone and configure:

```bash
git clone https://github.com/slob-coder/ops-agent.git && cd ops-agent/docker
cp .env.example .env
```

Edit `.env` with your settings. Minimum two lines:

```env
OPS_LLM_API_KEY=sk-ant-...        # Required: LLM API Key
OPS_TARGET_TYPE=local             # local=monitor container itself | ssh=remote server
```

To monitor an SSH server:

```env
OPS_LLM_API_KEY=sk-ant-...
OPS_TARGET_TYPE=ssh
OPS_TARGET_HOST=ubuntu@10.0.0.10  # SSH address
OPS_TARGET_KEY_FILE=/root/.ssh/id_rsa  # Key (auto-mounts ~/.ssh)
```

> `.env.example` has detailed descriptions and examples for every parameter.

Step 2 — Initialize and validate:

```bash
docker compose run --rm ops-agent init --from-env   # Generate config files
docker compose run --rm ops-agent check --test-llm   # Validate + test connectivity
```

Step 3 — Launch:

```bash
docker compose up -d               # Start in background
docker compose logs -f             # View logs
curl localhost:9876/healthz        # Health check
```

---

## Project Structure

```
ops-agent/
├── main.py                       # Entry point, argument parsing, launch OpsAgent
├── src/
│   ├── init.py                    # ops-agent init interactive config wizard
│   ├── core.py                   # OpsAgent class, main loop + state machine
│   ├── context_limits.py         # Context window limit configuration
│   ├── reporter.py               # Daily health report
│   │
│   ├── agent/                    # Thinking layer — Mixins
│   │   ├── pipeline.py           # OODA pipeline (observe/assess/diagnose/plan/execute/verify/reflect)
│   │   ├── parsers.py            # JSON parsing, command extraction, targeted observe
│   │   ├── prompt_engine.py      # Prompt template loading/filling
│   │   ├── human.py              # Human message handling, free chat, collaboration mode
│   │   ├── metrics.py            # Prometheus metrics mixin
│   │   └── pr_workflow.py        # PR create/merge/observe mixin
│   │
│   ├── infra/                    # Perception + Action layer
│   │   ├── tools.py              # Command execution (SSH/Docker/K8s/Local)
│   │   ├── targets.py            # Multi-target + SourceRepo configuration
│   │   ├── chat.py               # Terminal interaction (prompt_toolkit)
│   │   ├── llm.py                # LLM abstraction layer (includes RetryingLLM degradation)
│   │   ├── notebook.py           # Notebook read/write + git integrity
│   │   ├── deploy_watcher.py     # Deploy signal monitoring
│   │   ├── production_watcher.py # Recurrence detection
│   │   ├── notifier.py           # IM notifications (Slack/DingTalk/Feishu)
│   │   └── git_host.py           # GitHub/GitLab CLI abstraction
│   │
│   ├── safety/                   # Safety & Constraints
│   │   ├── trust.py              # Trust level engine + ActionPlan
│   │   ├── safety.py             # Emergency stop + command blacklist
│   │   ├── limits.py             # Blast radius limits
│   │   ├── patch_generator.py    # LLM patch generation
│   │   ├── patch_applier.py      # git apply + build + test
│   │   ├── patch_loop.py         # Retry loop (max 3 attempts)
│   │   └── revert_generator.py   # Auto-revert
│   │
│   ├── repair/                   # Self-repair & source code location
│   │   ├── self_repair.py        # Self-repair system
│   │   ├── self_context.py       # Self-repair context collection
│   │   ├── source_locator.py     # Anomaly → source code reverse lookup
│   │   └── stack_parser.py       # Multi-language traceback parsing
│   │
│   └── reliability/              # Reliability foundation
│       ├── state.py              # Crash recovery state persistence
│       ├── pending_events.py     # Pending event queue
│       ├── health.py             # Health check endpoint + /metrics
│       └── audit.py              # Append-only audit log
│
├── prompts/                      # 7 core prompt templates
├── templates/pr-body.md          # PR description template
│   └── targets.example.yaml  # Target config template (with comments)
├── notebook/                     # Agent's notebook (git repo)
│   ├── config/
│   │   ├── targets.yaml
│   │   ├── permissions.md
│   │   ├── limits.yaml
│   │   └── notifier.yaml.example
│   ├── playbook/
│   ├── incidents/
│   ├── lessons/
│   └── audit/
├── docker/                        # Docker deployment
│   ├── compose.yaml               # Docker Compose configuration
│   └── .env.example               # Environment variable template (with comments)
├── tests/                        # 10 test files
├── ops-agent.service             # systemd unit
├── scripts/
│   ├── watchdog.sh               # External health watchdog
│   └── install.sh                # One-line install
└── README.md / USER_GUIDE.md
```

---

## Core Concepts

| Concept | Description |
|---|---|
| **Notebook** | The Agent's memory — a git repo full of markdown files. You can open and edit them directly; the Agent reads them on the next loop. |
| **Playbook** | `notebook/playbook/*.md` — describes "what to do when X happens". Adding a new fix capability = drop a markdown file in this directory. |
| **Incident** | Each anomaly the Agent discovers and handles, fully documented in `notebook/incidents/`. |
| **Target** | A managed target system (SSH/Docker/K8s/Local). Configured in `notebook/config/targets.yaml`. |
| **SourceRepo** | A local source clone associated with a target, for reverse-tracing anomalies and generating patches. |
| **Trust Level** | L0 read-only / L1 write notes / L2 service operations / L3 code changes / L4 always forbidden |
| **Blast Radius Limits** | Frequency / concurrency / cooldown / token / auto-merge PR count — any breach forces human escalation. |
| **Emergency Stop** | Triggered via file / signal / CLI — the Agent immediately switches to read-only. |

---

## Notebook Extensibility

ops-agent's Notebook is pluggable. The built-in Basic Notebook (filesystem + git) is the default, and you can install third-party extension Notebooks to enhance capabilities (knowledge graphs, intelligent perception, growth engines, etc.). Extension packages are auto-detected at startup — zero configuration.

**→ Full docs: [docs/notebook-extension.md](./docs/notebook-extension.md)** (interface protocol, custom extension development, Docker/local install steps, private repos, existing data handling, verification methods)

---

## Testing

```bash
# Run all tests (no LLM config needed, all stdlib stubs)
cd tests
for t in test_basic test_blacklist test_sprint1 test_sprint2 \
         test_sprint3 test_sprint4 test_sprint5 test_sprint6; do
    python $t.py
done
```

Test stats: **493 items, 100% passing**.

| Sprint | Scope | Tests |
|---|---|---|
| 0 (baseline) | basic + blacklist | 85 |
| 1 | multi-target / blast radius / emergency stop | 53 |
| 2 | stack parsing / source code location | 51 |
| 3 | patch generation / apply / verify | 56 |
| 4 | git host / deploy / recurrence / revert | 74 |
| 5 | state persistence / queue / health / RetryingLLM | 79 |
| 6 | audit / notifications / daily report / metrics | 95 |
| **Total** | | **493** |

---

## Environment Variables

### LLM

| Variable | Default | Description |
|---|---|---|
| `OPS_LLM_PROVIDER` | `anthropic` | LLM provider (anthropic / openai / zhipu) |
| `OPS_LLM_MODEL` | `claude-sonnet-4-20250514` | Model name |
| `OPS_LLM_API_KEY` | (none) | API Key |
| `OPS_LLM_BASE_URL` | (none) | Custom API base URL |

### ops-agent init (--from-env mode)

| Variable | Required | Description |
|---|---|---|
| `OPS_TARGET_TYPE` | ✓ | Target type (ssh / docker / k8s / local) |
| `OPS_TARGET_NAME` | | Target name (default `my-{type}`) |
| `OPS_TARGET_HOST` | required for ssh | SSH address (user@host) |
| `OPS_TARGET_PORT` | | SSH port (default 22) |
| `OPS_TARGET_KEY_FILE` | | SSH key path |
| `OPS_TARGET_PASSWORD_ENV` | | SSH password environment variable name |
| `OPS_TARGET_CRITICALITY` | | Criticality (low/normal/high/critical) |
| `OPS_TARGET_DESCRIPTION` | | Target description |
| `OPS_REPO_NAME` | | Repo name (enables source config if present) |
| `OPS_REPO_PATH` | required if repo enabled | Local clone path |
| `OPS_REPO_URL` | | Git remote URL |
| `OPS_REPO_LANGUAGE` | | Programming language |
| `OPS_REPO_BUILD_CMD` | | Build command |
| `OPS_REPO_TEST_CMD` | | Test command |
| `OPS_REPO_DEPLOY_CMD` | | Deploy command |
| `OPS_REPO_GIT_HOST` | | Git hosting (github/gitlab) |
| `OPS_NOTIFIER_TYPE` | | Notification type (none/slack/dingtalk/feishu/feishu_app) |
| `OPS_NOTIFIER_WEBHOOK_URL` | (none) | Overrides webhook in notifier.yaml, recommended for production |
| `OPS_FEISHU_APP_ID` | | Feishu app App ID (feishu_app mode) |
| `OPS_FEISHU_APP_SECRET` | | Feishu app App Secret (feishu_app mode) |
| `OPS_FEISHU_CHAT_ID` | | Feishu group chat chat_id (feishu_app mode) |

### Feishu Notification Configuration

OpsAgent supports two Feishu notification methods:

**Option 1: Webhook Bot (simple, recommended for beginners)**

1. Add a "Custom Bot" in your Feishu group chat and get the Webhook URL
2. Run `ops-agent init`, select `feishu` as notification type, and enter the Webhook URL
3. Or manually create `notebook/config/notifier.yaml`:

```yaml
type: feishu
webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
```

> Webhook bots can only post messages to group chats; they cannot receive replies.

**Option 2: Custom App Bot (full-featured, supports two-way interaction)**

1. Create an enterprise custom app on the [Feishu Open Platform](https://open.feishu.cn/app)
2. Add the "Bot" capability and obtain the App ID and App Secret
3. Under "Permission Management", enable: `im:message:send_as_bot`
4. Create a group chat, add the bot to the group, and get the group's `chat_id` (Group settings → Group card → Copy group link; `chat_id` is in the URL)
5. Configure `notebook/config/notifier.yaml`:

```yaml
type: feishu_app
feishu_app:
  app_id: "cli_xxxxxxxx"
  app_secret: "xxxxxxxxxxxxxxxx"
  chat_id: "oc_xxxxxxxx"
```

6. **Security tip**: Don't commit `app_secret` to git — override it with environment variables:

```bash
export OPS_FEISHU_APP_ID="cli_xxxxxxxx"
export OPS_FEISHU_APP_SECRET="xxxxxxxxxxxxxxxx"
export OPS_FEISHU_CHAT_ID="oc_xxxxxxxx"
```

**Enable Feishu two-way interaction** (optional):

The Agent can receive and reply to @mentions in Feishu group chats:

```yaml
feishu_app:
  app_id: "cli_xxxxxxxx"
  app_secret: "xxxxxxxxxxxxxxxx"
  chat_id: "oc_xxxxxxxx"
  interactive:
    enabled: true
    callback_port: 9877      # Feishu event callback port (must be publicly reachable)
    encrypt_key: ""          # Feishu Open Platform → Event subscription → Encrypt key
    verification_token: ""   # Feishu Open Platform → Event subscription → Verification token
```

Configure event subscription on the Feishu Open Platform:
- Request URL: `http://<your-server-ip>:9877/feishu/event`
- Subscribe to event: `im.message.receive_v1` (receive messages)

> Two-way interaction requires the server to have a public IP so Feishu callbacks can reach it.

---

## Documentation

- **[docs/notebook-extension.md](./docs/notebook-extension.md)** — Notebook extensibility: interface protocol, custom extension development, install steps
- **[USER_GUIDE.en.md](./USER_GUIDE.en.md)** — Complete usage guide covering configuration, operations, troubleshooting, and ops best practices
- **[notebook/lessons/](./notebook/lessons/)** — Sprint retrospectives documenting design decisions and tradeoffs
- **[examples/docker-compose-demo/](./examples/docker-compose-demo/)** — End-to-end demo environment

## License

MIT
