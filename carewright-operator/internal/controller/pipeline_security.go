package controller

import (
	"strings"

	appsv1alpha1 "carewright.io/carewright-operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func pipelineSecuritySources(owner client.Object) (*appsv1alpha1.ProviderSourceSpec, *appsv1alpha1.ProviderSourceSpec) {
	switch pipeline := owner.(type) {
	case *appsv1alpha1.CarePlanWriter:
		return pipeline.Spec.ServiceTokens, pipeline.Spec.ApprovalSigning
	case *appsv1alpha1.CPGIngester:
		return pipeline.Spec.ServiceTokens, nil
	}
	return nil, nil
}

func pipelineTokenKeys(owner client.Object) []string {
	switch owner.(type) {
	case *appsv1alpha1.CarePlanWriter:
		return []string{"SERVICE_TOKEN_ACP_BFF", "SERVICE_TOKEN_ACP_DECISION_ENGINE", "SERVICE_TOKEN_ACP_LLM_REASONING", "SERVICE_TOKEN_ACP_PATIENT_DATA", "SERVICE_TOKEN_ACP_FHIR_GENERATION", "SERVICE_TOKEN_ACP_FHIR_SERVER"}
	case *appsv1alpha1.CPGIngester:
		return []string{"SERVICE_TOKEN_CPG_BFF", "SERVICE_TOKEN_CPG_INGESTION", "SERVICE_TOKEN_CPG_LLM_ANALYSIS", "SERVICE_TOKEN_CPG_ASSEMBLY", "SERVICE_TOKEN_CPG_DELIVERY", "SERVICE_TOKEN_ACP_BFF"}
	}
	return nil
}

func secretEnvironment(source *appsv1alpha1.ProviderSourceSpec, keys ...string) []corev1.EnvVar {
	if source == nil {
		return nil
	}
	return secretKeyEnvironment(source.SecretRef, keys...)
}

func secretKeyEnvironment(secret corev1.LocalObjectReference, keys ...string) []corev1.EnvVar {
	entries := []corev1.EnvVar{}
	for _, key := range keys {
		entries = append(entries, corev1.EnvVar{Name: key, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: secret, Key: key}}})
	}
	return entries
}

func browserSecurityEnvironment(owner client.Object) []corev1.EnvVar {
	tokens, signing := pipelineSecuritySources(owner)
	entries := []corev1.EnvVar{}
	values := map[string]string{}
	switch pipeline := owner.(type) {
	case *appsv1alpha1.CarePlanWriter:
		entries = append(entries, secretEnvironment(signing, "APPROVAL_SIGNING_KEY")...)
		if pipeline.Spec.SessionSigning != nil {
			entries = append(entries, secretKeyEnvironment(pipeline.Spec.SessionSigning.SecretRef, "SESSION_SIGNING_KEY")...)
		}
		entries = append(entries, secretEnvironment(tokens, "SERVICE_TOKEN_ACP_BFF", "SERVICE_TOKEN_ACP_DECISION_ENGINE", "SERVICE_TOKEN_ACP_LLM_REASONING", "SERVICE_TOKEN_ACP_FHIR_SERVER")...)
		origin := pipeline.Status.Endpoints["ui"]
		if smart := pipeline.Spec.Smart; smart != nil {
			if smart.Credentials != nil {
				entries = append(entries, secretKeyEnvironment(smart.Credentials.SecretRef, "SMART_CLIENT_ID", "SMART_CLIENT_SECRET")...)
			}
			if smart.PublicOrigin != "" {
				origin = smart.PublicOrigin
			}
			values["SMART_ISS_ALLOWLIST"] = strings.Join(smart.IssAllowlist, ",")
			if smart.RedirectURI != "" {
				values["SMART_REDIRECT_URI"] = smart.RedirectURI
			}
			if smart.Scopes != "" {
				values["SMART_SCOPES"] = smart.Scopes
			}
			values["SMART_FHIR_INTERNAL_BASE"] = smart.InternalFHIRBase
		}
		values["ACP_PUBLIC_ORIGIN"] = origin
		if values["SMART_REDIRECT_URI"] == "" && origin != "" {
			values["SMART_REDIRECT_URI"] = strings.TrimRight(origin, "/") + "/api/v1/launch/callback"
		}
	case *appsv1alpha1.CPGIngester:
		if pipeline.Spec.Signing != nil {
			entries = append(entries, secretKeyEnvironment(pipeline.Spec.Signing.SecretRef, "CPG_SIGNING_KEY")...)
		}
		entries = append(entries, secretEnvironment(tokens, "SERVICE_TOKEN_CPG_BFF")...)
		origin := pipeline.Spec.PublicOrigin
		if origin == "" {
			origin = pipeline.Status.Endpoints["ui"]
		}
		values["CPG_PUBLIC_ORIGIN"] = origin
	}
	return append(entries, environmentVariables(values)...)
}
