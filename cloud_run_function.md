Using a **Cloud Run function** (Google’s updated branding for Cloud Functions 2nd gen) to act as your lightweight webhook receiver and router to Google Cloud Pub/Sub is a highly recommended serverless design pattern.

This approach is particularly effective because of the following benefits:

1. **Instant Response (Bypasses GitHub’s 10s Timeout)**: Cloud Run functions can process signature validation, publish to Pub/Sub, and return an HTTP `202 Accepted` to GitHub in under 100 milliseconds.
2. **Scale-to-Zero Cost**: If your team isn’t pushing code or opening PRs overnight, your router scales down to zero, costing nothing. When a burst of activity happens (e.g., automated batch updates), it scales up instantly.
3. **Decoupled Architecture**: By offloading the event payload to Pub/Sub immediately, your downstream agent worker (which runs the `gemma-4-31b-it` model) can take as much time as it needs (even minutes) to analyze, call tools, and respond without risking GitHub delivery failures.

---

### Recommended Pipeline

```text
[GitHub Webhook]
       │
       ▼ (HTTPS POST)
[Cloud Run Function (Router)]  ◄─── 1. Verifies X-Hub-Signature-256
       │                            2. Normalizes payload to project schema
       │                            3. Publishes normalized event to Pub/Sub
       ▼ (Publish Event)            4. Returns 202 immediately to GitHub
 [Pub/Sub Topic]
       │
       ▼ (Pull or Push Subscription)
[Downstream Worker (Agent)]    ◄─── Uses existing WebhookProcessor & AgentCore
```

---

### Implementation

#### Configure the Cloud Run Function (Router)

This function handles signature verification and **payload normalization** at the edge. By normalizing the data before it hits Pub/Sub, the downstream worker can remain unchanged.

**Deployment Note:** When deploying this function, set the **Entry point** to `github_webhook_router`.

**`main.py`**:

```python
import os
import hmac
import hashlib
import json
from google.cloud import pubsub_v1
import functions_framework

# Initialize the Pub/Sub client globally to reuse it across requests
project_id = os.environ.get("PUBSUB_PROJECT")
topic_id = os.environ.get("PUBSUB_TOPIC")
publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(project_id, topic_id)

# The secret token you configured in your GitHub App settings
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")


def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    if not signature_header:
        return False
    try:
        sha_name, signature = signature_header.split("=")
    except ValueError:
        return False
    if sha_name != "sha256":
        return False

    mac = hmac.new(WEBHOOK_SECRET.encode(), msg=payload_body, digestmod=hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), signature)


def normalize_payload(event_name: str, raw_payload: dict, delivery_id: str) -> dict:
    """Normalize incoming webhook payload into the project's consistent event envelope."""
    return {
        "delivery_id": delivery_id,
        "event_name": event_name,
        "action": raw_payload.get("action"),
        "sender": raw_payload.get("sender"),
        "installation": raw_payload.get("installation"),
        "repository": raw_payload.get("repository"),
        "raw_payload": raw_payload,
    }


@functions_framework.http
def github_webhook_router(request):
    # Reject anything that isn't a POST request from GitHub
    if request.method != "POST":
        return "Method Not Allowed", 405

    signature_header = request.headers.get("X-Hub-Signature-256")
    event_type = request.headers.get("X-GitHub-Event", "unknown")
    delivery_id = request.headers.get("X-GitHub-Delivery", "unknown")
    payload_body = request.get_data()

    # 1. Verify that the webhook actually came from GitHub
    if not verify_signature(payload_body, signature_header):
        return "Unauthorized: Signature mismatch", 401

    try:
        # 2. Parse and Normalize
        raw_payload = json.loads(payload_body.decode("utf-8", errors="replace"))
        normalized = normalize_payload(event_type, raw_payload, delivery_id)

        # Convert normalized dict to bytes for Pub/Sub
        data_bytes = json.dumps(normalized).encode("utf-8")

        # 3. Publish to Pub/Sub with full metadata for filtering/logging
        future = publisher.publish(
            topic_path,
            data=data_bytes,
            delivery_id=delivery_id,
            event_name=str(event_type),
            action=str(normalized["action"] or ""),
        )
        future.result()
    except Exception as e:
        print(f"Error processing or publishing to Pub/Sub: {e}")
        return "Internal Server Error", 500

    # Respond to GitHub immediately
    return "Event normalized and queued successfully", 202
```

**`requirements.txt`**:

```text
functions-framework>=3.0.0
google-cloud-pubsub>=2.15.0
```

---

#### Service Account Permissions

The Cloud Run function needs a service account with the **Pub/Sub Publisher (`roles/pubsub.publisher`)** IAM role attached to it so it is authorized to publish payloads to your chosen topic.

---

### Integration with Existing Worker

Because the Cloud Run function now handles normalization, you do **not** need to rewrite your worker logic. Your existing `src/webhook_agent/worker.py` and `src/webhook_agent/processor.py` will work as-is because they already expect the normalized JSON envelope.

#### How the Worker Consumes the Event:
1. The worker pulls a message from the Pub/Sub subscription.
2. It decodes the `data` field, which is now the **normalized JSON object**.
3. It passes this object directly to `processor.process_event(payload)`.
4. The `WebhookProcessor` uses the `delivery_id` (inside the payload) to ensure idempotency and the `event_name` to route the event to the correct agent logic.

### Why this design works so well:

* **Zero-Change Downstream**: You get the benefits of a serverless edge router without having to modify your core agent logic or loop-protection systems.
* **No Lost Webhooks**: If your LLM worker crashes or is offline, Pub/Sub holds the normalized events for up to 7 days.
* **Bypasses Timeouts**: GitHub requires a response within 10 seconds. This router responds in milliseconds, while your agent can take minutes to reason with `gemma-4-31b-it`.
* **Simple Local Debugging**: You can let the production router queue events in GCP, then run your existing worker locally to pull and process those events for debugging.

---

### Key Operational Considerations

* **At-Least-Once Delivery**: Pub/Sub may deliver a message more than once. Your `WebhookProcessor` already handles this by tracking `delivery_id` in its `_processed_deliveries` set.
* **Filtering**: You can use Pub/Sub subscription filters on the attributes (e.g., `attributes.event_name = "pull_request"`) to ensure your worker only wakes up for events it actually cares about.