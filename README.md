# 🤖 Hannibal Hub Agents: Distributed GitHub App Webhook Orchestrator

A unified, event-driven service that handles GitHub webhooks via a serverless router, queues event processing asynchronously using **Google Cloud Pub/Sub**, and runs an agentic loop powered by **Gemma 4** & **Google ADK** to safely interact with GitHub repositories.

---

## 🏗️ System Architecture

The orchestrator is designed for high reliability, security, and zero-trust event execution using a decoupled, distributed architecture:

```mermaid
flowchart TD
    GH["GitHub Webhook Event"] -->|"1. HTTPS POST"| Router["Cloud Run Function Router"]
    Router -->|"Verify HMAC Signature"| Auth["Signature Validator"]
    Router -->|"Quick ACK 202 Accepted"| GH
    Router -->|"Normalize & Enqueue"| Queue[("Google Cloud Pub/Sub")]
    
    Queue -->|"Trigger Pull"| Worker["Background Worker Task"]
    Worker -->|"App Authentication"| Creds["GitHub App JWT / Installation Token"]
    Worker -->|"Instant 👀 Reaction (0-Token)"| GH_React["GitHub Comment Reaction"]
    Worker -->|"Load Context"| GH_API["GitHub REST API"]
    Worker -->|"Decide Actions"| Gemma["Gemma 4 Planner / Gemini API"]
    Worker -->|"Policy Verification"| Policy{"Mutations Allowed?"}
    Policy -->|"Yes"| Exec["Execute Tool Actions"]
    Policy -->|"No / Dry Run"| Log["Log Planned Actions"]
    
    Exec -->|"Writeback"| GH_Write["GitHub Comments, Reviews, PRs"]
```

---

## 📁 Repository Structure

```
├── .agents/skills/          # Localized agent operational skills
│   ├── gcloud-logging/      # GCP logging inspection protocol & project matrix
│   ├── github-bot-identity/ # Bot identity authentication & credentials switch
│   └── github-pr-manager/   # End-to-end GitHub PR lifecycle management
├── .github/workflows/
│   └── deploy.yml           # Automated CI/CD deployment to VM via IAP SSH
├── scripts/
│   ├── load_secrets.sh      # Load environment variables dynamically from GCP Secret Manager
│   ├── migrate_pubsub_messages.py # Pub/Sub backlog migration helper
│   ├── setup_vm_user_service.sh   # VM user-space systemd service setup script
│   ├── hannibal-webhook-agent.service # User-space systemd unit file template
│   ├── publish_test_message.py    # Test webhook payload publisher
│   └── ruff-all.sh          # Clinical linting & formatting validation script
├── src/
│   ├── token_optimized_agent/ # Zero-Cost ADK Token Optimization Module
│   │   ├── agent.py         # Task Mode sub-agents & stateless helper agent definitions
│   │   ├── app.py           # App with EventsCompactionConfig & ContextCacheConfig
│   │   ├── callbacks.py     # Tool response payload truncation & MessagePruningPlugin
│   │   └── tools.py         # Off-context artifact storage & lookup tools
│   └── webhook_agent/       # Core Webhook Orchestrator Package
│       ├── worker.py        # Pub/Sub subscriber entry point and main polling loop
│       ├── processor.py     # Event routing, deduplication, 👀 reaction, & AgentCore delegation
│       ├── agent_core.py    # ADK agent wrapper & execution entry point
│       ├── webhook_agent.py # ADK-powered WebhookAgent with tool execution & writeback policy
│       ├── bot_identity.py  # Multi-signal bot identity detection for loop avoidance
│       ├── memory_service.py # ADK agent memory and session persistence service
│       ├── gemma_planner.py # Gemma 4 model interaction interface
│       ├── github_credential_helper.py # GitHub App JWT generation & cached installation tokens
│       ├── enqueue.py       # Pub/Sub payload publishing helpers
│       ├── types.py         # Common dataclasses and ActionResult definitions
│       ├── templates/       # Local prompt & code review templates
│       └── tests/           # Pytest test suite & fixtures
├── tests/
│   └── unit/                # Unit tests for token optimization & callbacks
├── main.py                  # Distributed process manager entry point
├── pyproject.toml           # Dependency & pytest specification (uv-compatible)
├── README.md                # Repository documentation
└── cloud_run_function.md    # Serverless webhook router implementation guide
```

---

## ⚡ High-Efficiency Token Optimization & Programmatic Features

The project includes built-in strategies to maximize context efficiency and eliminate unnecessary LLM calls:

1. **Programmatic 👀 Reaction**: Immediately adds an `eyes` reaction to user comments upon receiving webhooks in `processor.py` (0 token cost).
2. **Context Compaction (`EventsCompactionConfig`)**: Automatically summarizes long multi-turn session event histories using Gemini AI Studio models.
3. **Context Caching (`ContextCacheConfig`)**: Caches static system prompts, rules, and tool schemas across requests.
4. **Sub-Agent Isolation (`include_contents="none"` & `mode="task"`)**: Isolates complex multi-turn sub-tasks using Task Mode with Pydantic output schemas, while keeping utility helpers stateless.
5. **Tool Payload Truncation (`after_tool_callback`)**: Truncates tool output arrays (max 5 items), long strings (max 1,000 chars), and total serialized JSON (max 4,000 chars) to prevent prompt overflow.
6. **Off-Context Data Storage (`InMemoryArtifactService`)**: Stores large datasets and documents off-context as artifacts rather than dumping raw text into model prompts.

---

## 🚀 Getting Started

### 1. Installation
This project uses `uv` for lightning-fast dependency management:

```bash
# Sync dependencies and set up the virtual environment
uv sync
```

### 2. Running Tests
Run the full test suite (including token optimization and worker tests):

```bash
uv run pytest
```

---

## 🛠️ Operations & Deployment

### Running the Background Worker
Start the worker process locally:

```bash
uv run python main.py
```

### VM Deployment & User-Space Systemd Service
On the target VM (`hannibal-hub-free`), the agent runs as a user-space systemd service (`hannibal-webhook-agent.service`):

```bash
# Initialize and start the user-space systemd service
bash scripts/setup_vm_user_service.sh

# Monitor service status on VM
systemctl --user status hannibal-webhook-agent.service

# Restart service on VM
systemctl --user restart hannibal-webhook-agent.service

# Follow live service logs
journalctl --user -u hannibal-webhook-agent.service -f
```

All pushes to `main` automatically trigger [`.github/workflows/deploy.yml`](file:///.github/workflows/deploy.yml) to deploy code updates and restart `hannibal-webhook-agent.service` via IAP SSH!