package controller

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	appsv1alpha1 "carewright.io/carewright-operator/api/v1alpha1"
	openshellv1 "github.com/ai-openshell/sdk-go/openshell/v1"
	openshellfake "github.com/ai-openshell/sdk-go/openshell/v1/fake"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	meta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

func securityTestScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, appsv1.AddToScheme, networkingv1.AddToScheme, appsv1alpha1.AddToScheme} {
		if err := add(s); err != nil {
			t.Fatal(err)
		}
	}
	return s
}

func securityTestWriter() *appsv1alpha1.CarePlanWriter {
	return &appsv1alpha1.CarePlanWriter{ObjectMeta: metav1.ObjectMeta{Name: "writer", Namespace: "test", UID: types.UID("writer-uid")}, Spec: appsv1alpha1.CarePlanWriterSpec{Security: &appsv1alpha1.PipelineSecuritySpec{ManagedSecrets: ptrTo(true), LocalCredentialFiles: ptrTo(true)}}}
}

func TestManagedSecurityDefaultsAreStableAndShared(t *testing.T) {
	ctx := context.Background()
	c := fake.NewClientBuilder().WithScheme(securityTestScheme(t)).Build()
	writer := securityTestWriter()
	if err := preparePipelineSecurity(ctx, c, writer); err != nil {
		t.Fatal(err)
	}
	before := map[string]map[string][]byte{}
	for _, name := range []string{serviceTokenSecret, "carewright-approval-signing-key", "carewright-session-signing-key"} {
		var secret corev1.Secret
		if err := c.Get(ctx, client.ObjectKey{Namespace: "test", Name: name}, &secret); err != nil {
			t.Fatal(err)
		}
		if len(secret.OwnerReferences) != 0 {
			t.Fatal("namespace-shared secret must not be garbage-collected with one pipeline")
		}
		for _, value := range secret.Data {
			if len(value) != 64 {
				t.Fatal("wrong entropy encoding")
			}
			if _, err := hex.DecodeString(string(value)); err != nil {
				t.Fatal(err)
			}
		}
		before[name] = secret.Data
	}
	if writer.Spec.ServiceTokens.SecretRef.Name != serviceTokenSecret {
		t.Fatal("effective reference not wired")
	}
	revision := writer.Annotations[securityRevisionAnnotation]
	ingester := &appsv1alpha1.CPGIngester{ObjectMeta: metav1.ObjectMeta{Name: "ingester", Namespace: "test"}, Spec: appsv1alpha1.CPGIngesterSpec{Security: &appsv1alpha1.PipelineSecuritySpec{ManagedSecrets: ptrTo(true), LocalCredentialFiles: ptrTo(true)}}}
	if err := preparePipelineSecurity(ctx, c, ingester); err != nil {
		t.Fatal(err)
	}
	if ingester.Spec.ServiceTokens.SecretRef.Name != writer.Spec.ServiceTokens.SecretRef.Name {
		t.Fatal("cross-pipeline callback tokens must agree")
	}
	if ingester.Spec.Signing.SecretRef.Name != "carewright-ingester-signing-key" {
		t.Fatal("ingester signing reference missing")
	}
	// Start from a fresh CR with omitted refs, just like the next reconcile.
	writer = securityTestWriter()
	if err := preparePipelineSecurity(ctx, c, writer); err != nil {
		t.Fatal(err)
	}
	if revision != writer.Annotations[securityRevisionAnnotation] {
		t.Fatal("idempotent reconcile changed revision")
	}
	for name, data := range before {
		var secret corev1.Secret
		if err := c.Get(ctx, client.ObjectKey{Namespace: "test", Name: name}, &secret); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(data, secret.Data) {
			t.Fatal("existing credential regenerated")
		}
	}
}

