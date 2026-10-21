package controller

import (
	"context"
	"testing"

	appsv1alpha1 "carewright.io/carewright-operator/api/v1alpha1"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func TestWorkflowOriginURL(t *testing.T) {
	tests := []struct {
		name, address, want string
	}{
		{"published route", "https://workflow.example.test/wcpgingesterworkflow", "https://workflow.example.test"},
		{"published route with port and query", "https://workflow.example.test:8443/wcpgingesterworkflow?foo=bar#state", "https://workflow.example.test:8443"},
		{"in-cluster service", "http://workflow.default.svc.cluster.local:80", "http://workflow.default.svc.cluster.local:80"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := workflowOriginURL(test.address); got != test.want {
				t.Errorf("workflowOriginURL(%q) = %q, want %q", test.address, got, test.want)
			}
		})
	}
}

func TestIngesterWriterWatchMapping(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := appsv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	matching := &appsv1alpha1.CPGIngester{
		ObjectMeta: metav1.ObjectMeta{Name: "matching", Namespace: "pipelines"},
		Spec:       appsv1alpha1.CPGIngesterSpec{CarePlanWriterRef: &appsv1alpha1.PipelineReference{Name: "writer"}},
	}
	other := &appsv1alpha1.CPGIngester{
		ObjectMeta: metav1.ObjectMeta{Name: "other", Namespace: "pipelines"},
		Spec:       appsv1alpha1.CPGIngesterSpec{CarePlanWriterRef: &appsv1alpha1.PipelineReference{Name: "another-writer"}},
	}
	reconciler := &CPGIngesterReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithObjects(matching, other).Build()}
	writer := &appsv1alpha1.CarePlanWriter{ObjectMeta: metav1.ObjectMeta{Name: "writer", Namespace: "pipelines"}}
	requests := reconciler.ingestersForWriter(context.Background(), writer)
	if len(requests) != 1 || requests[0].Namespace != "pipelines" || requests[0].Name != "matching" {
		t.Fatalf("unexpected CPGIngester requests for writer: %+v", requests)
	}
}

func TestPipelineComponentContracts(t *testing.T) {
	find := func(components []sandboxComponent, name string) sandboxComponent {
		t.Helper()
		for _, component := range components {
			if component.Name == name {
				return component
			}
		}
		t.Fatalf("component %q not found", name)
		return sandboxComponent{}
	}
	checkAccess := func(component sandboxComponent, name, host string) {
		t.Helper()
		for _, access := range component.NetworkAccess {
			if access.Name == name && access.Host == host && access.Port == 443 {
				return
			}
		}
		t.Errorf("%s lacks %s egress to %s: %+v", component.Name, name, host, component.NetworkAccess)
	}

	ingester := &appsv1alpha1.CPGIngester{ObjectMeta: metav1.ObjectMeta{Name: "ingester", Namespace: "pipelines"}}
	ingesterComponents := cpgIngesterComponents(ingester)
	ingesterBFF := cpgIngesterBFFComponent(ingester, "https://workflow.example.test/wingesterworkflow", "https://writer-bff.example.test")
	if ingesterBFF.Env["SONATAFLOW_URL"] != "https://workflow.example.test" || ingesterBFF.Env["SONATAFLOW_WORKFLOW_NAME"] != "wingesterworkflow" || ingesterBFF.Env["ACP_WRITER_BFF_URL"] != "https://writer-bff.example.test" {
		t.Errorf("incorrect CPG BFF workflow and writer inputs: %+v", ingesterBFF.Env)
	}
	if len(ingesterComponents) != 4 {
		t.Errorf("got %d CPG sandbox components, want 4 backend components", len(ingesterComponents))
	}

	writer := &appsv1alpha1.CarePlanWriter{ObjectMeta: metav1.ObjectMeta{Name: "writer", Namespace: "pipelines"}}
	writerComponents := carePlanWriterComponents(writer, map[string]string{
		"bff": "https://writer-bff.example.test", "llm-reasoning": "https://reasoning.example.test",
		"decision-engine": "https://decision.example.test", "decision-service": "https://dmn.example.test",
		"fhir-server": "https://fhir.example.test",
	})
	writerBFF := carePlanWriterBFFComponent(writer, map[string]string{
		"llm-reasoning": "https://reasoning.example.test", "decision-engine": "https://decision.example.test", "fhir-server": "https://fhir.example.test",
	}, "https://workflow.example.test/wwriterworkflow")
	if writerBFF.Env["SONATAFLOW_URL"] != "https://workflow.example.test" || writerBFF.Env["SONATAFLOW_WORKFLOW_NAME"] != "wwriterworkflow" {
		t.Errorf("incorrect writer BFF workflow inputs: %+v", writerBFF.Env)
	}
	if len(writerComponents) != 7 {
		t.Errorf("got %d writer sandbox components, want 7 backend components", len(writerComponents))
	}
	if writerBFF.Env["LLM_REASONING_URL"] != "https://reasoning.example.test" || writerBFF.Env["DECISION_ENGINE_URL"] != "https://decision.example.test" || writerBFF.Env["FHIR_SERVER_URL"] != "https://fhir.example.test" {
		t.Errorf("incorrect writer BFF peer inputs: %+v", writerBFF.Env)
	}
	checkAccess(find(writerComponents, "llm-reasoning"), "decision-engine", "decision.example.test")
	checkAccess(find(writerComponents, "decision-engine"), "decision-service", "dmn.example.test")
	checkAccess(find(writerComponents, "mcp"), "decision-service", "dmn.example.test")
	checkAccess(find(writerComponents, "mcp"), "fhir-server", "fhir.example.test")
}

