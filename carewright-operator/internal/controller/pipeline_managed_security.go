package controller

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"slices"
	"sort"
	"strings"

	appsv1alpha1 "carewright.io/carewright-operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	meta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const securityRevisionAnnotation = "apps.carewright.io/security-revision"
const serviceTokenSecret = "carewright-service-tokens"
const approvalSigningKey = "APPROVAL_SIGNING_KEY"

var applicationSecretKeys = map[string][]string{
	serviceTokenSecret: {
		"SERVICE_TOKEN_ACP_BFF", "SERVICE_TOKEN_ACP_DECISION_ENGINE", "SERVICE_TOKEN_ACP_LLM_REASONING",
		"SERVICE_TOKEN_ACP_PATIENT_DATA", "SERVICE_TOKEN_ACP_FHIR_GENERATION", "SERVICE_TOKEN_ACP_FHIR_SERVER",
		"SERVICE_TOKEN_CPG_BFF", "SERVICE_TOKEN_CPG_INGESTION", "SERVICE_TOKEN_CPG_LLM_ANALYSIS",
		"SERVICE_TOKEN_CPG_ASSEMBLY", "SERVICE_TOKEN_CPG_DELIVERY",
	},
	"carewright-approval-signing-key": {approvalSigningKey},
	"carewright-session-signing-key":  {"SESSION_SIGNING_KEY"},
	"carewright-ingester-signing-key": {"CPG_SIGNING_KEY"},
}

type resolvedPipelineSecurity struct {
	ManagedSecrets       bool
	LocalCredentialFiles bool
}

// Resolve defaults at runtime as well as in the CRD: fake clients and older
// stored resources may omit the security object or either individual field.
func pipelineSecurityOptions(owner client.Object) *resolvedPipelineSecurity {
	var spec *appsv1alpha1.PipelineSecuritySpec
	switch p := owner.(type) {
	case *appsv1alpha1.CarePlanWriter:
		spec = p.Spec.Security
	case *appsv1alpha1.CPGIngester:
		spec = p.Spec.Security
	default:
		return nil
	}
	options := &resolvedPipelineSecurity{ManagedSecrets: true, LocalCredentialFiles: true}
	if spec != nil {
		if spec.ManagedSecrets != nil {
			options.ManagedSecrets = *spec.ManagedSecrets
		}
		if spec.LocalCredentialFiles != nil {
			options.LocalCredentialFiles = *spec.LocalCredentialFiles
		}
	}
	return options
}

// preparePipelineSecurity changes only the effective in-memory pipeline copy.
// Namespace-shared Secrets deliberately have no pipeline owner: deleting one
// pipeline must not garbage-collect another pipeline's tokens or signing keys.
func preparePipelineSecurity(ctx context.Context, c client.Client, owner client.Object) error {
	options := pipelineSecurityOptions(owner)
	if options == nil || (!options.ManagedSecrets && !options.LocalCredentialFiles) {
		return nil
	}
	if err := validateCredentialMigration(owner); err != nil {
		return err
	}
	defaults := map[string]bool{}
	provider := func(source **appsv1alpha1.ProviderSourceSpec, name, profile string) {
		if !options.ManagedSecrets {
			return
		}
		if *source == nil {
			*source = &appsv1alpha1.ProviderSourceSpec{Type: profile, SecretRef: corev1.LocalObjectReference{Name: name}}
		}
		if (*source).SecretRef.Name == name && (*source).Type == profile {
			defaults[name] = true
		}
	}
	signing := func(source **appsv1alpha1.SecretSourceSpec, name string) {
		if !options.ManagedSecrets {
			return
		}
		if *source == nil {
			*source = &appsv1alpha1.SecretSourceSpec{SecretRef: corev1.LocalObjectReference{Name: name}}
		}
		if (*source).SecretRef.Name == name {
			defaults[name] = true
		}
	}
	switch p := owner.(type) {
	case *appsv1alpha1.CarePlanWriter:
		provider(&p.Spec.ServiceTokens, serviceTokenSecret, "carewright-service-tokens")
		provider(&p.Spec.ApprovalSigning, "carewright-approval-signing-key", "carewright-approval-signing")
		signing(&p.Spec.SessionSigning, "carewright-session-signing-key")
	case *appsv1alpha1.CPGIngester:
		provider(&p.Spec.ServiceTokens, serviceTokenSecret, "carewright-service-tokens")
		signing(&p.Spec.Signing, "carewright-ingester-signing-key")
	}
	for name := range defaults {
		key := client.ObjectKey{Namespace: owner.GetNamespace(), Name: name}
		var existing corev1.Secret
		if err := c.Get(ctx, key, &existing); apierrors.IsNotFound(err) {
			data := map[string][]byte{}
			for _, field := range applicationSecretKeys[name] {
				value := make([]byte, 32)
				if _, err := rand.Read(value); err != nil {
					return fmt.Errorf("generate application credential")
				}
				data[field] = []byte(hex.EncodeToString(value))
			}
			desired := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: owner.GetNamespace(), Labels: map[string]string{managedByLabel: "security-controller"}}, Type: corev1.SecretTypeOpaque, Data: data}
			if err := c.Create(ctx, desired); err != nil && !apierrors.IsAlreadyExists(err) {
				return fmt.Errorf("create application Secret %s: %w", name, err)
			}
		} else if err != nil {
			return fmt.Errorf("read application Secret %s: %w", name, err)
		}
	}
	revision, err := pipelineSecurityRevision(ctx, c, owner)
	if err != nil {
		return err
	}
	annotations := owner.GetAnnotations()
	if annotations == nil {
		annotations = map[string]string{}
	}
	annotations[securityRevisionAnnotation] = revision
	owner.SetAnnotations(annotations)
	return nil
}