func TestManagedSecurityRejectsIncompleteExistingSecretsWithoutRepair(t *testing.T) {
	ctx := context.Background()
	secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: serviceTokenSecret, Namespace: "test"}, Data: map[string][]byte{"unrelated": []byte("preserve")}}
	c := fake.NewClientBuilder().WithScheme(securityTestScheme(t)).WithObjects(secret).Build()
	if err := preparePipelineSecurity(ctx, c, securityTestWriter()); err == nil {
		t.Fatal("incomplete existing Secret accepted")
	}
	var after corev1.Secret
	if err := c.Get(ctx, client.ObjectKeyFromObject(secret), &after); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(after.Data, secret.Data) || len(after.OwnerReferences) != 0 {
		t.Fatal("existing Secret adopted or overwritten")
	}
}

func TestExplicitMissingSecretIsNotGenerated(t *testing.T) {
	ctx := context.Background()
	c := fake.NewClientBuilder().WithScheme(securityTestScheme(t)).Build()
	writer := securityTestWriter()
	writer.Spec.ServiceTokens = &appsv1alpha1.ProviderSourceSpec{Type: "custom", SecretRef: corev1.LocalObjectReference{Name: "external"}}
	if err := preparePipelineSecurity(ctx, c, writer); err == nil {
		t.Fatal("missing external Secret accepted")
	}
	err := c.Get(ctx, client.ObjectKey{Namespace: "test", Name: "external"}, &corev1.Secret{})
	if !apierrors.IsNotFound(err) {
		t.Fatal("operator generated an explicitly external Secret")
	}
}

func TestSecurityDefaultsAndOptOuts(t *testing.T) {
	for _, test := range []struct {
		name           string
		spec           *appsv1alpha1.PipelineSecuritySpec
		managed, files bool
	}{
		{"omitted", nil, true, true},
		{"empty", &appsv1alpha1.PipelineSecuritySpec{}, true, true},
		{"generation-only-disabled", &appsv1alpha1.PipelineSecuritySpec{ManagedSecrets: ptrTo(false)}, false, true},
		{"files-only-disabled", &appsv1alpha1.PipelineSecuritySpec{LocalCredentialFiles: ptrTo(false)}, true, false},
		{"both-disabled", &appsv1alpha1.PipelineSecuritySpec{ManagedSecrets: ptrTo(false), LocalCredentialFiles: ptrTo(false)}, false, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			writer := securityTestWriter()
			writer.Spec.Security = test.spec
			ingester := &appsv1alpha1.CPGIngester{Spec: appsv1alpha1.CPGIngesterSpec{Security: test.spec}}
			for _, owner := range []client.Object{writer, ingester} {
				got := pipelineSecurityOptions(owner)
				if got.ManagedSecrets != test.managed || got.LocalCredentialFiles != test.files {
					t.Fatal("wrong effective security defaults")
				}
			}
			// Explicit false must survive JSON round trips (no bool/omitempty trap).
			data, err := json.Marshal(writer)
			if err != nil {
				t.Fatal(err)
			}
			var roundTrip appsv1alpha1.CarePlanWriter
			if err := json.Unmarshal(data, &roundTrip); err != nil {
				t.Fatal(err)
			}
			if *pipelineSecurityOptions(&roundTrip) != *pipelineSecurityOptions(writer) {
				t.Fatal("opt-out lost during serialization")
			}
		})
	}
}