func TestArtifactStoreNetworkAccessUsesProviderPolicy(t *testing.T) {
	const endpoint = "http://minio.carewright.svc.cluster.local:9000"
	tests := []struct {
		name        string
		store       *appsv1alpha1.ArtifactStoreSpec
		wantNetwork bool
	}{
		{
			name:        "no provider keeps direct egress",
			store:       &appsv1alpha1.ArtifactStoreSpec{URL: endpoint},
			wantNetwork: true,
		},
		{
			name: "managed provider supplies signed egress",
			store: &appsv1alpha1.ArtifactStoreSpec{URL: endpoint, Credentials: &appsv1alpha1.ProviderSourceSpec{
				Type: "carewright-minio", SecretRef: corev1.LocalObjectReference{Name: "artifact-secret"},
			}},
		},
		{
			name:  "external provider supplies egress",
			store: &appsv1alpha1.ArtifactStoreSpec{URL: endpoint, CredentialsProvider: "artifact-provider"},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			ingester := &appsv1alpha1.CPGIngester{
				ObjectMeta: metav1.ObjectMeta{Name: "ingester", Namespace: "carewright"},
				Spec:       appsv1alpha1.CPGIngesterSpec{ArtifactStore: test.store},
			}
			writer := &appsv1alpha1.CarePlanWriter{
				ObjectMeta: metav1.ObjectMeta{Name: "writer", Namespace: "carewright"},
				Spec:       appsv1alpha1.CarePlanWriterSpec{ArtifactStore: test.store},
			}
			for _, pipeline := range []struct {
				name       string
				components []sandboxComponent
				consumers  map[string]bool
			}{
				{"ingester", cpgIngesterComponents(ingester), map[string]bool{"ingestion": true, "llm-analysis": true, "assembly": true, "delivery": true}},
				{"writer", carePlanWriterComponents(writer, nil), map[string]bool{"patient-data": true, "llm-reasoning": true, "decision-engine": true, "fhir-generation": true, "fhir-server": true}},
			} {
				for _, component := range pipeline.components {
					count := 0
					for _, access := range component.NetworkAccess {
						if access.Name == "artifact-store" {
							count++
							if access.Host != "minio.carewright.svc.cluster.local" || access.Port != 9000 {
								t.Errorf("%s/%s has unexpected artifact endpoint: %+v", pipeline.name, component.Name, access)
							}
						}
					}
					want := 0
					if test.wantNetwork && pipeline.consumers[component.Name] {
						want = 1
					}
					if count != want {
						t.Errorf("%s/%s has %d artifact-store rules, want %d", pipeline.name, component.Name, count, want)
					}
				}
			}
		})
	}
}
