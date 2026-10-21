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
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	appsv1alpha1 "carewright.io/carewright-operator/api/v1alpha1"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	meta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

var _ = Describe("CarePlanWriter Controller", func() {
	const resourceName = "careplanwriter-controller-test"
	resourceKey := types.NamespacedName{Name: resourceName, Namespace: "default"}

	AfterEach(func() {
		var requests appsv1alpha1.SandboxRequestList
		Expect(k8sClient.List(ctx, &requests, client.InNamespace("default"))).To(Succeed())
		for index := range requests.Items {
			if requests.Items[index].Labels[pipelineLabel] != "" {
				Expect(k8sClient.Delete(ctx, &requests.Items[index])).To(Succeed())
			}
		}
		resource := &appsv1alpha1.CarePlanWriter{}
		if err := k8sClient.Get(ctx, resourceKey, resource); err == nil {
			var policies corev1.ConfigMapList
			Expect(k8sClient.List(ctx, &policies, client.InNamespace("default"), client.MatchingLabels{pipelineLabel: string(resource.UID)})).To(Succeed())
			for index := range policies.Items {
				if metav1.IsControlledBy(&policies.Items[index], resource) {
					Expect(k8sClient.Delete(ctx, &policies.Items[index])).To(Succeed())
				}
			}
			Expect(k8sClient.Delete(ctx, resource)).To(Succeed())
		}
	})

	It("manages all component SandboxRequests and removes stale children", func() {
		image := func(name string) appsv1alpha1.ComponentSpec {
			return appsv1alpha1.ComponentSpec{Image: "example.invalid/" + name + ":latest"}
		}
		resource := &appsv1alpha1.CarePlanWriter{
			ObjectMeta: metav1.ObjectMeta{Name: resourceName, Namespace: "default"},
			Spec: appsv1alpha1.CarePlanWriterSpec{
				Sandbox:       appsv1alpha1.PipelineSandboxSpec{Gateway: appsv1alpha1.SandboxGatewaySpec{Endpoint: "https://openshell.test"}, ApprovalMode: "auto"},
				ArtifactStore: &appsv1alpha1.ArtifactStoreSpec{URL: "http://minio.test:9000", ArtifactBucket: "artifacts", PHIBucket: "phi", Credentials: &appsv1alpha1.ProviderSourceSpec{Type: "carewright-minio", SecretRef: corev1.LocalObjectReference{Name: "artifact-secret"}}},
				Observability: &appsv1alpha1.ObservabilitySpec{MLflowTrackingURI: "http://mlflow.test:5000"},
				LLM:           &appsv1alpha1.LLMConfigSpec{URL: "http://litellm.test:4000", Model: "model", CredentialsProvider: "llm-provider", RequestTimeoutSeconds: 60},
				FHIRTarget:    &appsv1alpha1.FHIRTargetSpec{URL: "https://fhir.test/R4", CredentialsProvider: "fhir-provider"},
				AITransparency: &appsv1alpha1.AITransparencySpec{
					CapturePrompts: true,
					ModelCardURL:   "https://models.test/card",
					Reviewer:       &appsv1alpha1.ReviewerSpec{Display: "Clinician", Reference: "Practitioner/1"},
				},
				PatientData: appsv1alpha1.PythonComponentSpec{ComponentSpec: image("patient-data"), PythonPath: "/app/src"},
				LLMReasoning: appsv1alpha1.CarePlanLLMReasoningComponentSpec{
					PythonComponentSpec: appsv1alpha1.PythonComponentSpec{ComponentSpec: image("llm"), PythonPath: "/app/src"},
					Embedding:           &appsv1alpha1.EmbeddingSpec{Provider: "openai", Model: "embedding-model", URL: "https://embedding.test", CredentialsProvider: "embedding-provider"},
				},
				DecisionEngine:  appsv1alpha1.PythonComponentSpec{ComponentSpec: image("decision")},
				FHIRGeneration:  appsv1alpha1.PythonComponentSpec{ComponentSpec: image("fhir-gen")},
				FHIRServer:      appsv1alpha1.PythonComponentSpec{ComponentSpec: image("fhir-server")},
				BFF:             appsv1alpha1.PythonComponentSpec{ComponentSpec: image("bff")},
				UI:              image("ui"),
				MCP:             appsv1alpha1.PythonComponentSpec{ComponentSpec: image("mcp")},
				DecisionService: appsv1alpha1.DecisionServiceComponentSpec{ComponentSpec: image("decision-service"), JavaOptions: "-Xmx256m"},
			},
		}
		Expect(k8sClient.Create(ctx, resource)).To(Succeed())
		artifactProvider, _ := managedProvider(resource, "artifact", "", resource.Spec.ArtifactStore.Credentials)

		stale := &appsv1alpha1.SandboxRequest{
			ObjectMeta: metav1.ObjectMeta{
				Name:      resourceName + "-bff",
				Namespace: "default",
				Labels:    map[string]string{managedByLabel: managedByValue, pipelineLabel: string(resource.UID)},
			},
			Spec: appsv1alpha1.SandboxRequestSpec{Image: "example.invalid/stale:latest"},
		}
		Expect(controllerutil.SetControllerReference(resource, stale, k8sClient.Scheme())).To(Succeed())
		Expect(k8sClient.Create(ctx, stale)).To(Succeed())

		reconciler := &CarePlanWriterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		_, err := reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: resourceKey})
		Expect(err).NotTo(HaveOccurred())

		bootstrapWorkflow := sonataFlowObject()
		workflowKey := types.NamespacedName{Name: workflowResourceName(resource), Namespace: "default"}
		Expect(k8sClient.Get(ctx, workflowKey, bootstrapWorkflow)).To(Succeed())
		bootstrapWorkflow.Object["status"] = map[string]any{
			"observedGeneration": bootstrapWorkflow.GetGeneration(),
			"address":            map[string]any{"url": "https://writer-workflow.gateway.test/" + workflowResourceName(resource)},
			"conditions": []any{map[string]any{
				"type": "Running", "status": "True",
			}},
		}
		Expect(k8sClient.Status().Update(ctx, bootstrapWorkflow)).To(Succeed())
		_, err = reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: resourceKey})
		Expect(err).NotTo(HaveOccurred())
		Expect(k8sClient.Get(ctx, workflowKey, bootstrapWorkflow)).To(Succeed())
		bootstrapWorkflow.Object["status"] = map[string]any{
			"observedGeneration": bootstrapWorkflow.GetGeneration(),
			"address":            map[string]any{"url": "https://writer-workflow.gateway.test/" + workflowResourceName(resource)},
			"conditions": []any{map[string]any{
				"type": "Running", "status": "True",
			}},
		}
		Expect(k8sClient.Status().Update(ctx, bootstrapWorkflow)).To(Succeed())
		_, err = reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: resourceKey})
		Expect(err).NotTo(HaveOccurred())

		var requests appsv1alpha1.SandboxRequestList
		Expect(k8sClient.List(ctx, &requests, client.InNamespace("default"), client.MatchingLabels{pipelineLabel: string(resource.UID)})).To(Succeed())
		Expect(requests.Items).To(HaveLen(7))
		defaultPolicyName := ""
		for index := range requests.Items {
			request := &requests.Items[index]
			Expect(request.Spec.PolicyRef).NotTo(BeNil())
			Expect(request.Spec.PolicyRef.Key).To(Equal(defaultComponentPolicyKey))
			if defaultPolicyName == "" {
				defaultPolicyName = request.Spec.PolicyRef.Name
			} else {
				Expect(request.Spec.PolicyRef.Name).To(Equal(defaultPolicyName))
			}
		}
		defaultPolicy := &corev1.ConfigMap{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: defaultPolicyName, Namespace: "default"}, defaultPolicy)).To(Succeed())
		Expect(metav1.IsControlledBy(defaultPolicy, resource)).To(BeTrue())
		Expect(defaultPolicy.Data).To(HaveKeyWithValue(defaultComponentPolicyKey, defaultComponentPolicy))
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: stale.Name, Namespace: "default"}, stale)).To(Satisfy(apierrors.IsNotFound))
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: resourceName + "-ui", Namespace: "default"}, &appsv1alpha1.SandboxRequest{})).To(Satisfy(apierrors.IsNotFound))

		uiDeployment := &appsv1.Deployment{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: resourceName + "-ui", Namespace: "default"}, uiDeployment)).To(Succeed())
		Expect(uiDeployment.Spec.Template.Spec.Containers[0].Image).To(Equal("example.invalid/ui:latest"))

		reasoning := &appsv1alpha1.SandboxRequest{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: resourceName + "-llm-reasoning", Namespace: "default"}, reasoning)).To(Succeed())
		Expect(reasoning.Spec.ApprovalMode).To(Equal("auto"))
		Expect(reasoning.Spec.Providers).To(ConsistOf(artifactProvider, "llm-provider", "embedding-provider"))
		Expect(reasoning.Spec.Env).To(HaveKeyWithValue("EMBEDDING_BASE_URL", "https://embedding.test"))
		Expect(reasoning.Spec.Env).NotTo(HaveKey("DECISION_ENGINE_URL"))
		Expect(reasoning.Spec.NetworkAccess).To(ConsistOf(
			appsv1alpha1.SandboxNetworkAccessSpec{Name: "sonataflow", Host: "writer-workflow.gateway.test", Port: 443, Protocol: "rest"},
			appsv1alpha1.SandboxNetworkAccessSpec{Name: "inference", Host: "litellm.test", Port: 4000, Protocol: "rest"},
			appsv1alpha1.SandboxNetworkAccessSpec{Name: "embedding", Host: "embedding.test", Port: 443, Protocol: "rest"},
		))

		fhir := &appsv1alpha1.SandboxRequest{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: resourceName + "-fhir-server", Namespace: "default"}, fhir)).To(Succeed())
		Expect(fhir.Spec.Providers).To(ConsistOf(artifactProvider, "fhir-provider"))
		Expect(fhir.Spec.Env).To(HaveKeyWithValue("FHIR_SERVER_URL", "https://fhir.test/R4"))
		Expect(fhir.Spec.Env).To(HaveKeyWithValue("ACP_REVIEWER_DISPLAY", "Clinician"))
		Expect(fhir.Spec.NetworkAccess).To(ContainElement(appsv1alpha1.SandboxNetworkAccessSpec{
			Name: "fhir-target", Host: "fhir.test", Port: 443, Protocol: "rest",
		}))

		decisionService := &appsv1alpha1.SandboxRequest{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: resourceName + "-decision-service", Namespace: "default"}, decisionService)).To(Succeed())
		Expect(decisionService.Spec.Command).To(Equal([]string{"java", "-jar", "/app/quarkus-run.jar"}))
		Expect(decisionService.Spec.Services).To(Equal([]appsv1alpha1.SandboxServiceSpec{{Name: "http", TargetPort: 8081}}))
		Expect(decisionService.Spec.Env).To(HaveKeyWithValue("JAVA_OPTS_APPEND", "-Xmx256m"))

		updated := &appsv1alpha1.CarePlanWriter{}
		Expect(k8sClient.Get(ctx, resourceKey, updated)).To(Succeed())
		Expect(updated.Status.ObservedGeneration).To(Equal(updated.Generation))
		Expect(meta.IsStatusConditionTrue(updated.Status.Conditions, "Accepted")).To(BeTrue())
		Expect(meta.IsStatusConditionTrue(updated.Status.Conditions, readyCondition)).To(BeFalse())

		for index := range requests.Items {
			request := &requests.Items[index]
			request.Status.ObservedGeneration = request.Generation
			request.Status.Services = []appsv1alpha1.SandboxServiceStatus{{
				Name: "http", TargetPort: request.Spec.Services[0].TargetPort, URL: "https://" + request.Labels[componentLabel] + ".gateway.test",
			}}
			meta.SetStatusCondition(&request.Status.Conditions, metav1.Condition{
				Type: readyCondition, Status: metav1.ConditionTrue, Reason: "Ready", ObservedGeneration: request.Generation,
			})
			Expect(k8sClient.Status().Update(ctx, request)).To(Succeed())
		}
		_, err = reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: resourceKey})
		Expect(err).NotTo(HaveOccurred())

		workflow := sonataFlowObject()
		workflowKey = types.NamespacedName{Name: workflowResourceName(resource), Namespace: "default"}
		Expect(k8sClient.Get(ctx, workflowKey, workflow)).To(Succeed())
		workflow.Object["status"] = map[string]any{
			"observedGeneration": workflow.GetGeneration(),
			"address":            map[string]any{"url": "https://writer-workflow.gateway.test/" + workflowResourceName(resource)},
			"conditions": []any{map[string]any{
				"type": "Running", "status": "True",
			}},
		}
		Expect(k8sClient.Status().Update(ctx, workflow)).To(Succeed())
		_, err = reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: resourceKey})
		Expect(err).NotTo(HaveOccurred())

		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: resourceName + "-llm-reasoning", Namespace: "default"}, reasoning)).To(Succeed())
		Expect(reasoning.Spec.Env).To(HaveKeyWithValue("DECISION_ENGINE_URL", "https://decision-engine.gateway.test"))
		Expect(reasoning.Spec.NetworkAccess).To(ContainElement(appsv1alpha1.SandboxNetworkAccessSpec{
			Name: "decision-engine", Host: "decision-engine.gateway.test", Port: 443, Protocol: "rest",
		}))
		bff := &appsv1.Deployment{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: resourceName + "-bff", Namespace: "default"}, bff)).To(Succeed())
		bffEnv := bff.Spec.Template.Spec.Containers[0].Env
		Expect(bffEnv).To(ContainElement(corev1.EnvVar{Name: "SONATAFLOW_URL", Value: "https://writer-workflow.gateway.test"}))
		Expect(bffEnv).To(ContainElement(corev1.EnvVar{Name: "SONATAFLOW_WORKFLOW_NAME", Value: workflowResourceName(resource)}))
		for _, peer := range []struct{ name, value string }{{"LLM_REASONING_URL", "https://llm-reasoning.gateway.test"}, {"DECISION_ENGINE_URL", "https://decision-engine.gateway.test"}, {"FHIR_SERVER_URL", "https://fhir-server.gateway.test"}} {
			Expect(bffEnv).To(ContainElement(corev1.EnvVar{Name: peer.name, Value: peer.value}))
		}
		Expect(k8sClient.Get(ctx, workflowKey, workflow)).To(Succeed())
		workflowJSON, err := workflow.MarshalJSON()
		Expect(err).NotTo(HaveOccurred())
		Expect(string(workflowJSON)).To(ContainSubstring("https://patient-data.gateway.test/api/v1/scan-async"))
		for _, path := range []string{
			"wait-scan", "wait-resolve", "wait-execute-dmn", "wait-retrieve", "wait-compose", "wait-generate", "wait-review",
		} {
			Expect(string(workflowJSON)).To(ContainSubstring("https://writer-workflow.gateway.test/" + path))
			Expect(string(workflowJSON)).NotTo(ContainSubstring("https://writer-workflow.gateway.test/" + workflowResourceName(resource) + "/" + path))
		}
		props := &corev1.ConfigMap{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: workflowResourceName(resource) + "-props", Namespace: "default"}, props)).To(Succeed())
		for channel, path := range map[string]string{
			"scan-done": "wait-scan", "resolve-done": "wait-resolve", "execute-dmn-done": "wait-execute-dmn",
			"retrieve-done": "wait-retrieve", "compose-done": "wait-compose", "generate-done": "wait-generate",
			"review-done": "wait-review", "careplan-reviewed": "wait-careplan-review",
		} {
			Expect(props.Data["application.properties"]).To(ContainSubstring("mp.messaging.incoming." + channel + ".path=/" + path))
		}
	})
})
