# Model Chain Architecture & Dual-Tier Rate Limit Registry

This document details the model fallback chain and comparative rate limit quotas (Free Tier vs Paid Tier) enforced by `RPMWaiter` and `WebhookAgent` in `hannibal-hub-agents`.

---

## 🎯 Active Tier Resolution Cascade

The active tier is resolved dynamically via `_resolve_tier()` in `src/webhook_agent/models/rate_limiter.py`:

1. **Explicit Environment Variable**: `WEBHOOK_TIER` (`"free"` or `"paid"`).
2. **GCE VM Instance Metadata**: `instance/attributes/WEBHOOK_TIER` (read with 60s cache).
3. **Dynamic Firestore Configuration**: `system_config/runtime -> WEBHOOK_TIER` (read with 30s cache).
4. **Strict Baseline Default**: `"free"`.

API keys are resolved strictly based on active tier (`WEBHOOK_FREE_KEY` vs `WEBHOOK_PAID_KEY`) with automatic fallback to GCP Secret Manager.

---

## 🔄 Dynamic Model Fallback Chains

When rate limit errors (`429 RESOURCE_EXHAUSTED`), quota depletion, single-request TPM ceiling breaches, or transient server errors (`503 UNAVAILABLE`) occur, `WebhookAgent._advance_model_chain()` dynamically transitions `self._code_auditor.model` and `self._conversational_agent.model` to the next available tier model. `InMemorySessionService` maintains full conversation state and history across model transitions.

### 🟢 Free Tier Chain (TPM & RPD Optimized)

Default primary model: `gemini-3.5-flash-lite` (can be overridden via `PRIMARY_MODEL` / `GEMMA_MODEL` env vars).

All Gemini models below provide **250,000 TPM**, allowing full multi-file PR reviews and AST dossiers without token truncation. Gemma models provide massive daily request budgets (14,400 RPD) but are strictly token-gated to payloads ≤ 14,000 tokens due to their 16k TPM ceiling.

| Step | Model | Free Tier RPM | Free Tier TPM | Free Tier RPD | Role |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **0** | `gemini-3.5-flash-lite` | 15 | 250,000 | 500 | Default high-volume primary model |
| **1** | `gemini-3.1-flash-lite` | 15 | 250,000 | 500 | Secondary Flash-Lite fallback |
| **2** | `gemini-3.8-flash` | 5 | 250,000 | 20 | High-reasoning audit fallback |
| **3** | `gemini-3.7-flash` | 5 | 250,000 | 20 | Secondary balanced reasoning fallback |
| **4** | `gemini-3.6-flash` | 5 | 250,000 | 20 | Tertiary reasoning fallback |
| **5** | `gemini-3.5-flash` | 5 | 250,000 | 20 | Quota backup fallback |
| **6** | `gemini-2.5-flash-lite` | 10 | 250,000 | 20 | High-efficiency lightweight fallback |
| **7** | `gemini-2.5-flash` | 5 | 250,000 | 20 | Fast general-purpose fallback |
| **8** | `gemma-4-31b-it` | 30 | ⚠️ 16,000 | 14,400 | Token-gated fallback (payloads ≤ 14k tokens) |
| **9** | `gemma-4-26b-a4b-it` | 30 | ⚠️ 16,000 | 14,400 | Token-gated backup (payloads ≤ 14k tokens) |

### 💳 Paid Tier Chain (Cost-Optimized High-Bandwidth)

Default primary model: `gemini-3.5-flash-lite` (can be overridden via `PRIMARY_MODEL` / `GEMMA_MODEL` env vars, or escalated to `gemini-3.8-flash` via `/deep`, `/audit`, or `/model gemini-3.8-flash`).

Optimized for 80% lower token cost ($0.30/1M input vs $1.50/1M for 3.8 Flash) with massive 4,000 RPM / 4M TPM headroom, preserving `gemini-3.8-flash` for complex reasoning audits and deep reviews.

| Step | Model | Paid Tier RPM | Paid Tier TPM | Paid Tier RPD | Role |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **0** | `gemini-3.5-flash-lite` | 4,000 | 4,000,000 | 150,000 | Default cost-optimized primary ($0.30/1M in) |
| **1** | `gemini-3.1-flash-lite` | 4,000 | 4,000,000 | 150,000 | Secondary high-throughput Flash-Lite fallback |
| **2** | `gemini-3.8-flash` | 1,000 | 2,000,000 | 10,000 | High-reasoning audit & deep review escalation ($1.50/1M in) |
| **3** | `gemini-3.7-flash` | 1,000 | 2,000,000 | 10,000 | Secondary balanced reasoning fallback |
| **4** | `gemini-3.6-flash` | 1,000 | 2,000,000 | 10,000 | Tertiary reasoning fallback |
| **5** | `gemini-3.5-flash` | 1,000 | 2,000,000 | 10,000 | Quota backup fallback |
| **6** | `gemini-2.5-flash` | 1,000 | 1,000,000 | 10,000 | Standard flash fallback |
| **7** | `gemini-2.5-flash-lite` | 4,000 | 4,000,000 | 1,000,000 | Maximum daily request ceiling |

---

## ⚡ Fast Failover & Depletion Registry

- **TPM Ceiling Fast-Fail**: Single-request payloads exceeding a model's TPM ceiling (e.g., >16k on Gemma) trigger an immediate `0.5s` fast-fail in `RPMWaiter` rather than burning 60s sleep delays or throwing live API 429 errors.
- **503 Instant Failover**: High-demand server spikes trigger an immediate `0.5s` failover to the next tier model rather than burning lengthy exponential backoffs.
- **Durable Depletion Tracking**: `_DEPLETED_MODEL_REGISTRY` registers depleted models into Firestore (with in-memory fallback) to ensure single runs do not recycle failed models during cooldowns.
- **Zero-Quota Fast-Fail**: Models with `0` RPM/RPD on the active tier trigger an immediate fast-fail (`ValueError`) in `RPMWaiter` to prevent delayed fallback retries.
