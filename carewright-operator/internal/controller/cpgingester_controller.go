/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"maps"
	"strconv"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	meta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	appsv1alpha1 "carewright.io/carewright-operator/api/v1alpha1"
)

// CPGIngesterReconciler reconciles a CPGIngester object
type CPGIngesterReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=apps.carewright.io,resources=cpgingesters,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=apps.carewright.io,resources=cpgingesters/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=apps.carewright.io,resources=cpgingesters/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps.carewright.io,resources=careplanwriters,verbs=get;list;watch
// +kubebuilder:rbac:groups=apps.carewright.io,resources=sandboxrequests,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=sonataflow.org,resources=sonataflows,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=aistor.min.io,resources=objectstores,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=configmaps;secrets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=services,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=networking.k8s.io,resources=networkpolicies,verbs=get;delete
// +kubebuilder:rbac:groups=agents.x-k8s.io,resources=sandboxes,verbs=get;list;watch
// +kubebuilder:rbac:groups=route.openshift.io,resources=routes,verbs=get;list;watch;create;update;patch;delete

// Reconcile manages sandboxed processing components and regular UI/BFF Deployments.
func (r *CPGIngesterReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	var ingester appsv1alpha1.CPGIngester
	if err := r.Get(ctx, req.NamespacedName, &ingester); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	artifactStore, err := reconcileArtifactStore(ctx, r.Client, &ingester, ingester.Spec.ArtifactStore)
	if err != nil {
		return ctrl.Result{}, err
	}
	effective := ingester.DeepCopy()
	effective.Spec.ArtifactStore = artifactStore.Spec

	knownEndpoints, err := componentServiceURLs(ctx, r.Client, &ingester)
	if err != nil {
		return ctrl.Result{}, err
	}
	knownEndpoints["bff"] = bffServiceURL(&ingester)

	literals := map[string]string{}
	writerBFFURL := ""
	if ingester.Spec.CarePlanWriterRef != nil {
		var writer appsv1alpha1.CarePlanWriter
		if err := r.Get(ctx, client.ObjectKey{Name: ingester.Spec.CarePlanWriterRef.Name, Namespace: ingester.Namespace}, &writer); err != nil && !apierrors.IsNotFound(err) {
			return ctrl.Result{}, err
		} else if err == nil && writer.Status.Endpoints["bff"] != "" {
			writerBFFURL = strings.TrimRight(writer.Status.Endpoints["bff"], "/")
			literals["http://acp-bff:8080"] = writerBFFURL
		}
	}
	template := pipelineWorkflowTemplate{
		WorkflowYAML: cpgIngesterWorkflowYAML,
		PropsYAML:    cpgIngesterPropsYAML,
		Replacements: map[string]string{
			"http://cpg-ingester-ingestion:8080":    "ingestion",
			"http://cpg-ingester-llm-analysis:8080": "llm-analysis",
			"http://cpg-ingester-assembly:8080":     "assembly",
			"http://cpg-ingester-delivery:8080":     "delivery",
			"http://cpg-ingester-bff:8080":          "bff",
		},
		LiteralReplacements: literals,
	}
	components := cpgIngesterComponents(effective)
	workflow, err := reconcilePipelineWorkflow(ctx, r.Client, r.Scheme, &ingester, template, knownEndpoints, len(components)+1)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !workflow.Ready {
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}
	components = cpgIngesterComponents(effective)

	summary, err := reconcileComponentSandboxes(ctx, r.Client, r.Scheme, &ingester, ingester.Spec.Sandbox, workflow.URL, components)
	if err != nil {
		return ctrl.Result{}, err
	}
	bff, err := reconcileBFFDeployment(ctx, r.Client, r.Scheme, &ingester, cpgIngesterBFFComponent(effective, workflow.URL, writerBFFURL), effective.Spec.ArtifactStore)
	if err != nil {
		return ctrl.Result{}, err
	}
	summary.Desired++
	summary.Endpoints["bff"] = bff.URL
	if bff.Ready {
		summary.Ready++
	}
	exposure, err := reconcileUIExposure(ctx, r.Client, r.Scheme, &ingester, ingester.Spec.UI)
	if err != nil {
		return ctrl.Result{}, err
	}
	workflow, err = reconcilePipelineWorkflow(ctx, r.Client, r.Scheme, &ingester, template, summary.Endpoints, summary.Desired)
	if err != nil {
		return ctrl.Result{}, err
	}
	before := ingester.DeepCopy()
	ingester.Status.ObservedGeneration = ingester.Generation
	ingester.Status.Endpoints = maps.Clone(summary.Endpoints)
	if artifactStore.URL != "" {
		ingester.Status.Endpoints["artifactStore"] = artifactStore.URL
	}
	if workflow.Created {
		ingester.Status.Endpoints["workflow"] = workflow.URL
	}
	if exposure.URL != "" {
		ingester.Status.Endpoints["ui"] = exposure.URL
	}
	meta.SetStatusCondition(&ingester.Status.Conditions, metav1.Condition{
		Type:               "Accepted",
		Status:             metav1.ConditionTrue,
		ObservedGeneration: ingester.Generation,
		Reason:             "SpecAccepted",
		Message:            "The CPGIngester components have been reconciled",
	})
	setPipelineReadyCondition(&ingester.Status.Conditions, ingester.Generation, summary, workflow)
	exposure.Ready = exposure.Ready && bff.Ready
	setUIExposureCondition(&ingester.Status.Conditions, ingester.Generation, exposure)
	if err := r.Status().Patch(ctx, &ingester, client.MergeFrom(before)); err != nil {
		return ctrl.Result{}, err
	}

	log.Info("reconciled CPGIngester components", "generation", ingester.Generation, "ready", summary.Ready, "desired", summary.Desired)

	if exposure.URL == "" || !exposure.Ready {
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}
	return ctrl.Result{}, nil
}

