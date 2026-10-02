# Model Chain Architecture & Dual-Tier Rate Limit Registry

This document details the model fallback chain and comparative rate limit quotas (Free Tier vs Paid Tier) enforced by `RPMWaiter` and `WebhookAgent` in `hannibal-hub-agents`.

---

## 🎯 Active Tier Resolution Cascade

The active tier is resolved dynamically via `_resolve_tier()` in `src/webhook_agent/logic/rate_limiter.py`:

1. **Explicit Environment Variable**: `WEBHOOK_TIER` (`"free"` or `"paid"`).
2. **GCE VM Instance Metadata**: `instance/attributes/WEBHOOK_TIER` (read with 60s cache).
3. **Dynamic Firestore Configuration**: `system_config/runtime -> WEBHOOK_TIER` (read with 30s cache).
4. **Strict Baseline Default**: `"free"`.

API keys are resolved strictly based on active tier (`WEBHOOK_FREE_KEY` vs `WEBHOOK_PAID_KEY`) with automatic fallback to GCP Secret Manager.

---

## 🔄 Dynamic Model Fallback Chains

When rate limit errors (`429 RESOURCE_EXHAUSTED`), quota depletion, or transient server errors (`503 UNAVAILABLE`) occur, `WebhookAgent._advance_model_chain()` dynamically transitions `self._code_auditor.model` to the next available tier model. `InMemorySessionService` maintains full conversation state and history across model transitions.

### 🟢 Free Tier Chain (High-Volume & Zero 429 Bottlenecks)

Default primary model: `gemini-3.5-flash-lite` (can be overridden via `GEMMA_MODEL` env var).

| Step | Model | Free Tier RPM | Free Tier TPM | Free Tier RPD | Role |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **0** | `gemini-3.5-flash-lite` | 15 | 250,000 | 500 | Default high-volume primary model |
| **1** | `gemini-3.1-flash-lite` | 15 | 250,000 | 500 | Secondary Flash-Lite fallback |
| **2** | `gemini-2.5-flash` | 5 | 250,000 | 20 | Fast general-purpose fallback |
| **3** | `gemini-2.5-flash-lite` | 10 | 250,000 | 20 | High-efficiency lightweight fallback |
| **4** | `gemma-4-31b-it` | 30 | 16,000 | 14,400 | High daily request budget open model |
| **5** | `gemma-4-26b-a4b-it` | 30 | 16,000 | 14,400 | Ultra-high daily request budget backup |

### 💳 Paid Tier Chain (Maximum Reasoning & Bandwidth)

Default primary model: `gemini-3.8-flash` (can be overridden via `GEMMA_MODEL` env var).

| Step | Model | Paid Tier RPM | Paid Tier TPM | Paid Tier RPD | Role |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **0** | `gemini-3.8-flash` | 1,000 | 2,000,000 | 10,000 | High-speed intelligent reasoning & AST diff analysis |
| **1** | `gemini-3.7-flash` | 1,000 | 2,000,000 | 10,000 | Secondary audit fallback |
| **2** | `gemini-3.6-flash` | 1,000 | 2,000,000 | 10,000 | Tertiary balanced reasoning fallback |
| **3** | `gemini-3.5-flash-lite` | 4,000 | 4,000,000 | 150,000 | High-throughput quota fallback |
| **4** | `gemini-3.1-flash-lite` | 4,000 | 4,000,000 | 150,000 | Secondary high-throughput fallback |
| **5** | `gemini-2.5-flash` | 1,000 | 1,000,000 | 10,000 | Standard flash fallback |
| **6** | `gemini-2.5-flash-lite` | 4,000 | 4,000,000 | 1,000,000 | Maximum daily request ceiling |

---

## ⚡ Fast Failover & Depletion Registry

- **503 Instant Failover**: High-demand server spikes trigger an immediate `0.5s` failover to the next tier model rather than burning lengthy exponential backoffs.
- **Durable Depletion Tracking**: `_DEPLETED_MODEL_REGISTRY` registers depleted models into Firestore (with in-memory fallback) to ensure single runs do not recycle failed models during cooldowns.
- **Zero-Quota Fast-Fail**: Models with `0` RPM/RPD on the active tier trigger an immediate fast-fail (`ValueError`) in `RPMWaiter` to prevent delayed fallback retries.