func TestDefaultSecurityGeneratesKeysAndFiles(t *testing.T) {
	ctx := context.Background()
	c := fake.NewClientBuilder().WithScheme(securityTestScheme(t)).Build()
	writer := securityTestWriter()
	writer.Spec.Security = nil
	if err := preparePipelineSecurity(ctx, c, writer); err != nil {
		t.Fatal(err)
	}
	if writer.Spec.ServiceTokens == nil || componentLocalCredentialRegistration(writer, "patient-data") == nil {
		t.Fatal("default security is not functional")
	}
	ingester := &appsv1alpha1.CPGIngester{ObjectMeta: metav1.ObjectMeta{Name: "ingester", Namespace: "test"}}
	if err := preparePipelineSecurity(ctx, c, ingester); err != nil {
		t.Fatal(err)
	}
	if ingester.Spec.ServiceTokens == nil || ingester.Spec.Signing == nil || componentLocalCredentialRegistration(ingester, "ingestion") == nil {
		t.Fatal("ingester defaults not functional")
	}
	disabled := securityTestWriter()
	disabled.Spec.Security = &appsv1alpha1.PipelineSecuritySpec{ManagedSecrets: ptrTo(false), LocalCredentialFiles: ptrTo(false)}
	if err := preparePipelineSecurity(ctx, c, disabled); err != nil {
		t.Fatal(err)
	}
	if disabled.Spec.ServiceTokens != nil || componentLocalCredentialRegistration(disabled, "patient-data") != nil {
		t.Fatal("explicit opt-out ignored")
	}
	generationOnly := securityTestWriter()
	generationOnly.Spec.Security = &appsv1alpha1.PipelineSecuritySpec{LocalCredentialFiles: ptrTo(false)}
	if err := preparePipelineSecurity(ctx, c, generationOnly); err != nil {
		t.Fatal(err)
	}
	if generationOnly.Spec.ServiceTokens == nil || componentLocalCredentialRegistration(generationOnly, "patient-data") != nil {
		t.Fatal("file-only opt-out not respected")
	}
}

func TestBuiltInSecretReferencesCanBeGenerated(t *testing.T) {
	ctx := context.Background()
	c := fake.NewClientBuilder().WithScheme(securityTestScheme(t)).Build()
	writer := securityTestWriter()
	writer.Spec.Security = nil
	writer.Spec.ServiceTokens = &appsv1alpha1.ProviderSourceSpec{Type: "carewright-service-tokens", SecretRef: corev1.LocalObjectReference{Name: serviceTokenSecret}}
	writer.Spec.ApprovalSigning = &appsv1alpha1.ProviderSourceSpec{Type: "carewright-approval-signing", SecretRef: corev1.LocalObjectReference{Name: "carewright-approval-signing-key"}}
	writer.Spec.SessionSigning = &appsv1alpha1.SecretSourceSpec{SecretRef: corev1.LocalObjectReference{Name: "carewright-session-signing-key"}}
	if err := preparePipelineSecurity(ctx, c, writer); err != nil {
		t.Fatal(err)
	}
	// This is the shape already used in the shipped deployment overlays.
	var tokens corev1.Secret
	if err := c.Get(ctx, client.ObjectKey{Namespace: "test", Name: serviceTokenSecret}, &tokens); err != nil {
		t.Fatal(err)
	}
}

func TestScopedLocalCredentialsNeverEnterSandboxRequestValues(t *testing.T) {
	ctx := context.Background()
	s := securityTestScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	writer := securityTestWriter()
	if err := preparePipelineSecurity(ctx, c, writer); err != nil {
		t.Fatal(err)
	}
	if _, err := reconcileComponentSandboxes(ctx, c, s, writer, writer.Spec.Sandbox, "https://workflow.test", carePlanWriterComponents(writer, nil)); err != nil {
		t.Fatal(err)
	}
	var requests appsv1alpha1.SandboxRequestList
	if err := c.List(ctx, &requests); err != nil {
		t.Fatal(err)
	}
	if len(requests.Items) != 7 {
		t.Fatal("unexpected component count")
	}
	var tokens corev1.Secret
	if err := c.Get(ctx, client.ObjectKey{Namespace: "test", Name: serviceTokenSecret}, &tokens); err != nil {
		t.Fatal(err)
	}
	for _, request := range requests.Items {
		expected := localComponentCredentialKeys(request.Labels[componentLabel])
		files := 0
		for _, registration := range request.Spec.ProviderRegistrations {
			if registration.Type == "carewright-service-tokens" || registration.Type == "carewright-approval-signing" {
				t.Fatal("legacy blanket credential provider attached in local-file mode")
			}
			if len(registration.LocalCredentials) == 0 {
				continue
			}
			files++
			if len(registration.LocalCredentials) != len(expected) {
				t.Fatal("incorrect credential scope")
			}
			for _, key := range expected {
				if _, ok := registration.LocalCredentials[key]; !ok {
					t.Fatal("missing scoped credential")
				}
			}
			if request.Labels[componentLabel] == "fhir-server" && registration.LocalCredentials["APPROVAL_SIGNING_KEY"].Name != "carewright-approval-signing-key" {
				t.Fatal("signing key reference incorrect")
			}
			if len(request.Spec.Command) < 6 || request.Spec.Command[2] != localCredentialBootstrap {
				t.Fatal("startup wrapper missing")
			}
			if len(registration.Config) != 0 {
				t.Fatal("resolved payload in CR config")
			}
		}
		if (len(expected) > 0 && files != 1) || (len(expected) == 0 && files != 0) {
			t.Fatal("file provider attached to wrong component")
		}
		raw, _ := json.Marshal(request)
		for _, value := range tokens.Data {
			if strings.Contains(string(raw), string(value)) {
				t.Fatal("Secret value leaked into SandboxRequest")
			}
		}
	}
}

