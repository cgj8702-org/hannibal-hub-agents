# 🤖 Hannibal Hub Agents: Standalone GitHub App Webhook Orchestrator

A unified, event-driven service that handles GitHub webhooks, verifies signatures, queues event processing asynchronously in-memory, and runs an agentic loop powered by **Gemma 4** to safely interact with GitHub repositories.

---

## 🏗️ System Architecture

The webhook orchestrator is designed for high reliability, security, and zero-trust event execution, now running as a unified process:

```mermaid
flowchart TD
    GH[GitHub Webhook Event] -->|HTTPS POST| Ingress[FastAPI Webhook Ingress]
    Ingress -->|1. Verify HMAC Signature| Auth[Signature Validator]
    Ingress -->|2. Quick ACK 202 Accepted| GH
    Ingress -->|3. Enqueue Event| Queue[(Internal Async Queue)]
    
    Queue -->|4. Trigger Pull| Worker[Background Worker Task]
    Worker -->|5. App Authentication| Creds[GitHub App JWT / Installation Token]
    Worker -->|6. Load Context| GH_API[GitHub REST API]
    Worker -->|7. Decide Actions| Gemma[Gemma 4 Planner / Gemini API]
    Worker -->|8. Policy Verification| Policy{Mutations Allowed?}
    Policy -->|Yes| Exec[Execute Tool Actions]
    Policy -->|No / Dry Run| Log[Log Planned Actions]
    
    Exec -->|9. Writeback| GH_Write[GitHub Comments, Reviews, PRs]
```

---

## 📁 Repository Structure

```
├── .agents/                 # Shared agent scripts & protocols
├── src/
│   └── webhook_agent/       # Core package
│       ├── app.py           # FastAPI Webhook Ingress & Background Worker
│       ├── processor.py     # Event routing & agent orchestration logic
│       ├── agent_core.py    # Tool schema validation & action execution
│       └── gemma_planner.py # Gemma 4 model interaction via Gemini SDK
├── main.py                  # Unified entry point to launch the server
├── github_app_credential_helper.py  # Utility for App JWT & cached access tokens
├── pyproject.toml           # Dependency specification (uv-compatible)
├── webhook_agent_TODO.md    # Local roadmap and first-PR tasks
└── github_app_webhook_project_plan.md # Global design document
```

---

## 🚀 Getting Started

### 1. Installation
This project uses `uv` for lightning-fast dependency management:

```bash
# Sync dependencies and set up the virtual environment
uv sync
```

### 2. Configuration
Ensure the following environment variables are set in your environment or `.envrc` file:

#### Server Config:
- `WEBHOOK_SECRET`: The shared secret configured on the GitHub App to verify HMAC signatures.
- `CF_TUNNEL_TOKEN`: (Optional) Token to automatically start a Cloudflare Tunnel for local development.

#### Agent Config:
- `GITHUB_APP_ID`: Numeric ID of your GitHub App.
- `GITHUB_INSTALLATION_ID`: Target installation ID for token exchange.
- `GITHUB_PRIVATE_KEY_PATH`: Path to the private key PEM file for your GitHub App.
- `GEMINI_API_KEY`: API key for model planning calls.
- `GEMMA_MODEL`: The Gemma model to use (defaults to `gemma-4-31b-it`).
- `ALLOW_AUTOMATED_MUTATIONS`: Set to `1` or `true` to allow active writebacks to GitHub.
- `DRY_RUN`: Set to `1` to preview actions without executing mutations.

---

## 🛠️ Operations & Execution

### Running the Unified Server
Start the unified server which launches the FastAPI ingress, the background event processor, and the Cloudflare tunnel (if configured) in a single process:

```bash
uv run python main.py
```

### Testing Credentials & App Tokens
Use the credential helper CLI directly to fetch or verify installation access tokens:
```bash
uv run python github_app_credential_helper.py \
    --app-id <YOUR_APP_ID> \
    --installation-id <YOUR_INSTALLATION_ID> \
    --private-key <PATH_TO_PEM>
```

---

## 🔒 Security & Policy Gates

1. **HMAC Signature Checks**: All incoming webhooks must match the configured `WEBHOOK_SECRET` signature. Unsigned or mismatching payloads are rejected immediately with `401 Unauthorized`.
2. **Short-lived Tokens**: The helper automatically handles cache expiration and rotation of installation access tokens (valid for maximum 1 hour).
3. **Purity Gates**: The processor checks `ALLOW_AUTOMATED_MUTATIONS`. If not explicitly enabled, all actions fallback to log-only operations, preventing unexpected automated commits, issues, or comments.