// pipelineSecurityReferences includes only application credentials, not LLM or
// FHIR OAuth credentials, which are provisioned and rotated externally.
func pipelineSecurityReferences(owner client.Object) map[string][]string {
	refs := map[string][]string{}
	add := func(name string, keys ...string) {
		if name != "" {
			refs[name] = append(refs[name], keys...)
		}
	}
	tokens, approval := pipelineSecuritySources(owner)
	if tokens != nil {
		add(tokens.SecretRef.Name, pipelineTokenKeys(owner)...)
	}
	if approval != nil {
		add(approval.SecretRef.Name, approvalSigningKey)
	}
	switch p := owner.(type) {
	case *appsv1alpha1.CarePlanWriter:
		if p.Spec.SessionSigning != nil {
			add(p.Spec.SessionSigning.SecretRef.Name, "SESSION_SIGNING_KEY")
		}
	case *appsv1alpha1.CPGIngester:
		if p.Spec.Signing != nil {
			add(p.Spec.Signing.SecretRef.Name, "CPG_SIGNING_KEY")
		}
	}
	return refs
}

func pipelineSecurityRevision(ctx context.Context, c client.Client, owner client.Object) (string, error) {
	tokens, approval := pipelineSecuritySources(owner)
	if tokens == nil {
		return "", fmt.Errorf("application security requires service-token and signing Secret references")
	}
	if _, writer := owner.(*appsv1alpha1.CarePlanWriter); writer && pipelineSecurityOptions(owner).LocalCredentialFiles && approval == nil {
		return "", fmt.Errorf("local credential files require approvalSigning.secretRef")
	}
	markers := map[string]string{}
	for name, keys := range pipelineSecurityReferences(owner) {
		var secret corev1.Secret
		if err := c.Get(ctx, client.ObjectKey{Namespace: owner.GetNamespace(), Name: name}, &secret); err != nil {
			return "", fmt.Errorf("read application Secret %s: %w", name, err)
		}
		for _, key := range keys {
			value := secret.Data[key]
			if len(value) == 0 {
				return "", fmt.Errorf("application Secret %s is missing required key %s", name, key)
			}
			if pipelineSecurityOptions(owner).LocalCredentialFiles && (slices.Contains(pipelineTokenKeys(owner), key) || key == approvalSigningKey) {
				if len(value) != 64 {
					return "", fmt.Errorf("local credential files require 32-byte hex credentials: %s/%s", name, key)
				}
				if _, err := hex.DecodeString(string(value)); err != nil {
					return "", fmt.Errorf("local credential file key is not hex: %s/%s", name, key)
				}
			}
		}
		markers[name] = string(secret.UID) + "/" + secret.ResourceVersion
	}
	encoded, _ := json.Marshal(markers)
	sum := sha256.Sum256(encoded)
	return hex.EncodeToString(sum[:]), nil
}

