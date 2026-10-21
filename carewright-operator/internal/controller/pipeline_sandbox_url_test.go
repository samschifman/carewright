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

const testServiceProxyURL = "http://openshell-service-proxy.test.svc.cluster.local:8080"

func TestSandboxServiceURLThroughInternalProxy(t *testing.T) {
	proxy := testServiceProxyURL
	services := []appsv1alpha1.SandboxServiceStatus{{
		Name: "http",
		URL:  "http://default--ingestion-fb0af8b3--http.openshell.localhost:8080/",
	}}
	want := testServiceProxyURL + "/default--ingestion-fb0af8b3--http"
	if got := sandboxServiceURL(services, "http", proxy); got != want {
		t.Fatalf("sandboxServiceURL() = %q, want %q", got, want)
	}
}

func TestSandboxServiceProxyURLFromBootstrapService(t *testing.T) {
	t.Setenv("OPENSHELL_SERVICE_PROXY_URL", "")
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	k8sClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(&corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "openshell-service-proxy",
			Namespace: "test",
			Labels:    map[string]string{"app.kubernetes.io/managed-by": "carewright-bootstrap"},
		},
	}).Build()
	want := "http://openshell-service-proxy.test.svc.cluster.local:8080"
	got, err := sandboxServiceProxyURL(context.Background(), k8sClient, "test")
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("sandboxServiceProxyURL() = %q, want %q", got, want)
	}
}

func TestSandboxServiceURLPreservesOtherGatewayDomains(t *testing.T) {
	proxy := testServiceProxyURL
	services := []appsv1alpha1.SandboxServiceStatus{{Name: "http", URL: "https://ingestion.example.test/"}}
	if got := sandboxServiceURL(services, "http", proxy); got != "https://ingestion.example.test" {
		t.Fatalf("sandboxServiceURL() = %q, want original URL", got)
	}
}

func TestSandboxServiceURLWithoutInternalProxy(t *testing.T) {
	services := []appsv1alpha1.SandboxServiceStatus{{Name: "http", URL: "http://default--ingestion--http.openshell.localhost:8080/"}}
	if got := sandboxServiceURL(services, "http", ""); got != "http://default--ingestion--http.openshell.localhost:8080" {
		t.Fatalf("sandboxServiceURL() = %q, want original URL", got)
	}
}