func TestBFFCredentialReferencesAndRevision(t *testing.T) {
	ctx := context.Background()
	s := securityTestScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	writer := securityTestWriter()
	if err := preparePipelineSecurity(ctx, c, writer); err != nil {
		t.Fatal(err)
	}
	if _, err := reconcileBFFDeployment(ctx, c, s, writer, carePlanWriterBFFComponent(writer, nil, "https://workflow.test"), nil); err != nil {
		t.Fatal(err)
	}
	var bff appsv1.Deployment
	if err := c.Get(ctx, client.ObjectKey{Namespace: "test", Name: "writer-bff"}, &bff); err != nil {
		t.Fatal(err)
	}
	if bff.Spec.Template.Annotations[securityRevisionAnnotation] == "" {
		t.Fatal("BFF rotation marker missing")
	}
	for _, env := range bff.Spec.Template.Spec.Containers[0].Env {
		if strings.HasPrefix(env.Name, "SERVICE_TOKEN_") || strings.HasSuffix(env.Name, "SIGNING_KEY") {
			if env.Value != "" || env.ValueFrom == nil || env.ValueFrom.SecretKeyRef == nil {
				t.Fatal("BFF credential not a Secret key reference")
			}
		}
	}
}

func TestLocalCredentialResolutionRotationAndWatch(t *testing.T) {
	ctx := context.Background()
	s := securityTestScheme(t)
	writer := securityTestWriter()
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(writer).Build()
	if err := preparePipelineSecurity(ctx, c, writer); err != nil {
		t.Fatal(err)
	}
	registration := componentLocalCredentialRegistration(writer, "fhir-server")
	payload, revision, err := resolveLocalCredentialConfig(ctx, c, "test", *registration)
	if err != nil {
		t.Fatal(err)
	}
	values := map[string]string{}
	if err := json.Unmarshal([]byte(payload["payload"]), &values); err != nil {
		t.Fatal(err)
	}
	if len(values) != 2 || values["APPROVAL_SIGNING_KEY"] == "" || values["SERVICE_TOKEN_ACP_FHIR_SERVER"] == "" {
		t.Fatal("wrong local payload")
	}
	var signing corev1.Secret
	if err := c.Get(ctx, client.ObjectKey{Namespace: "test", Name: "carewright-approval-signing-key"}, &signing); err != nil {
		t.Fatal(err)
	}
	if !registrationUsesSecret(*registration, signing.Name) {
		t.Fatal("secondary signing Secret not watched")
	}
	if len(pipelinesForSecuritySecret(ctx, c, &signing, true)) != 1 {
		t.Fatal("pipeline does not watch shared default Secret")
	}
	oldPipelineRevision := writer.Annotations[securityRevisionAnnotation]
	signing.Data["APPROVAL_SIGNING_KEY"] = []byte(strings.Repeat("a", 64))
	if err := c.Update(ctx, &signing); err != nil {
		t.Fatal(err)
	}
	_, rotated, err := resolveLocalCredentialConfig(ctx, c, "test", *registration)
	if err != nil {
		t.Fatal(err)
	}
	if rotated == revision {
		t.Fatal("file revision did not change")
	}
	writer = securityTestWriter()
	if err := preparePipelineSecurity(ctx, c, writer); err != nil {
		t.Fatal(err)
	}
	if oldPipelineRevision == writer.Annotations[securityRevisionAnnotation] {
		t.Fatal("BFF/workflow revision did not change")
	}
	request := &appsv1alpha1.SandboxRequest{Spec: appsv1alpha1.SandboxRequestSpec{ProviderRegistrations: []appsv1alpha1.SandboxProviderRegistration{*registration}}, Status: appsv1alpha1.SandboxRequestStatus{ProviderSyncMarkers: map[string]string{registration.Name: revision}}}
	before := localCredentialRevision(request)
	request.Status.ProviderSyncMarkers[registration.Name] = rotated
	if before == localCredentialRevision(request) {
		t.Fatal("running-process replacement marker did not change")
	}
	bad := *registration
	bad.Type = "other"
	if _, _, err := resolveLocalCredentialConfig(ctx, c, "test", bad); err == nil {
		t.Fatal("arbitrary profile accepted")
	}
	if _, _, err := resolveLocalCredentialConfig(ctx, c, "another-namespace", *registration); err == nil {
		t.Fatal("cross-namespace Secret resolution allowed")
	}
}