// SetupWithManager sets up the controller with the Manager.
func (r *CPGIngesterReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&appsv1alpha1.CPGIngester{}).
		Watches(&appsv1alpha1.CarePlanWriter{}, handler.EnqueueRequestsFromMapFunc(r.ingestersForWriter)).
		Owns(&appsv1alpha1.SandboxRequest{}).
		Owns(sonataFlowObject()).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.Service{}).
		Owns(&appsv1.Deployment{}).
		Owns(routeObject()).
		Named("cpgingester").
		Complete(r)
}

func (r *CPGIngesterReconciler) ingestersForWriter(ctx context.Context, writer client.Object) []reconcile.Request {
	var ingesters appsv1alpha1.CPGIngesterList
	if err := r.List(ctx, &ingesters, client.InNamespace(writer.GetNamespace())); err != nil {
		logf.FromContext(ctx).Error(err, "list CPGIngesters referencing CarePlanWriter", "writer", client.ObjectKeyFromObject(writer))
		return nil
	}
	var requests []reconcile.Request
	for index := range ingesters.Items {
		ingester := &ingesters.Items[index]
		if ingester.Spec.CarePlanWriterRef != nil && ingester.Spec.CarePlanWriterRef.Name == writer.GetName() {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(ingester)})
		}
	}
	return requests
}

