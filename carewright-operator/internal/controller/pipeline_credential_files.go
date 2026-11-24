package controller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"reflect"
	"slices"
	"strings"

	appsv1alpha1 "carewright.io/carewright-operator/api/v1alpha1"
	openshellv1 "github.com/ai-openshell/sdk-go/openshell/v1"
	openshelltypes "github.com/ai-openshell/sdk-go/openshell/v1/types"
	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const localCredentialProfile = "carewright-local-credentials-file"

// This loader never logs values, validates the exact expected key set, and
// replaces itself with the application. Its arguments contain references only.
// File delivery does NOT keep these values secret from the sandbox workload.
const localCredentialBootstrap = `import json,os,re,sys
try:
    expected=json.loads(sys.argv[2])
    with open(sys.argv[1]) as handle:
        values=json.load(handle)
    if not isinstance(values,dict) or set(values)!=set(expected):
        raise ValueError()
    for name,value in values.items():
        if not (name.startswith("SERVICE_TOKEN_") or name=="APPROVAL_SIGNING_KEY"):
            raise ValueError()
        if not isinstance(value,str) or not re.fullmatch("[a-fA-F0-9]{64}",value):
            raise ValueError()
    os.environ.update(values)
except Exception:
    sys.stderr.write("Local credential file unavailable or invalid; refusing startup\n")
    sys.exit(78)
os.execvpe(sys.argv[3],sys.argv[3:],os.environ)
`

func localComponentCredentialKeys(component string) []string {
	switch component {
	case "patient-data":
		return []string{"SERVICE_TOKEN_ACP_PATIENT_DATA"}
	case "decision-engine":
		return []string{"SERVICE_TOKEN_ACP_DECISION_ENGINE"}
	case "llm-reasoning":
		return []string{"SERVICE_TOKEN_ACP_LLM_REASONING", "SERVICE_TOKEN_ACP_DECISION_ENGINE"}
	case "fhir-generation":
		return []string{"SERVICE_TOKEN_ACP_FHIR_GENERATION"}
	case "fhir-server":
		return []string{"SERVICE_TOKEN_ACP_FHIR_SERVER", approvalSigningKey}
	case "ingestion":
		return []string{"SERVICE_TOKEN_CPG_INGESTION"}
	case "llm-analysis":
		return []string{"SERVICE_TOKEN_CPG_LLM_ANALYSIS"}
	case "assembly":
		return []string{"SERVICE_TOKEN_CPG_ASSEMBLY"}
	case "delivery":
		return []string{"SERVICE_TOKEN_CPG_DELIVERY"}
	}
	return nil
}

func componentLocalCredentialRegistration(owner client.Object, component string) *appsv1alpha1.SandboxProviderRegistration {
	options := pipelineSecurityOptions(owner)
	keys := localComponentCredentialKeys(component)
	if options == nil || !options.LocalCredentialFiles || len(keys) == 0 {
		return nil
	}
	tokens, signing := pipelineSecuritySources(owner)
	if tokens == nil {
		return nil
	} // preparePipelineSecurity rejects missing references.
	_, registration := managedProvider(owner, "local-"+component, "", &appsv1alpha1.ProviderSourceSpec{Type: localCredentialProfile, SecretRef: tokens.SecretRef})
	registration.LocalCredentials = map[string]corev1.SecretKeySelector{}
	for _, key := range keys {
		source := tokens.SecretRef
		if key == approvalSigningKey && signing != nil {
			source = signing.SecretRef
		}
		registration.LocalCredentials[key] = corev1.SecretKeySelector{LocalObjectReference: source, Key: key}
	}
	return registration
}

func wrapLocalCredentialCommand(command []string, registration *appsv1alpha1.SandboxProviderRegistration) []string {
	if registration == nil {
		return command
	}
	keys := localComponentCredentialKeysFromRegistration(registration)
	encoded, _ := json.Marshal(keys)
	path := "/run/openshell/providers/" + registration.Name + "/credentials.json"
	return append([]string{"python3", "-c", localCredentialBootstrap, path, string(encoded)}, command...)
}