type credentialProfileStub struct {
	openshellv1.ProfileInterface
	current *openshellv1.ProviderProfile
	imports int
}

func (p *credentialProfileStub) Get(context.Context, string, string) (*openshellv1.ProviderProfile, error) {
	if p.current == nil {
		return nil, &openshellv1.StatusError{Code: openshellv1.ErrorNotFound}
	}
	return p.current, nil
}
func (p *credentialProfileStub) Import(_ context.Context, _ string, items []openshellv1.ProfileImportItem) (*openshellv1.ImportResult, error) {
	p.imports++
	p.current = &items[0].Profile
	return &openshellv1.ImportResult{Imported: true}, nil
}

type credentialProviderStub struct {
	openshellv1.ProviderInterface
	profiles *credentialProfileStub
}

func (p *credentialProviderStub) Profiles() openshellv1.ProfileInterface { return p.profiles }

type credentialClientStub struct {
	openshellv1.ClientInterface
	providers openshellv1.ProviderInterface
}

func (c *credentialClientStub) Providers() openshellv1.ProviderInterface { return c.providers }

func TestLocalCredentialProfileAndRegistrationAreScoped(t *testing.T) {
	ctx := context.Background()
	s := securityTestScheme(t)
	writer := securityTestWriter()
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if err := preparePipelineSecurity(ctx, c, writer); err != nil {
		t.Fatal(err)
	}
	registration := componentLocalCredentialRegistration(writer, "patient-data")
	request := &appsv1alpha1.SandboxRequest{ObjectMeta: metav1.ObjectMeta{Name: "patient", Namespace: "test"}, Spec: appsv1alpha1.SandboxRequestSpec{Providers: []string{registration.Name}, ProviderRegistrations: []appsv1alpha1.SandboxProviderRegistration{*registration}}}
	if err := c.Create(ctx, request); err != nil {
		t.Fatal(err)
	}
	// Use fake status subresource semantics for provider sync markers.
	c = fake.NewClientBuilder().WithScheme(s).WithStatusSubresource(request).WithObjects(request).Build()
	// Copy generated Secrets to this client's object tracker.
	for name, keys := range applicationSecretKeys {
		data := map[string][]byte{}
		for _, key := range keys {
			data[key] = []byte(strings.Repeat("b", 64))
		}
		if err := c.Create(ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "test"}, Data: data}); err != nil {
			t.Fatal(err)
		}
	}
	sdk := openshellfake.NewClient()
	defer func() { _ = sdk.Close() }()
	profiles := &credentialProfileStub{}
	stub := &credentialClientStub{ClientInterface: sdk, providers: &credentialProviderStub{ProviderInterface: sdk.Providers(), profiles: profiles}}
	reconciler := &SandboxRequestReconciler{Client: c, Scheme: s, ClientFactory: &fakeOpenShellClientFactory{client: stub}}
	if err := reconciler.registerProviders(ctx, request); err != nil {
		t.Fatal(err)
	}
	provider, err := sdk.Providers().Get(ctx, "default", registration.Name)
	if err != nil {
		t.Fatal(err)
	}
	if len(provider.Spec.Credentials) != 0 || len(provider.Spec.Config) != 1 {
		t.Fatal("wrong delivery channel")
	}
	values := map[string]string{}
	if err := json.Unmarshal([]byte(provider.Spec.Config["payload"]), &values); err != nil {
		t.Fatal(err)
	}
	if len(values) != 1 || values["SERVICE_TOKEN_ACP_PATIENT_DATA"] == "" {
		t.Fatal("unscoped provider payload")
	}
	raw, _ := json.Marshal(request)
	if strings.Contains(string(raw), strings.Repeat("b", 64)) {
		t.Fatal("payload leaked to request status")
	}
	if err := reconciler.registerProviders(ctx, request); err != nil {
		t.Fatal(err)
	}
	if profiles.imports != 1 {
		t.Fatal("profile import is not idempotent")
	}
	profiles.current.Files[0].Content = "unexpected"
	if err := reconciler.registerProviders(ctx, request); err == nil || !strings.Contains(err.Error(), "sensitive details suppressed") {
		t.Fatal("profile mismatch did not fail closed")
	}
}