func cpgIngesterComponents(ingester *appsv1alpha1.CPGIngester) []sandboxComponent {
	artifactEnv, externalArtifactProvider := artifactStoreInputs(ingester.Spec.ArtifactStore)
	var artifactSource *appsv1alpha1.ProviderSourceSpec
	if ingester.Spec.ArtifactStore != nil {
		artifactSource = ingester.Spec.ArtifactStore.Credentials
	}
	artifactProvider, artifactRegistration := managedProvider(ingester, "artifact", externalArtifactProvider, artifactSource)
	artifactAccess := artifactStoreNetworkAccess(artifactEnv["ARTIFACT_STORE_URL"], artifactProvider)
	observabilityEnv := observabilityInputs(ingester.Spec.Observability)
	llmEnv, externalLLMProvider := llmInputs(ingester.Spec.LLM)
	var llmSource *appsv1alpha1.ProviderSourceSpec
	if ingester.Spec.LLM != nil {
		llmSource = ingester.Spec.LLM.Credentials
	}
	llmProvider, llmRegistration := managedProvider(ingester, "llm", externalLLMProvider, llmSource)
	inferenceAccess := networkAccessForURL("inference", llmEnv["LITELLM_URL"])

	ingestionEnv := mergedEnv(artifactEnv, observabilityEnv)
	setIfNotEmpty(ingestionEnv, "LOG_LEVEL", ingester.Spec.Ingestion.LogLevel)
	setIfNotEmpty(ingestionEnv, "DOCLING_LOG_LEVEL", ingester.Spec.Ingestion.DoclingLogLevel)
	ingestionEnv["PYTHONUNBUFFERED"] = strconv.FormatBool(ingester.Spec.Ingestion.PythonUnbuffered)
	setIfNotEmpty(ingestionEnv, "DOCLING_CACHE_DIR", ingester.Spec.Ingestion.DoclingCacheDirectory)
	setIfNotEmpty(ingestionEnv, "DOCLING_ARTIFACTS_PATH", ingester.Spec.Ingestion.DoclingArtifactsPath)
	ingestionEnv["HF_HUB_ENABLE_HF_TRANSFER"] = boolInt(ingester.Spec.Ingestion.HuggingFaceTransferEnabled)
	ingestionEnv["HF_HUB_OFFLINE"] = boolInt(ingester.Spec.Ingestion.HuggingFaceOffline)
	ingestionEnv["INGESTION_OCR_ENABLED"] = strconv.FormatBool(ingester.Spec.Ingestion.OCREnabled)

	analysisEnv := mergedEnv(artifactEnv, observabilityEnv)
	analysisEnv = mergedEnv(analysisEnv, llmEnv)
	setIfNotEmpty(analysisEnv, "PYTHONPATH", ingester.Spec.LLMAnalysis.PythonPath)
	analysisEnv["FIGURE_INTERPRETATION_ENABLED"] = strconv.FormatBool(ingester.Spec.LLMAnalysis.FigureInterpretationEnabled)
	analysisEnv["FIGURE_INTERPRETATION_MAX_FIGURES"] = strconv.FormatInt(int64(ingester.Spec.LLMAnalysis.FigureInterpretationMaxFigures), 10)

	pythonEnv := func(spec appsv1alpha1.PythonComponentSpec) map[string]string {
		env := mergedEnv(artifactEnv, observabilityEnv)
		setIfNotEmpty(env, "PYTHONPATH", spec.PythonPath)
		return env
	}
	return []sandboxComponent{
		{Name: "ingestion", Port: 8080, Spec: ingester.Spec.Ingestion.ComponentSpec, Command: pythonServiceCommand("cpg_ingester.services.ingestion:app", "8080"), Env: ingestionEnv, Providers: []string{artifactProvider}, Registrations: providerRegistrations(artifactRegistration), NetworkAccess: artifactAccess},
		{Name: "llm-analysis", Port: 8080, Spec: ingester.Spec.LLMAnalysis.ComponentSpec, Command: pythonServiceCommand("cpg_ingester.services.llm_analysis:app", "8080"), Env: analysisEnv, Providers: []string{artifactProvider, llmProvider}, Registrations: providerRegistrations(artifactRegistration, llmRegistration), NetworkAccess: combinedNetworkAccess(artifactAccess, inferenceAccess)},
		{Name: "assembly", Port: 8080, Spec: ingester.Spec.Assembly.ComponentSpec, Command: pythonServiceCommand("cpg_ingester.services.assembly_svc:app", "8080"), Env: pythonEnv(ingester.Spec.Assembly), Providers: []string{artifactProvider}, Registrations: providerRegistrations(artifactRegistration), NetworkAccess: artifactAccess},
		{Name: "delivery", Port: 8080, Spec: ingester.Spec.Delivery.ComponentSpec, Command: pythonServiceCommand("cpg_ingester.services.delivery_svc:app", "8080"), Env: pythonEnv(ingester.Spec.Delivery), Providers: []string{artifactProvider}, Registrations: providerRegistrations(artifactRegistration), NetworkAccess: artifactAccess},
	}
}

func cpgIngesterBFFComponent(ingester *appsv1alpha1.CPGIngester, workflowURL, writerBFFURL string) sandboxComponent {
	artifactEnv, _ := artifactStoreInputs(ingester.Spec.ArtifactStore)
	env := mergedEnv(artifactEnv, observabilityInputs(ingester.Spec.Observability))
	setIfNotEmpty(env, "PYTHONPATH", ingester.Spec.BFF.PythonPath)
	env["SONATAFLOW_URL"] = workflowOriginURL(workflowURL)
	env["SONATAFLOW_WORKFLOW_NAME"] = workflowResourceName(ingester)
	setIfNotEmpty(env, "ACP_WRITER_BFF_URL", writerBFFURL)
	if ingester.Spec.ArtifactStore != nil {
		setIfNotEmpty(env, "MINIO_ENDPOINT", ingester.Spec.ArtifactStore.URL)
	}
	return sandboxComponent{Name: "bff", Port: 8080, Spec: ingester.Spec.BFF.ComponentSpec, Command: pythonServiceCommand("cpg_ingester.services.bff:app", "8080"), Env: env}
}
