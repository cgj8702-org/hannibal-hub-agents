# ⛓️ Model Chain Architecture

The Hannibal Webhook Agent utilizes a **TPM Descending Model Chain** fallback sequence. This ensures maximum token bandwidth (up to 4M TPM) and daily request headroom (up to 150k RPD) while guaranteeing 100% resilience against rate limit (`429 RESOURCE_EXHAUSTED`) and server (`503`) errors.

## 📊 Model Chain Tier Hierarchy

| Tier | Model | TPM (Tokens/Min) | RPD (Requests/Day) | Primary Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 0** | `GEMMA_MODEL` env var | Configurable | Configurable | Configurable primary selection (default: `gemini-3.6-flash`) |
| **Tier 1** | `gemini-3.5-flash-lite` | ⚡ **4,000,000 (4M)** | 🚀 **150,000** | Maximum token throughput & daily request ceiling |
| **Tier 2** | `gemini-3.6-flash` | 🎯 **2,000,000 (2M)** | **10,000** | High-speed balanced reasoning & tool use |
| **Tier 3** | `gemini-2.5-flash` | 🛡️ **1,000,000 (1M)** | **10,000** | Reliable Flash safety net |
| **Tier 4** | `gemma-4-26b` | 💎 **16,000 (16K)** | **14,400** | Dedicated daily request budget model |

## 🔄 Dynamic Model Cascading

When a 429 rate limit or transient error occurs during execution:
1. The agent catches the transient exception.
2. `_advance_model_chain()` mutates `self._agent.model = Gemini(model=next_model)` dynamically.
3. ADK's `InMemorySessionService` maintains full conversation state and history across model transitions.
4. The turn retries immediately on the next tier model without dropping event execution.
