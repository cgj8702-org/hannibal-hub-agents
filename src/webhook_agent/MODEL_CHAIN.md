# ⛓️ Model Chain Architecture

The Hannibal Webhook Agent utilizes a **Tier-Aware Model Chain** fallback sequence. This guarantees maximum token bandwidth and daily request headroom while ensuring 100% resilience against rate limit (`429 RESOURCE_EXHAUSTED`) and server (`503`) errors.

## 📊 Model Chain Tier Hierarchy

### 🟢 Free Tier Chain (High-Volume & Zero 429 Bottlenecks)

| Tier | Model | TPM (Tokens/Min) | RPD (Requests/Day) | Primary Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 0** | `gemini-3.5-flash-lite` | ⚡ **250,000** | 🚀 **500** | Primary high-volume model (zero 20 RPD cap) |
| **Tier 1** | `gemini-3.1-flash-lite` | ⚡ **250,000** | 🚀 **500** | Secondary Flash-Lite fallback |
| **Tier 2** | `gemma-4-31b-it` | 💎 **16,000** | **14,400** | Dedicated high daily request budget model |
| **Tier 3** | `gemma-4-26b-a4b-it` | 💎 **16,000** | **14,400** | Ultra-high daily request budget backup |

### 💳 Paid Tier Chain (Maximum Reasoning & Bandwidth)

| Tier | Model | TPM (Tokens/Min) | RPD (Requests/Day) | Primary Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 0** | `gemini-3.6-flash` | 🎯 **2,000,000 (2M)** | **10,000** | High-speed balanced reasoning & tool use |
| **Tier 1** | `gemini-3.5-flash-lite` | ⚡ **4,000,000 (4M)** | 🚀 **150,000** | Maximum token throughput & daily request ceiling |
| **Tier 2** | `gemini-3.1-flash-lite` | ⚡ **4,000,000 (4M)** | 🚀 **150,000** | High-throughput fallback |

## 🔄 Dynamic Model Cascading

When a 429 rate limit or transient error occurs during execution:
1. The agent catches the transient exception.
2. `_advance_model_chain()` mutates `self._agent.model = Gemini(model=next_model)` dynamically.
3. ADK's `InMemorySessionService` maintains full conversation state and history across model transitions.
4. The turn retries immediately on the next tier model without dropping event execution.