// Watch namespace-local Secrets so browser/workflow consumers roll after rotation.
// Generated defaults are also matched before references have been materialized.
func pipelinesForSecuritySecret(ctx context.Context, c client.Client, object client.Object, writer bool) []reconcile.Request {
	var owners []client.Object
	if writer {
		var list appsv1alpha1.CarePlanWriterList
		if err := c.List(ctx, &list, client.InNamespace(object.GetNamespace())); err != nil {
			return nil
		}
		for i := range list.Items {
			owners = append(owners, &list.Items[i])
		}
	} else {
		var list appsv1alpha1.CPGIngesterList
		if err := c.List(ctx, &list, client.InNamespace(object.GetNamespace())); err != nil {
			return nil
		}
		for i := range list.Items {
			owners = append(owners, &list.Items[i])
		}
	}
	result := []reconcile.Request{}
	for _, owner := range owners {
		options := pipelineSecurityOptions(owner)
		if options == nil || (!options.ManagedSecrets && !options.LocalCredentialFiles) {
			continue
		}
		_, explicit := pipelineSecurityReferences(owner)[object.GetName()]
		_, standard := applicationSecretKeys[object.GetName()]
		if explicit || (options.ManagedSecrets && standard) {
			result = append(result, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(owner)})
		}
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Name < result[j].Name })
	return result
}

// CredentialsUnavailable must clear stale Ready=True instead of silently leaving
// a pipeline looking healthy after a required key disappears or becomes invalid.
func reportPipelineSecurityFailure(ctx context.Context, c client.Client, owner client.Object, cause error) error {
	before := owner.DeepCopyObject().(client.Object)
	conditions := pipelineConditions(owner)
	for _, name := range []string{"SecurityReady", readyCondition} {
		meta.SetStatusCondition(conditions, metav1.Condition{Type: name, Status: metav1.ConditionFalse,
			ObservedGeneration: owner.GetGeneration(), Reason: "CredentialsUnavailable",
			Message: "Required application credentials are unavailable or invalid; dependent reconciliation stopped"})
	}
	if err := c.Status().Patch(ctx, owner, client.MergeFrom(before)); err != nil {
		return err
	}
	return cause
}

func pipelineConditions(owner client.Object) *[]metav1.Condition {
	switch p := owner.(type) {
	case *appsv1alpha1.CarePlanWriter:
		return &p.Status.Conditions
	case *appsv1alpha1.CPGIngester:
		return &p.Status.Conditions
	}
	panic("unsupported pipeline type")
}

func markPipelineSecurityReady(owner client.Object) {
	options := pipelineSecurityOptions(owner)
	if options == nil || (!options.ManagedSecrets && !options.LocalCredentialFiles) {
		return
	}
	meta.SetStatusCondition(pipelineConditions(owner), metav1.Condition{Type: "SecurityReady", Status: metav1.ConditionTrue,
		ObservedGeneration: owner.GetGeneration(), Reason: "CredentialsResolved", Message: "Application credential references were validated; workload readiness is tracked separately"})
}

func validateCredentialMigration(owner client.Object) error {
	options := pipelineSecurityOptions(owner)
	if options == nil || !options.LocalCredentialFiles {
		return nil
	}
	var components []sandboxComponent
	switch p := owner.(type) {
	case *appsv1alpha1.CarePlanWriter:
		components = carePlanWriterComponents(p, nil)
		components = append(components, sandboxComponent{Spec: p.Spec.BFF.ComponentSpec}, sandboxComponent{Spec: p.Spec.UI})
	case *appsv1alpha1.CPGIngester:
		components = cpgIngesterComponents(p)
		components = append(components, sandboxComponent{Spec: p.Spec.BFF.ComponentSpec}, sandboxComponent{Spec: p.Spec.UI})
	}
	for _, component := range components {
		for _, argument := range component.Spec.Command {
			if strings.Contains(argument, "CAREWRIGHT_PROVIDER_FILE_BOOTSTRAP_V1") || argument == localCredentialBootstrap {
				return fmt.Errorf("remove manual local-credential command overrides before enabling security.localCredentialFiles")
			}
		}
		for _, provider := range component.Spec.ExtraProviders {
			if strings.HasPrefix(provider, "carewright-file-") {
				return fmt.Errorf("remove manual carewright-file providers before enabling security.localCredentialFiles")
			}
		}
		for key := range component.Spec.ExtraEnv {
			if strings.HasPrefix(key, "SERVICE_TOKEN_") || key == approvalSigningKey || key == "SESSION_SIGNING_KEY" || key == "CPG_SIGNING_KEY" {
				return fmt.Errorf("application credentials must not be supplied through component extraEnv")
			}
		}
	}
	return nil
}
