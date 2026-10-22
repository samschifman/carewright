# CPG-to-ACP Documentation

User-facing documentation for the CPG-to-Actionable Care Plans system.

| Document | Description |
|---|---|
| [Agent Security with OpenShell](openshell-agent-security.md) | How OpenShell sandboxes enforce per-agent security policies on OpenShift |
| [Tool Governance with MCP Gateway](mcp-gateway-tool-governance.md) | How MCP Gateway governs AI agent tool access with aggregation, prefixing, and virtual servers |
| [API Gateway](api-gateway.md) | How the API gateway unifies pod-split services behind a single URL with path-based routing |
| [Workflow Orchestration with SonataFlow](sonataflow-orchestration.md) | How SonataFlow orchestrates the pipelines, async callback patterns, and MinIO-based state transfer |
| [Adversarial Review Pattern](adversarial-review-pattern.md) | How agents check agents — the adversarial review architecture across both pipelines |
| [Clinical Data Question Answering](clinical-data-qa.md) | How acp-writer extracts patient data from FHIR IPS bundles — layered resolution, temporal primitives, benchmarking |
| [AI Transparency on FHIR](ai-transparency.md) | How acp-writer labels and traces AI-produced care plans (AI-Device/Provenance/InputPrompt/ModelCard, AIAST, AIconfidence) and records plan-level conflicts as Provenance resources |
| [Automation templates and publication](automation.md) | How CPG automation templates are reviewed, selected, and published; current service limits and configuration |

For internal development documentation (design docs, spikes, project plan), see [`dev_docs/`](../dev_docs/).
