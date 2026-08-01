# 🤖 Hannibal Hub Agents: Distributed GitHub App Webhook Orchestrator

A unified, event-driven service that handles GitHub webhooks via a serverless router, queues event processing asynchronously using **Google Cloud Pub/Sub**, and runs an agentic loop powered by **Gemma 4** to safely interact with GitHub repositories.

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
├── .github/workflows/
│   └── deploy.yml           # Automated CI/CD deployment to VM via IAP SSH
├── scripts/
│   ├── load_secrets.sh      # Load environment variables from GCP Secret Manager
│   ├── migrate_pubsub_messages.py # Pub/Sub backlog migration helper
│   ├── setup_vm_user_service.sh   # VM systemd user service setup
│   ├── hannibal-webhook-agent.service # Systemd unit template
│   ├── publish_test_message.py    # Test event publisher
│   └── ruff-all.sh          # Clinical linting & formatting script
├── src/
│   └── webhook_agent/       # Core package
│       ├── worker.py        # Pub/Sub subscriber and entry point
│       ├── processor.py     # Event routing & agent orchestration logic
│       ├── agent_core.py    # Tool schema validation & action execution
│       ├── gemma_planner.py # Gemma 4 model interaction via Gemini SDK
│       ├── enqueue.py       # Pub/Sub publishing helpers
│       ├── github_credential_helper.py # App JWT & cached access tokens
│       ├── templates/       # Local prompt/review templates
│       └── tests/           # Pytest test suite & fixtures
├── main.py                  # Entry point to launch the background worker
├── pyproject.toml           # Dependency specification (uv-compatible)
├── README.md                # Documentation
└── cloud_run_function.md    # Implementation guide for the serverless router
```

---

## 🚀 Getting Started

### 1. Installation
This project uses `uv` for lightning-fast dependency management:

```bash
# Sync dependencies and set up the virtual environment
uv sync
```

### 2. Configuration & Secret Management
Ensure the required environment variables are configured. You can load secrets directly from **GCP Secret Manager** without keeping plain-text secrets or key files on disk:

```bash
# Load secrets directly from GCP Secret Manager into memory
source scripts/load_secrets.sh
```

#### Infrastructure Config:
- `PUBSUB_PROJECT`: Dedicated Google Cloud Project ID (e.g. `cgj8702-webhook-agent`).
- `PUBSUB_TOPIC`: Full path to the topic (`projects/.../topics/webhooks`).
- `PUBSUB_SUBSCRIPTION`: Full path to the subscription (`projects/.../subscriptions/webhooks-sub`).
- `PUBSUB_DEAD_LETTER_TOPIC`: Optional dead-letter topic path for failed payloads.

#### Agent Config:
- `GITHUB_APP_ID`: Numeric ID of your GitHub App.
- `GITHUB_INSTALLATION_ID`: Target installation ID for token exchange.
- `GITHUB_PRIVATE_KEY_PATH`: Path to the private key PEM file for your GitHub App.
- `GEMINI_API_KEY`: API key for model planning calls.
- `GEMMA_MODEL`: The Gemma model to use (defaults to `gemma-4-31b-it`).
- `GEMMA_MODEL_FALLBACK`: Fallback model when primary is unavailable (defaults to `gemma-4-26b-a4b-it`).
- `GEMMA_MODEL_MAX_RETRIES`: Maximum retry attempts on transient errors (defaults to `5`).
- `ALLOW_AUTOMATED_MUTATIONS`: Set to `1` or `true` to allow active writebacks to GitHub.

---

## 🛠️ Operations & Deployment

### Running the Background Worker
Start the worker process locally or on a VM:

```bash
uv run python main.py
```

### Migrating Backlogged Pub/Sub Messages
To migrate backlogged messages between Pub/Sub projects or subscriptions:

```bash
uv run python scripts/migrate_pubsub_messages.py \
    --source-subscription projects/OLD_PROJECT/subscriptions/webhook-sub \
    --target-topic projects/NEW_PROJECT/topics/webhooks
```

### VM Deployment (Compute Engine)
Run the user-space service setup script on your VM (`hannibal-hub-free`):

```bash
bash scripts/setup_vm_user_service.sh
```

All pushes to `main` will automatically trigger [`.github/workflows/deploy.yml`](file:///.github/workflows/deploy.yml) to deploy code updates and restart the service!

---

## 🔒 Security & Policy Gates

1. **Edge Signature Checks**: All incoming webhooks are verified at the router level (Cloud Run Function) using the `WEBHOOK_SECRET` HMAC signature. Mismatching payloads are rejected before ever reaching the queue.
2. **Short-lived Tokens**: The credential helper handles automatic rotation and caching of installation access tokens (valid for max 1 hour).
3. **Purity Gates**: The `AgentCore` checks `ALLOW_AUTOMATED_MUTATIONS`. If not enabled, all actions fallback to log-only operations, preventing unexpected automated commits or comments.
4. **Agentic Guardrails**: To prevent LLM hallucinations and unauthorized mutations:
   - **Least Privilege Tooling**: The planner provides only the minimum necessary tool schemas for the specific event type being processed.
   - **Context-Aware Prompting**: Prompts are enriched with precise metadata and injected PR diffs/templates to ensure the agent targets the correct resources accurately.