# CareWright operator

The operator deploys the CPG Ingester and Care Plan Writer pipelines on OpenShift. It creates OpenShell sandboxes for backend components, Kubernetes Deployments and Services for the BFFs and UIs, UI Routes, and SonataFlow workflows. The [detailed end-to-end guide](docs/openshift-e2e.md) covers recovery and troubleshooting; this README is a concise walkthrough using the validated development configuration.

Run the commands below from the **CareWright monorepo root**. They target `jrao-carewright`; to use another namespace, copy the [operator overlay](config/overlays/jrao-carewright) and change its namespace, `WATCH_NAMESPACE`, and namespace-qualified service URLs before applying it. Keep this operator-managed path separate from the component `deploy/` scripts in the same namespace. You need `oc` logged in with cluster-admin access, plus `helm`, `jq`, `curl`, `openssl`, a default StorageClass, and OpenShift OVN-Kubernetes. This bootstrap uses development MinIO credentials and an unauthenticated, cluster-internal OpenShell gateway; do not use it for production or real patient data.

## 1. Install the prerequisites

Keep your Enmaas API key in a local file **outside the repository**. Pass its absolute path, not the key itself:

```sh
OPENSHELL_DEV_INSECURE=1 \
LLM_ORIGIN=https://api.enmaas.devshift.net \
LLM_MODEL=gpt-6-sol \
LLM_API_KEY_FILE=/absolute/path/to/enmaas-api-key \
carewright-operator/scripts/bootstrap-prerequisites.sh --namespace jrao-carewright
```

The script creates or reuses the namespace, Agent Sandbox and SonataFlow operators, SonataFlowPlatform, MinIO and its `cpg-artifacts` and `cpg-phi` buckets, the OpenShell gateway and service proxy, credential Secrets, and the OpenShell provider profiles. It waits for the prerequisites and prints their URLs and Secret names. It does **not** install the CareWright operator or create pipeline CRs. See the [bootstrap reference](docs/bootstrap-prerequisites.md) for overrides and rerun behavior.

`LLM_ORIGIN` must be a bare origin, without `/v1`. Its host and port must match the OpenShell `carewright-openai` profile and `spec.llm.url` in **both** pipeline CRs. `LLM_MODEL` is a bootstrap hint; set the model in both CRs too. The key is stored in the namespace Secret `carewright-llm-credentials`, not in a CR or component environment variable.

## 2. Install the CRDs and operator

```sh
oc apply -k carewright-operator/config/crd
oc apply -k carewright-operator/config/overlays/jrao-carewright
oc -n jrao-carewright rollout status deployment/carewright-operator-controller-manager --timeout=5m
```

Wait for `deployment/carewright-operator-controller-manager` to become available before creating pipeline CRs. The overlay scopes its controllers to `jrao-carewright` with `WATCH_NAMESPACE`. At this point, the CRDs and operator should be present; pipeline sandboxes and workflows will appear only after the next steps.

## 3. Create the CPG Ingester

Apply the [complete CPGIngester manifest](config/overlays/jrao-carewright/cpgingester.yaml):

```sh
oc apply -f carewright-operator/config/overlays/jrao-carewright/cpgingester.yaml
```

Its key settings look like this (the checked-in manifest includes additional component options):

```yaml
apiVersion: apps.carewright.io/v1alpha1
kind: CPGIngester
metadata:
  name: cpg-ingester
  namespace: jrao-carewright
spec:
  sandbox:
    gateway:
      endpoint: http://jrao-carewright-openshell.jrao-carewright.svc.cluster.local:8080
      insecure: true
    workspace: default
  artifactStore:
    url: http://minio.jrao-carewright.svc.cluster.local:9000
    artifactBucket: cpg-artifacts
    phiBucket: cpg-phi
    credentials:
      type: carewright-minio
      secretRef:
        name: carewright-artifact-credentials
  llm:
    url: https://api.enmaas.devshift.net
    model: gpt-6-sol
    requestTimeoutSeconds: 600
    credentials:
      type: carewright-openai
      secretRef:
        name: carewright-llm-credentials
  carePlanWriterRef:
    name: care-plan-writer
  ingestion:
    image: quay.io/carewright/cpg-ingester-ingestion:latest
  llmAnalysis:
    image: quay.io/carewright/cpg-ingester-llm:latest
  assembly:
    image: quay.io/carewright/cpg-ingester-assembly:latest
  delivery:
    image: quay.io/carewright/cpg-ingester-delivery:latest
  bff:
    image: quay.io/carewright/cpg-ingester-bff:latest
  ui:
    image: quay.io/carewright/cpg-ingester-ui:latest
```

