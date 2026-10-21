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
	meta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var _ = Describe("CPGIngester Controller", func() {
	const resourceName = "cpgingester-controller-test"
	resourceKey := types.NamespacedName{Name: resourceName, Namespace: "default"}

	AfterEach(func() {
		var requests appsv1alpha1.SandboxRequestList
		Expect(k8sClient.List(ctx, &requests, client.InNamespace("default"))).To(Succeed())
		for index := range requests.Items {
			if requests.Items[index].Labels[pipelineLabel] != "" {
				Expect(k8sClient.Delete(ctx, &requests.Items[index])).To(Succeed())
			}
		}
		resource := &appsv1alpha1.CPGIngester{}
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

	It("creates, updates, and reports readiness for component SandboxRequests", func() {
		image := func(name string) appsv1alpha1.ComponentSpec {
			return appsv1alpha1.ComponentSpec{Image: "example.invalid/" + name + ":latest"}
		}
		resource := &appsv1alpha1.CPGIngester{
			ObjectMeta: metav1.ObjectMeta{Name: resourceName, Namespace: "default"},
			Spec: appsv1alpha1.CPGIngesterSpec{
				Sandbox: appsv1alpha1.PipelineSandboxSpec{
					Gateway:   appsv1alpha1.SandboxGatewaySpec{Endpoint: "http://openshell.test:8080", Insecure: true},
					Workspace: "pipelines",
				},
				ArtifactStore: &appsv1alpha1.ArtifactStoreSpec{
					URL:            "http://minio.test:9000",
					ArtifactBucket: "artifacts",
					Credentials:    &appsv1alpha1.ProviderSourceSpec{Type: "carewright-minio", SecretRef: corev1.LocalObjectReference{Name: "artifact-secret"}},
				},
				Observability: &appsv1alpha1.ObservabilitySpec{MLflowTrackingURI: "http://mlflow.test:5000"},
				LLM: &appsv1alpha1.LLMConfigSpec{
					URL:                   "http://litellm.test:4000",
					Model:                 "test-model",
					CredentialsProvider:   "llm-provider",
					RequestTimeoutSeconds: 30,
				},
				Ingestion: appsv1alpha1.CPGIngestionComponentSpec{ComponentSpec: image("ingestion"), OCREnabled: true},
				LLMAnalysis: appsv1alpha1.CPGLLMAnalysisComponentSpec{
					PythonComponentSpec:            appsv1alpha1.PythonComponentSpec{ComponentSpec: image("llm"), PythonPath: "/app/src"},
					FigureInterpretationEnabled:    true,
					FigureInterpretationMaxFigures: 12,
				},
				Assembly: appsv1alpha1.PythonComponentSpec{
					ComponentSpec: appsv1alpha1.ComponentSpec{
						Image:          "example.invalid/assembly:v1",
						Command:        []string{"custom-assembly"},
						PolicyRef:      &corev1.ConfigMapKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "assembly-policy"}, Key: "policy.yaml"},
						Resources:      appsv1alpha1.SandboxResources{CPU: "200m", Memory: "512Mi"},
						ExtraProviders: []string{"extra-provider", "minio-provider"},
						ExtraEnv:       map[string]string{"CUSTOM": "value", "PYTHONPATH": "ignored"},
					},
					PythonPath: "/app/src",
				},
				Delivery: appsv1alpha1.PythonComponentSpec{ComponentSpec: image("delivery")},
				BFF:      appsv1alpha1.PythonComponentSpec{ComponentSpec: image("bff")},
				UI:       image("ui"),
			},
		}
		Expect(k8sClient.Create(ctx, resource)).To(Succeed())
		artifactProvider, _ := managedProvider(resource, "artifact", "", resource.Spec.ArtifactStore.Credentials)

		reconciler := &CPGIngesterReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
		_, err := reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: resourceKey})
		Expect(err).NotTo(HaveOccurred())

		bootstrapWorkflow := sonataFlowObject()
		workflowKey := types.NamespacedName{Name: workflowResourceName(resource), Namespace: "default"}
		Expect(workflowKey.Name).NotTo(ContainSubstring("-"))
		Expect(k8sClient.Get(ctx, workflowKey, bootstrapWorkflow)).To(Succeed())
		bootstrapWorkflow.Object["status"] = map[string]any{
			"observedGeneration": bootstrapWorkflow.GetGeneration(),
			"address":            map[string]any{"url": "https://workflow.gateway.test/callback"},
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
			"address":            map[string]any{"url": "https://workflow.gateway.test/callback"},
			"conditions": []any{map[string]any{
				"type": "Running", "status": "True",
			}},
		}
		Expect(k8sClient.Status().Update(ctx, bootstrapWorkflow)).To(Succeed())
		_, err = reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: resourceKey})
		Expect(err).NotTo(HaveOccurred())

		var requests appsv1alpha1.SandboxRequestList
		Expect(k8sClient.List(ctx, &requests, client.InNamespace("default"), client.MatchingLabels{pipelineLabel: string(resource.UID)})).To(Succeed())
		Expect(requests.Items).To(HaveLen(4))
		defaultPolicyName := ""
		for index := range requests.Items {
			request := &requests.Items[index]
			if request.Labels[componentLabel] == "assembly" {
				continue
			}
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
		decodedPolicy, err := decodeSandboxPolicy([]byte(defaultPolicy.Data[defaultComponentPolicyKey]))
		Expect(err).NotTo(HaveOccurred())
		Expect(decodedPolicy.Filesystem.ReadOnly).To(ContainElement("/opt"))
		Expect(decodedPolicy.Filesystem.ReadWrite).To(ContainElements("/app", "/run", "/var"))
		Expect(decodedPolicy.Filesystem.ReadWrite).NotTo(ContainElement("/opt/app-root/etc"))

		uiDeployment := &appsv1.Deployment{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: resourceName + "-ui", Namespace: "default"}, uiDeployment)).To(Succeed())
		Expect(uiDeployment.Spec.Template.Spec.Containers[0].Image).To(Equal("example.invalid/ui:latest"))

		assembly := &appsv1alpha1.SandboxRequest{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: resourceName + "-assembly", Namespace: "default"}, assembly)).To(Succeed())
		Expect(len(assembly.Spec.SandboxName)).To(BeNumerically("<=", 19))
		Expect(assembly.Spec.SandboxName).To(Equal(componentSandboxName(resourceName, "assembly")))
		Expect(assembly.Spec.Gateway.Endpoint).To(Equal("http://openshell.test:8080"))
		Expect(assembly.Spec.Workspace).To(Equal("pipelines"))
		Expect(assembly.Spec.Image).To(Equal("example.invalid/assembly:v1"))
		Expect(assembly.Spec.Command).To(Equal([]string{"custom-assembly"}))
		Expect(assembly.Spec.PolicyRef.Name).To(Equal("assembly-policy"))
		Expect(assembly.Spec.Resources.Memory).To(Equal("512Mi"))
		Expect(assembly.Spec.Services).To(Equal([]appsv1alpha1.SandboxServiceSpec{{Name: "http", TargetPort: 8080}}))
		Expect(assembly.Spec.NetworkAccess).To(ConsistOf(
			appsv1alpha1.SandboxNetworkAccessSpec{Name: "sonataflow", Host: "workflow.gateway.test", Port: 443, Protocol: "rest"},
		))
		Expect(assembly.Spec.Providers).To(ConsistOf(artifactProvider, "extra-provider", "minio-provider"))
		Expect(assembly.Spec.Env).To(HaveKeyWithValue("ARTIFACT_STORE_URL", "http://minio.test:9000"))
		Expect(assembly.Spec.Env).To(HaveKeyWithValue("MLFLOW_TRACKING_URI", "http://mlflow.test:5000"))
		Expect(assembly.Spec.Env).To(HaveKeyWithValue("PYTHONPATH", "/app/src"))
		Expect(assembly.Spec.Env).To(HaveKeyWithValue("CUSTOM", "value"))
		Expect(metav1.IsControlledBy(assembly, resource)).To(BeTrue())

		analysis := &appsv1alpha1.SandboxRequest{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: resourceName + "-llm-analysis", Namespace: "default"}, analysis)).To(Succeed())
		Expect(analysis.Spec.Providers).To(ConsistOf(artifactProvider, "llm-provider"))
		Expect(analysis.Spec.Env).To(HaveKeyWithValue("LITELLM_URL", "http://litellm.test:4000"))
		Expect(analysis.Spec.Env).To(HaveKeyWithValue("FIGURE_INTERPRETATION_MAX_FIGURES", "12"))
		Expect(analysis.Spec.NetworkAccess).To(ContainElement(appsv1alpha1.SandboxNetworkAccessSpec{
			Name: "inference", Host: "litellm.test", Port: 4000, Protocol: "rest",
		}))
		bff := &appsv1.Deployment{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: resourceName + "-bff", Namespace: "default"}, bff)).To(Succeed())
		Expect(bff.Spec.Template.Spec.Containers[0].Env).To(ContainElement(corev1.EnvVar{Name: "SONATAFLOW_URL", Value: "https://workflow.gateway.test"}))
		Expect(bff.Spec.Template.Spec.Containers[0].Env).To(ContainElement(corev1.EnvVar{Name: "SONATAFLOW_WORKFLOW_NAME", Value: workflowResourceName(resource)}))

		updated := &appsv1alpha1.CPGIngester{}
		Expect(k8sClient.Get(ctx, resourceKey, updated)).To(Succeed())
		Expect(updated.Status.ObservedGeneration).To(Equal(updated.Generation))
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
		Expect(k8sClient.Get(ctx, workflowKey, workflow)).To(Succeed())
		workflow.Object["status"] = map[string]any{
			"observedGeneration": workflow.GetGeneration(),
			"address":            map[string]any{"url": "https://workflow.gateway.test/callback"},
			"conditions": []any{map[string]any{
				"type": "Running", "status": "True",
			}},
		}
		Expect(k8sClient.Status().Update(ctx, workflow)).To(Succeed())
		_, err = reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: resourceKey})
		Expect(err).NotTo(HaveOccurred())
		Expect(k8sClient.Get(ctx, workflowKey, workflow)).To(Succeed())
		workflowJSON, err := workflow.MarshalJSON()
		Expect(err).NotTo(HaveOccurred())
		Expect(string(workflowJSON)).To(ContainSubstring("https://assembly.gateway.test/api/v1/assemble"))
		for _, path := range []string{"wait-parse", "wait-analyze", "wait-generate"} {
			Expect(string(workflowJSON)).To(ContainSubstring("https://workflow.gateway.test/" + path))
			Expect(string(workflowJSON)).NotTo(ContainSubstring("https://workflow.gateway.test/callback/" + path))
		}
		props := &corev1.ConfigMap{}
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: workflowResourceName(resource) + "-props", Namespace: "default"}, props)).To(Succeed())
		for channel, path := range map[string]string{
			"parse-done": "wait-parse", "analyze-done": "wait-analyze", "generate-done": "wait-generate",
			"manifest-reviewed": "wait-manifest-review", "artifacts-reviewed": "wait-artifact-review",
		} {
			Expect(props.Data["application.properties"]).To(ContainSubstring("mp.messaging.incoming." + channel + ".path=/" + path))
		}

		Expect(k8sClient.Get(ctx, resourceKey, updated)).To(Succeed())
		Expect(meta.IsStatusConditionTrue(updated.Status.Conditions, readyCondition)).To(BeFalse())
		Expect(updated.Status.Endpoints).To(HaveKeyWithValue("assembly", "https://assembly.gateway.test"))
		Expect(updated.Status.Endpoints).To(HaveKeyWithValue("bff", bffServiceURL(resource)))
		Expect(updated.Status.Endpoints).To(HaveKeyWithValue("workflow", "https://workflow.gateway.test/callback"))

		Expect(k8sClient.List(ctx, &requests, client.InNamespace("default"), client.MatchingLabels{pipelineLabel: string(resource.UID)})).To(Succeed())
		for index := range requests.Items {
			request := &requests.Items[index]
			request.Status.ObservedGeneration = request.Generation
			meta.SetStatusCondition(&request.Status.Conditions, metav1.Condition{
				Type: readyCondition, Status: metav1.ConditionTrue, Reason: "Ready", ObservedGeneration: request.Generation,
			})
			Expect(k8sClient.Status().Update(ctx, request)).To(Succeed())
		}
		workflow.Object["status"] = map[string]any{
			"observedGeneration": workflow.GetGeneration(),
			"address":            map[string]any{"url": "https://workflow.gateway.test/callback"},
			"conditions": []any{map[string]any{
				"type": "Running", "status": "True",
			}},
		}
		Expect(k8sClient.Status().Update(ctx, workflow)).To(Succeed())
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: resourceName + "-bff", Namespace: "default"}, bff)).To(Succeed())
		bff.Status.ObservedGeneration = bff.Generation
		bff.Status.Replicas = 1
		bff.Status.ReadyReplicas = 1
		bff.Status.AvailableReplicas = 1
		Expect(k8sClient.Status().Update(ctx, bff)).To(Succeed())
		_, err = reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: resourceKey})
		Expect(err).NotTo(HaveOccurred())
		Expect(k8sClient.Get(ctx, resourceKey, updated)).To(Succeed())
		Expect(meta.IsStatusConditionTrue(updated.Status.Conditions, readyCondition)).To(BeTrue())
		Expect(updated.Status.Endpoints).To(HaveKeyWithValue("workflow", "https://workflow.gateway.test/callback"))

		updated.Spec.Assembly.Image = "example.invalid/assembly:v2"
		Expect(k8sClient.Update(ctx, updated)).To(Succeed())
		_, err = reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: resourceKey})
		Expect(err).NotTo(HaveOccurred())
		Expect(k8sClient.Get(ctx, types.NamespacedName{Name: assembly.Name, Namespace: "default"}, assembly)).To(Succeed())
		Expect(assembly.Spec.Image).To(Equal("example.invalid/assembly:v2"))
	})
})
