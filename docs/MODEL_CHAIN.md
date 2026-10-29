# Model Chain Architecture & Dual-Tier Rate Limit Registry

This document details the model fallback chain and comparative rate limit quotas (Free Tier vs Paid Tier) enforced by `RPMWaiter` and `WebhookAgent` in `hannibal-hub-agents`.

---

## 🎯 Active Tier Resolution Cascade

The active tier is resolved dynamically via `_resolve_tier()` in `src/webhook_agent/models/rate_limiter.py`:

1. **Explicit Environment Variable**: `WEBHOOK_TIER` (`"free"` or `"paid"`).
2. **GCE VM Instance Metadata**: `instance/attributes/WEBHOOK_TIER` (read with 30s cache).
3. **Dynamic Firestore Configuration**: `system_config/runtime -> WEBHOOK_TIER` (read with 30s cache).
4. **Strict Baseline Default**: `"free"`.

API keys are resolved strictly based on active tier (`WEBHOOK_FREE_KEY` vs `WEBHOOK_PAID_KEY`) with automatic fallback to GCP Secret Manager.

---

## 🔄 Dynamic Model Fallback Chains

When rate limit errors (`429 RESOURCE_EXHAUSTED`), quota depletion, single-request TPM ceiling breaches, or transient server errors (`503 UNAVAILABLE`) occur, `WebhookAgent._advance_model_chain()` dynamically transitions `self._code_auditor.model` and `self._conversational_agent.model` to the next available tier model. `InMemorySessionService` maintains full conversation state and history across model transitions.

### 🟢 Free Tier Chain (TPM & RPD Optimized)

Default primary model: `gemini-3.5-flash-lite` (can be overridden via `PRIMARY_MODEL` / `GEMMA_MODEL` env vars).

All Gemini models below provide **250,000 TPM**, allowing full multi-file PR reviews and AST dossiers without token truncation. Gemma models provide massive daily request budgets (14,400 RPD) but are strictly token-gated to payloads ≤ 10,000 tokens due to their 16k TPM ceiling.

| Step | Model | Free Tier RPM | Free Tier TPM | Free Tier RPD | Role |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **0** | `gemini-3.1-flash-lite` | 15 | 250,000 | 500 | Default primary model |
| **1** | `gemini-3.5-flash-lite` | 15 | 250,000 | 500 | Secondary fallback |
| **2** | `gemini-3.5-flash` | 5 | 250,000 | 20 | Tertiary fallback |
| **3** | `gemini-3.6-flash` | 5 | 250,000 | 20 | Quaternary fallback |
| **4** | `gemini-3.7-flash` | 5 | 250,000 | 20 | Quinary fallback |
| **8** | `gemma-4-31b-it` | 30 | ⚠️ 16,000 | 14,400 | Token-gated fallback (payloads ≤ 10k tokens) |
| **9** | `gemma-4-26b-a4b-it` | 30 | ⚠️ 16,000 | 14,400 | Token-gated backup (payloads ≤ 10k tokens) |

### 💳 Paid Tier Chain (3.x High-Bandwidth & 4M TPM Token Expansion)

Default primary model: `gemini-3.5-flash-lite` (can be overridden via `PRIMARY_MODEL` / `GEMMA_MODEL` env vars, or escalated to `gemini-3.8-flash` / `gemini-2.5-pro` via `/deep`, `/audit`, `/reason`, or `/model`).

Optimized for high-throughput with massive 4,000,000 TPM and **Unlimited RPD**, removing diff and payload truncation boundaries:
- **Unshackled Context Window**: Up to 1,000,000 input tokens on paid tier.
- **Uncapped Diff Ingestion**: 5MB diff byte budget (effectively uncapped) eliminating `[... remaining files truncated ...]` omissions.
- **Full File Ingestion**: Ingests complete modified file contents alongside diffs for context-grounded verification.
- **High-Throughput Parallel Audits**: Runs concurrent static analysis passes (symbol impact, test coverage, security scans) concurrently.

| Step | Model | Paid Tier RPM | Paid Tier TPM | Paid Tier RPD | Role |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **0** | `gemini-3.1-flash-lite` | 4,000 | 4,000,000 | **Unlimited** | Default primary workhorse |
| **1** | `gemini-3.5-flash-lite` | 4,000 | 4,000,000 | **Unlimited** | Secondary fallback |
| **2** | `gemini-3.5-flash` | 1,000 | 2,000,000 | 10,000 | Tertiary fallback |
| **3** | `gemini-3.6-flash` | 1,000 | 2,000,000 | 10,000 | Quaternary fallback |
| **4** | `gemini-3.7-flash` | 1,000 | 2,000,000 | 10,000 | Quinary fallback |

---

## ⚡ Fast Failover & Depletion Registry

- **TPM Ceiling Fast-Fail**: Single-request payloads exceeding a model's TPM ceiling (e.g., >16k on Gemma) trigger an immediate `0.5s` fast-fail in `RPMWaiter` rather than burning 60s sleep delays or throwing live API 429 errors.
- **503 Instant Failover**: High-demand server spikes trigger an immediate `0.5s` failover to the next tier model rather than burning lengthy exponential backoffs.
- **Durable Depletion Tracking**: `_DEPLETED_MODEL_REGISTRY` registers depleted models into Firestore (with in-memory fallback) to ensure single runs do not recycle failed models during cooldowns.
- **Zero-Quota Fast-Fail**: Models with `0` RPM/RPD on the active tier trigger an immediate fast-fail (`ValueError`) in `RPMWaiter` to prevent delayed fallback retries.