The operator creates the ingester's SandboxRequests, SonataFlow workflow, BFF/UI Deployments and Services, and UI Route. `carePlanWriterRef` can resolve after the writer is created in step 4, but do not start an ingestion run until both pipelines are available.

With BPMN-capable component images, process items in a CPG also produce automation templates. The creator, validator, and reviewer run inside the existing `llm-analysis` sandbox; they do not require a new CR or sandbox. The operator's embedded workflow forwards the previous analysis/generation results and automation review data needed by those components. Engine preflight remains optional and disabled when `BPMN_PREFLIGHT_URL` is unset.

## 4. Create the Care Plan Writer

Apply the [complete CarePlanWriter manifest](config/overlays/jrao-carewright/careplanwriter.yaml):

```sh
oc apply -f carewright-operator/config/overlays/jrao-carewright/careplanwriter.yaml
```

It uses the same gateway, artifact store, and LLM credentials, and also configures the embedding endpoint in `spec.llmReasoning.embedding`. The operator creates its SandboxRequests, SonataFlow workflow, BFF/UI resources, and UI Route. Change endpoint, model, or bucket settings in **both** manifests if you customized the bootstrap; do not edit the generated children.

The writer BFF receives published BPMN templates through the existing ingester notification, registers them with its decision-engine service, and reloads published artifacts when the BFF starts. The operator's writer workflow passes a stable `careplan_id` through bundle generation and FHIR approval. The separate automation receiver is not deployed by this operator; approved plans can still be stored without publication to that service.

## 5. Check the generated resources

```sh
oc -n jrao-carewright get cpgingester,careplanwriter,sandboxrequest,sonataflow
oc -n jrao-carewright get route cpg-ingester-ui care-plan-writer-ui
oc -n jrao-carewright get deployment/minio,statefulset/jrao-carewright-openshell
```

The ingester and writer CR status report component readiness and generated endpoints. A Ready CR means its components are available, **not** that a pipeline run completed. Investigate any failed component before treating a deployment as ready; the [detailed guide](docs/openshift-e2e.md) notes a known writer MCP image failure.

## 6. Run a synthetic end-to-end test

Forward the ingester BFF and upload the repository's synthetic guideline from another terminal:

```sh
oc -n jrao-carewright port-forward service/cpg-ingester-bff 18081:80
```

```sh
curl -fsS -F 'pdf=@cpg-ingester/data/synthetic-hypertension-cpg.pdf;type=application/pdf' \
  http://127.0.0.1:18081/api/v1/upload
```

Open the `cpg-ingester-ui` Route from step 5 and submit the manifest/artifact review gates. The upload response contains a `runId`; poll `GET /api/v1/runs/<runId>` until it reports `completed`. That delivers the guideline to Care Plan Writer. Then use the [writer smoke-test commands](docs/openshift-e2e.md#3-run-the-synthetic-cpg-to-careplan-smoke-test) to submit the synthetic IPS bundle, complete its review gate in the `care-plan-writer-ui` Route, and verify a completed run with a `careplanId`.

For upgrades and failures—including provider endpoint mismatches and image tag behavior—use the [detailed guide](docs/openshift-e2e.md). Delete pipeline CRs before removing the operator or gateway so the SandboxRequest finalizers can clean up sandboxes and providers.

## Local development

Go 1.25+ is required. From `carewright-operator/`, run `make test` or `make run` against a cluster with prerequisites installed. Set `WATCH_NAMESPACE` when running a local controller so it does not reconcile unrelated namespaces. Use `go run ./cmd/main.go --controllers=sandboxrequest` to run only the SandboxRequest controller. The operator image is published by [the build workflow](../.github/workflows/publish-operator-image.yml) on changes to `carewright-operator/**` on `main`.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](../LICENSE).
