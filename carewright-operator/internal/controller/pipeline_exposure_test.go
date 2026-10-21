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

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	appsv1alpha1 "carewright.io/carewright-operator/api/v1alpha1"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

var _ = Describe("Pipeline UI exposure", func() {
	It("runs both UI and BFF as Deployments and removes the old bridge policy", func() {
		testScheme := runtime.NewScheme()
		Expect(corev1.AddToScheme(testScheme)).To(Succeed())
		Expect(appsv1.AddToScheme(testScheme)).To(Succeed())
		Expect(networkingv1.AddToScheme(testScheme)).To(Succeed())
		Expect(appsv1alpha1.AddToScheme(testScheme)).To(Succeed())

		owner := &appsv1alpha1.CPGIngester{ObjectMeta: metav1.ObjectMeta{
			Name: "example", Namespace: "default", UID: types.UID("pipeline-uid"),
		}}
		policy := &networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: "example-ui-to-bff", Namespace: "default"}}
		Expect(controllerutil.SetControllerReference(owner, policy, testScheme)).To(Succeed())

		k8sClient := fake.NewClientBuilder().WithScheme(testScheme).
			WithStatusSubresource(&appsv1.Deployment{}).
			WithObjects(owner, policy).Build()
		bff := sandboxComponent{Name: "bff", Spec: appsv1alpha1.ComponentSpec{Image: "example.invalid/bff:v1"}, Command: []string{"python", "-m", "uvicorn"}, Env: map[string]string{"SONATAFLOW_URL": "http://workflow"}}
		artifactStore := &appsv1alpha1.ArtifactStoreSpec{Credentials: &appsv1alpha1.ProviderSourceSpec{SecretRef: corev1.LocalObjectReference{Name: "artifact-creds"}}}
		bffSummary, err := reconcileBFFDeployment(context.Background(), k8sClient, testScheme, owner, bff, artifactStore)
		Expect(err).NotTo(HaveOccurred())
		Expect(bffSummary.URL).To(Equal("http://example-bff.default.svc.cluster.local"))
		Expect(bffSummary.Ready).To(BeFalse())
		uiSpec := appsv1alpha1.ComponentSpec{
			Image:    "example.invalid/ui:v1",
			ExtraEnv: map[string]string{"CUSTOM": "value", "BFF_HOST": "ignored"},
			Resources: appsv1alpha1.SandboxResources{
				CPU: "200m", Memory: "256Mi",
			},
		}

		exposure, err := reconcileUIExposure(context.Background(), k8sClient, testScheme, owner, uiSpec)
		Expect(err).NotTo(HaveOccurred())
		Expect(exposure.Created).To(BeTrue())
		Expect(exposure.Ready).To(BeFalse())
		Expect(exposure.URL).To(BeEmpty())

		deployment := &appsv1.Deployment{}
		uiKey := client.ObjectKey{Name: "example-ui", Namespace: "default"}
		Expect(k8sClient.Get(context.Background(), uiKey, deployment)).To(Succeed())
		Expect(deployment.Spec.Template.Spec.Containers).To(HaveLen(1))
		container := deployment.Spec.Template.Spec.Containers[0]
		Expect(container.Image).To(Equal("example.invalid/ui:v1"))
		Expect(container.Command).To(BeEmpty())
		Expect(container.Env).To(ConsistOf(
			corev1.EnvVar{Name: "BFF_HOST", Value: "example-bff"},
			corev1.EnvVar{Name: "CUSTOM", Value: "value"},
		))
		Expect(container.Resources.Limits.Cpu().String()).To(Equal("200m"))
		Expect(container.Resources.Limits.Memory().String()).To(Equal("256Mi"))
		Expect(metav1.IsControlledBy(deployment, owner)).To(BeTrue())

		uiService := &corev1.Service{}
		Expect(k8sClient.Get(context.Background(), uiKey, uiService)).To(Succeed())
		Expect(uiService.Spec.Selector).To(Equal(pipelineComponentSelector(owner, "ui")))
		Expect(uiService.Spec.Ports[0].TargetPort.StrVal).To(Equal("http"))

		bffService := &corev1.Service{}
		Expect(k8sClient.Get(context.Background(), client.ObjectKey{Name: "example-bff", Namespace: "default"}, bffService)).To(Succeed())
		Expect(bffService.Spec.Selector).To(Equal(pipelineComponentSelector(owner, "bff")))
		Expect(bffService.Spec.Ports[0].TargetPort.StrVal).To(Equal("http"))

		Expect(apierrors.IsNotFound(k8sClient.Get(context.Background(), client.ObjectKey{Name: "example-ui-to-bff", Namespace: "default"}, policy))).To(BeTrue())
		bffDeployment := &appsv1.Deployment{}
		Expect(k8sClient.Get(context.Background(), client.ObjectKey{Name: "example-bff", Namespace: "default"}, bffDeployment)).To(Succeed())
		bffContainer := bffDeployment.Spec.Template.Spec.Containers[0]
		Expect(bffContainer.Image).To(Equal("example.invalid/bff:v1"))
		Expect(bffContainer.Command).To(Equal([]string{"python", "-m", "uvicorn"}))
		Expect(bffContainer.Env).To(ContainElement(corev1.EnvVar{Name: "ARTIFACT_STORE_ACCESS_KEY", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "artifact-creds"}, Key: "ARTIFACT_STORE_ACCESS_KEY"}}}))

		route := routeObject()
		Expect(k8sClient.Get(context.Background(), uiKey, route)).To(Succeed())
		serviceName, _, err := unstructured.NestedString(route.Object, "spec", "to", "name")
		Expect(err).NotTo(HaveOccurred())
		Expect(serviceName).To(Equal("example-ui"))
		termination, _, err := unstructured.NestedString(route.Object, "spec", "tls", "termination")
		Expect(err).NotTo(HaveOccurred())
		Expect(termination).To(Equal("edge"))

		deployment.Status.ObservedGeneration = deployment.Generation
		deployment.Status.AvailableReplicas = 1
		Expect(k8sClient.Status().Update(context.Background(), deployment)).To(Succeed())
		bffDeployment.Status.ObservedGeneration = bffDeployment.Generation
		bffDeployment.Status.AvailableReplicas = 1
		Expect(k8sClient.Status().Update(context.Background(), bffDeployment)).To(Succeed())
		route.Object["status"] = map[string]any{
			"ingress": []any{map[string]any{"host": "example-ui.apps.test.example"}},
		}
		Expect(k8sClient.Update(context.Background(), route)).To(Succeed())

		exposure, err = reconcileUIExposure(context.Background(), k8sClient, testScheme, owner, uiSpec)
		Expect(err).NotTo(HaveOccurred())
		Expect(exposure.Ready).To(BeTrue())
		Expect(exposure.URL).To(Equal("https://example-ui.apps.test.example"))
		bffSummary, err = reconcileBFFDeployment(context.Background(), k8sClient, testScheme, owner, bff, artifactStore)
		Expect(err).NotTo(HaveOccurred())
		Expect(bffSummary.Ready).To(BeTrue())
		_, err = reconcileBFFDeployment(context.Background(), k8sClient, testScheme, owner, bff, &appsv1alpha1.ArtifactStoreSpec{CredentialsProvider: "openshell-only"})
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(ContainSubstring("cannot supply credentials"))
	})
})
