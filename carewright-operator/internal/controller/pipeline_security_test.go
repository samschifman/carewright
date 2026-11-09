package controller

import (
	"testing"

	appsv1alpha1 "carewright.io/carewright-operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

func TestIngesterBrowserCredentialsAreSecretReferences(t *testing.T) {
	ingester := &appsv1alpha1.CPGIngester{Spec: appsv1alpha1.CPGIngesterSpec{
		PublicOrigin:  "https://ingester.test",
		ServiceTokens: &appsv1alpha1.ProviderSourceSpec{Type: "carewright-service-tokens", SecretRef: corev1.LocalObjectReference{Name: "tokens"}},
		Signing:       &appsv1alpha1.SecretSourceSpec{SecretRef: corev1.LocalObjectReference{Name: "signing"}},
	}}
	env := browserSecurityEnvironment(ingester)
	seen := map[string]bool{}
	for _, entry := range env {
		seen[entry.Name] = true
		switch entry.Name {
		case "CPG_PUBLIC_ORIGIN":
			if entry.Value != "https://ingester.test" {
				t.Fatal("incorrect browser origin")
			}
		case "SERVICE_TOKEN_CPG_BFF", "CPG_SIGNING_KEY":
			if entry.Value != "" || entry.ValueFrom == nil || entry.ValueFrom.SecretKeyRef == nil || entry.ValueFrom.SecretKeyRef.Key != entry.Name {
				t.Fatal("credential not supplied through individual Secret reference")
			}
		default:
			t.Fatalf("unexpected BFF credential: %s", entry.Name)
		}
	}
	for _, key := range []string{"CPG_PUBLIC_ORIGIN", "SERVICE_TOKEN_CPG_BFF", "CPG_SIGNING_KEY"} {
		if !seen[key] {
			t.Fatalf("missing %s", key)
		}
	}
	if seen["APPROVAL_SIGNING_KEY"] || seen["SESSION_SIGNING_KEY"] {
		t.Fatal("writer signing keys delivered to ingester")
	}
	for _, key := range pipelineTokenKeys(ingester) {
		if key == "CPG_SIGNING_KEY" {
			t.Fatal("signing key must not enter workflow environment")
		}
	}
}