func TestLocalCredentialBootstrapFailsClosed(t *testing.T) {
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 unavailable")
	}
	path := filepath.Join(t.TempDir(), "credentials.json")
	key := "SERVICE_TOKEN_ACP_PATIENT_DATA"
	value := strings.Repeat("c", 64)
	for _, test := range []struct {
		name, payload string
		ok            bool
	}{
		{"valid", `{"SERVICE_TOKEN_ACP_PATIENT_DATA":"` + value + `"}`, true},
		{"missing-key", `{}`, false}, {"extra-key", `{"UNEXPECTED":"value"}`, false}, {"wrong-format", `{"SERVICE_TOKEN_ACP_PATIENT_DATA":"short"}`, false}, {"malformed", `not-json`, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := os.WriteFile(path, []byte(test.payload), 0600); err != nil {
				t.Fatal(err)
			}
			command := exec.Command(python, "-c", localCredentialBootstrap, path, `["`+key+`"]`, python, "-c", `import os,sys;sys.exit(0 if len(os.environ["SERVICE_TOKEN_ACP_PATIENT_DATA"])==64 else 1)`)
			output, err := command.CombinedOutput()
			if test.ok && err != nil {
				t.Fatalf("valid file failed: %s", output)
			}
			if !test.ok && (err == nil || !strings.Contains(string(output), "refusing startup")) {
				t.Fatal("invalid file did not fail closed")
			}
			if strings.Contains(string(output), value) {
				t.Fatal("loader printed credential")
			}
		})
	}
}

func TestManagedWorkflowReferencesAndRotation(t *testing.T) {
	ctx := context.Background()
	s := securityTestScheme(t)
	s.AddKnownTypeWithName(sonataFlowGVK, &unstructured.Unstructured{})
	c := fake.NewClientBuilder().WithScheme(s).Build()
	writer := securityTestWriter()
	if err := preparePipelineSecurity(ctx, c, writer); err != nil {
		t.Fatal(err)
	}
	template := pipelineWorkflowTemplate{WorkflowYAML: carePlanWriterWorkflowYAML, PropsYAML: carePlanWriterPropsYAML, Replacements: map[string]string{}}
	if _, err := reconcilePipelineWorkflow(ctx, c, s, writer, template, nil, 8); err != nil {
		t.Fatal(err)
	}
	workflow := sonataFlowObject()
	if err := c.Get(ctx, client.ObjectKey{Namespace: writer.Namespace, Name: workflowResourceName(writer)}, workflow); err != nil {
		t.Fatal(err)
	}
	env, _, err := unstructured.NestedSlice(workflow.Object, "spec", "podTemplate", "container", "env")
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, raw := range env {
		entry := raw.(map[string]interface{})
		name := entry["name"].(string)
		if strings.Contains(name, "SIGNING_KEY") {
			t.Fatal("signing key delivered to workflow")
		}
		if strings.HasPrefix(name, "SERVICE_TOKEN_") {
			ref, ok, _ := unstructured.NestedString(entry, "valueFrom", "secretKeyRef", "name")
			if !ok || ref != serviceTokenSecret || entry["value"] != nil {
				t.Fatal("workflow token is not Secret-backed")
			}
			seen[name] = true
		}
		if name == "CAREWRIGHT_SECURITY_REVISION" && entry["value"] != writer.Annotations[securityRevisionAnnotation] {
			t.Fatal("workflow rotation marker incorrect")
		}
	}
	if len(seen) != len(pipelineTokenKeys(writer)) {
		t.Fatal("missing workflow credential references")
	}
}