func localComponentCredentialKeysFromRegistration(registration *appsv1alpha1.SandboxProviderRegistration) []string {
	// json.Marshal sorts map keys, but the loader's expected list must also be deterministic.
	keys := make([]string, 0, len(registration.LocalCredentials))
	for key := range registration.LocalCredentials {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	return keys
}

// No secret content is serialized into a Kubernetes CR, annotation, or status.
// Only the in-memory provider payload contains the resolved credential values.
func resolveLocalCredentialConfig(ctx context.Context, c client.Client, namespace string, registration appsv1alpha1.SandboxProviderRegistration) (map[string]string, string, error) {
	if registration.Type != localCredentialProfile || len(registration.Config) != 0 {
		return nil, "", fmt.Errorf("local credentials require the dedicated profile and empty config")
	}
	anchored := false
	for _, selector := range registration.LocalCredentials {
		if selector.Name == registration.SecretRef.Name {
			anchored = true
		}
	}
	if !anchored {
		return nil, "", fmt.Errorf("local credential provider ownership Secret is not selected")
	}
	values := map[string]string{}
	markers := map[string]string{}
	for env, selector := range registration.LocalCredentials {
		if !strings.HasPrefix(env, "SERVICE_TOKEN_") && env != approvalSigningKey {
			return nil, "", fmt.Errorf("unsupported local credential name")
		}
		if selector.Name == "" || selector.Key != env || (selector.Optional != nil && *selector.Optional) {
			return nil, "", fmt.Errorf("local credential selectors must name required matching keys")
		}
		var secret corev1.Secret
		if err := c.Get(ctx, client.ObjectKey{Namespace: namespace, Name: selector.Name}, &secret); err != nil {
			return nil, "", fmt.Errorf("local credential Secret unavailable")
		}
		value := string(secret.Data[selector.Key])
		if len(value) != 64 {
			return nil, "", fmt.Errorf("local credential must be 32-byte hex")
		}
		if _, err := hex.DecodeString(value); err != nil {
			return nil, "", fmt.Errorf("local credential must be hex")
		}
		values[env] = value
		markers[selector.Name] = string(secret.UID) + "/" + secret.ResourceVersion
	}
	payload, _ := json.Marshal(values)
	revision, _ := json.Marshal(markers)
	sum := sha256.Sum256(revision)
	return map[string]string{"payload": string(payload)}, hex.EncodeToString(sum[:]), nil
}

func localCredentialProfileSpec() openshellv1.ProviderProfile {
	return openshellv1.ProviderProfile{
		ID: localCredentialProfile, DisplayName: "Carewright local credential file exception",
		Description: "Experimental NON-SECRET channel for synthetic-data local credentials only",
		Category:    openshellv1.ProfileCategoryOther,
		Files:       []openshelltypes.ProfileFile{{Path: "credentials.json", EnvVar: "CAREWRIGHT_LOCAL_CREDENTIALS_FILE", Content: "{{config.payload}}"}},
	}
}

func ensureLocalCredentialProfile(ctx context.Context, c openshellv1.ClientInterface, workspace string) error {
	desired := localCredentialProfileSpec()
	existing, err := c.Providers().Profiles().Get(ctx, workspace, localCredentialProfile)
	if err != nil && !openshellv1.IsNotFound(err) {
		return fmt.Errorf("read local credential file profile")
	}
	if existing == nil {
		result, err := c.Providers().Profiles().Import(ctx, workspace, []openshellv1.ProfileImportItem{{Profile: desired}})
		if err != nil || result == nil || !result.Imported || len(result.Diagnostics) != 0 {
			return fmt.Errorf("import local credential file profile")
		}
		existing, err = c.Providers().Profiles().Get(ctx, workspace, localCredentialProfile)
		if err != nil {
			return fmt.Errorf("verify imported local credential file profile")
		}
	}
	if existing == nil || !reflect.DeepEqual(existing.Files, desired.Files) || len(existing.Credentials) != 0 || len(existing.Endpoints) != 0 || len(existing.Binaries) != 0 || existing.InferenceCapable || len(existing.Discovery.Credentials) != 0 {
		return fmt.Errorf("existing local credential file profile differs; refusing to overwrite")
	}
	return nil
}

func localCredentialRevision(request *appsv1alpha1.SandboxRequest) string {
	markers := map[string]string{}
	for _, registration := range request.Spec.ProviderRegistrations {
		if len(registration.LocalCredentials) != 0 {
			markers[registration.Name] = request.Status.ProviderSyncMarkers[registration.Name]
		}
	}
	if len(markers) == 0 {
		return ""
	}
	data, _ := json.Marshal(markers)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func registrationUsesSecret(registration appsv1alpha1.SandboxProviderRegistration, name string) bool {
	if registration.SecretRef.Name == name {
		return true
	}
	for _, selector := range registration.LocalCredentials {
		if selector.Name == name {
			return true
		}
	}
	return false
}

func sanitizeLocalCredentialError(request *appsv1alpha1.SandboxRequest, err error) error {
	if err == nil {
		return nil
	}
	for _, registration := range request.Spec.ProviderRegistrations {
		if len(registration.LocalCredentials) != 0 {
			return fmt.Errorf("local credential provider synchronization failed; sensitive details suppressed")
		}
	}
	return err
}
