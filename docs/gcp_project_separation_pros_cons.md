# Hannibal Hub Agents — GCP Project Separation Pros & Cons Analysis

This document provides a rigorous architectural pros and cons analysis regarding the full separation of the two GCP projects in the Hannibal Hub ecosystem (focusing on asynchronous Pub/Sub workers, VM deployment infrastructure, and Cloud Run functions).

---

## Executive Summary

Currently, Hannibal Hub operates across linked GCP project resources for webhook event ingestion, Pub/Sub messaging, Secret Manager secrets, and serverless compute. Fully decoupling these projects into completely isolated GCP environments involves distinct architectural trade-offs across security, cost attribution, operational overhead, and network/IAM complexity.

---

## Detailed Pros & Cons Analysis

### 🟢 Pros of Full GCP Project Separation

1. **Strict Blast Radius & Fault Isolation**
   - *Detail*: Isolating asynchronous Pub/Sub/VM workers from Cloud Run webhook API endpoints ensures that heavy agent execution workloads or worker resource saturation (CPU/memory exhaustion) do not degrade API responsiveness or uptime.
2. **Granular IAM & Least-Privilege Boundaries**
   - *Detail*: Service accounts (`webhook-agent-sa`), Secret Manager access policies, and Pub/Sub publisher/subscriber roles can be scoped strictly to their respective project boundaries without cross-project permission leakage.
3. **Independent Cost Attribution & Quotas**
   - *Detail*: Simplifies financial auditing, resource budgeting, and API quota allocation (e.g., GenAI API RPD/TPM quotas, Cloud Logging storage limits) per distinct functional domain.
4. **Environment Lifecycle Independence**
   - *Detail*: Staging, production, and experimental agent infrastructure can be deployed in separate projects with zero risk of naming collisions or accidental state pollution.

---

### 🔴 Cons of Full GCP Project Separation

1. **Increased Operational & CI/CD Complexity**
   - *Detail*: Requires managing multiple GCP project configurations, separate Terraform/gcloud deployment pipelines, and dual service account credential rotation mechanisms.
2. **Cross-Project Pub/Sub & IAM Overhead**
   - *Detail*: When publishers and subscribers reside in separate projects, Pub/Sub topics require explicit cross-project IAM grants (`roles/pubsub.publisher` and `roles/pubsub.subscriber`), increasing setup fragility.
3. **Secret Manager Fragmentation**
   - *Detail*: Secrets (such as GitHub App private keys, webhook secrets, and Gemini API keys) must either be replicated across projects or accessed via cross-project Secret Accessor bindings, adding latency and IAM configuration surface area.
4. **Monitoring & Logging Fragmentation**
   - *Detail*: Centralized log aggregation across multiple GCP projects requires Log Sinks routed to a common BigQuery or Cloud Storage sink, complicating observability dashboards.

---

## Recommendations & Implementation Roadmap

- **Recommendation**: Maintain project separation if security auditing or strict IAM multi-tenant isolation is a mandatory compliance requirement.
- **Phased Approach**: If decoupling is pursued, start by migrating Pub/Sub dead-letter topics and agent runner VMs into the target dedicated project while keeping shared secrets synchronized via Secret Manager replication.