func TestSecurityUnavailableClearsReady(t *testing.T) {
	ctx := context.Background()
	s := securityTestScheme(t)
	writer := securityTestWriter()
	meta.SetStatusCondition(&writer.Status.Conditions, metav1.Condition{Type: readyCondition, Status: metav1.ConditionTrue, Reason: "Ready"})
	c := fake.NewClientBuilder().WithScheme(s).WithStatusSubresource(writer).WithObjects(writer).Build()
	if err := reportPipelineSecurityFailure(ctx, c, writer, os.ErrNotExist); err == nil {
		t.Fatal("failure lost")
	}
	var current appsv1alpha1.CarePlanWriter
	if err := c.Get(ctx, client.ObjectKeyFromObject(writer), &current); err != nil {
		t.Fatal(err)
	}
	if !meta.IsStatusConditionFalse(current.Status.Conditions, readyCondition) || !meta.IsStatusConditionFalse(current.Status.Conditions, "SecurityReady") {
		t.Fatal("stale Ready=True survived credential failure")
	}
}

func TestManualCredentialOverridesRejected(t *testing.T) {
	for _, kind := range []string{"command", "provider", "environment"} {
		t.Run(kind, func(t *testing.T) {
			writer := securityTestWriter()
			switch kind {
			case "command":
				writer.Spec.PatientData.Command = []string{"python3", "-c", "# CAREWRIGHT_PROVIDER_FILE_BOOTSTRAP_V1"}
			case "provider":
				writer.Spec.PatientData.ExtraProviders = []string{"carewright-file-writer-patient-data"}
			case "environment":
				writer.Spec.PatientData.ExtraEnv = map[string]string{"SERVICE_TOKEN_ACP_PATIENT_DATA": "must-not-use"}
			}
			if err := validateCredentialMigration(writer); err == nil {
				t.Fatal("manual credential override not rejected")
			}
		})
	}
}

func TestLocalFileSandboxRecreatedOnRotation(t *testing.T) {
	assertValidLabels := func(labels map[string]string) {
		t.Helper()
		for key, value := range labels {
			if problems := validation.IsValidLabelValue(value); len(problems) != 0 {
				t.Errorf("sandbox label %q is invalid: %v", key, problems)
			}
		}
		if len(labels["carewright.io/spec-hash"]) != 63 {
			t.Error("credential revision hash must retain the existing 63-character label format")
		}
	}
	ctx := context.Background()
	s := securityTestScheme(t)
	writer := securityTestWriter()
	c := fake.NewClientBuilder().WithScheme(s).WithStatusSubresource(&appsv1alpha1.SandboxRequest{}).Build()
	if err := preparePipelineSecurity(ctx, c, writer); err != nil {
		t.Fatal(err)
	}
	registration := componentLocalCredentialRegistration(writer, "patient-data")
	request := &appsv1alpha1.SandboxRequest{ObjectMeta: metav1.ObjectMeta{Name: "patient-rotation", Namespace: "test"}, Spec: appsv1alpha1.SandboxRequestSpec{
		Image: "example.invalid/component:test", Providers: []string{registration.Name}, ProviderRegistrations: []appsv1alpha1.SandboxProviderRegistration{*registration},
	}}
	if err := c.Create(ctx, request); err != nil {
		t.Fatal(err)
	}
	sdk := openshellfake.NewClient()
	defer func() { _ = sdk.Close() }()
	stub := &credentialClientStub{ClientInterface: sdk, providers: &credentialProviderStub{ProviderInterface: sdk.Providers(), profiles: &credentialProfileStub{}}}
	r := &SandboxRequestReconciler{Client: c, Scheme: s, ClientFactory: &fakeOpenShellClientFactory{client: stub}}
	key := reconcile.Request{NamespacedName: client.ObjectKeyFromObject(request)}
	// First reconciliation installs the finalizer; second creates the sandbox.
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, key); err != nil {
			t.Fatal(err)
		}
	}
	first, err := sdk.Sandboxes().Get(ctx, "default", request.Name)
	if err != nil {
		t.Fatal(err)
	}
	assertValidLabels(first.Labels)
	if _, err := r.Reconcile(ctx, key); err != nil {
		t.Fatal(err)
	}
	unchanged, err := sdk.Sandboxes().Get(ctx, "default", request.Name)
	if err != nil {
		t.Fatal(err)
	}
	if !first.CreatedAt.Equal(unchanged.CreatedAt) {
		t.Fatal("stable credentials unnecessarily replaced sandbox")
	}
	var secret corev1.Secret
	if err := c.Get(ctx, client.ObjectKey{Namespace: "test", Name: serviceTokenSecret}, &secret); err != nil {
		t.Fatal(err)
	}
	secret.Data["SERVICE_TOKEN_ACP_PATIENT_DATA"] = []byte(strings.Repeat("d", 64))
	if err := c.Update(ctx, &secret); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, key); err != nil {
		t.Fatal(err)
	}
	rotated, err := sdk.Sandboxes().Get(ctx, "default", request.Name)
	if err != nil {
		t.Fatal(err)
	}
	assertValidLabels(rotated.Labels)
	if !rotated.CreatedAt.After(first.CreatedAt) || first.Labels["carewright.io/spec-hash"] == rotated.Labels["carewright.io/spec-hash"] {
		t.Fatal("file credentials updated without replacing the running process")
	}
}

func TestBootstrapCanDelegateApplicationKeyGeneration(t *testing.T) {
	script, err := os.ReadFile(filepath.Join("..", "..", "scripts", "bootstrap-prerequisites.sh"))
	if err != nil {
		t.Fatal(err)
	}
	start := strings.Index(string(script), "ensure_security_secrets() {")
	if start < 0 {
		t.Fatal("bootstrap security function not found")
	}
	tail := string(script[start:])
	end := strings.Index(tail, "\n}\n")
	if end < 0 {
		t.Fatal("bootstrap security function not terminated")
	}
	function := tail[:end+3]
	assignment := ""
	for _, line := range strings.Split(string(script), "\n") {
		if strings.HasPrefix(line, "operator_managed_security=") {
			assignment = line
			break
		}
	}
	if assignment == "" {
		t.Fatal("bootstrap delegation default missing")
	}

	// Any call to the legacy Secret generation path fails this test.
	command := exec.Command("bash", "-c", `set -e
log() { :; }
exists() { exit 91; }
oc() { exit 92; }
openssl() { exit 93; }
unset OPERATOR_MANAGED_SECURITY
`+assignment+"\n"+function+"\nensure_security_secrets\n")
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("delegation called bootstrap key generation: %s", output)
	}
	legacy := exec.Command("bash", "-c", `set -e
log() { :; }
exists() { exit 91; }
OPERATOR_MANAGED_SECURITY=0
`+assignment+"\n"+function+"\nensure_security_secrets\n")
	if err := legacy.Run(); err == nil {
		t.Fatal("bootstrap opt-out did not reach legacy generation")
	} else if exit, ok := err.(*exec.ExitError); !ok || exit.ExitCode() != 91 {
		t.Fatal("unexpected bootstrap opt-out failure")
	}

}
