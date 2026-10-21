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
	"fmt"
	"maps"
	"slices"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	meta "k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/intstr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	appsv1alpha1 "carewright.io/carewright-operator/api/v1alpha1"
)

var (
	routeGVK   = schema.GroupVersionKind{Group: "route.openshift.io", Version: "v1", Kind: "Route"}
	sandboxGVK = schema.GroupVersionKind{Group: "agents.x-k8s.io", Version: "v1beta1", Kind: "Sandbox"}
)

type uiExposureSummary struct {
	Created bool
	Ready   bool
	URL     string
}

type bffDeploymentSummary struct {
	URL   string
	Ready bool
}

// reconcileBFFDeployment runs the BFF outside OpenShell and gives the UI and
// workflow a stable in-cluster endpoint.
func reconcileBFFDeployment(ctx context.Context, k8sClient client.Client, scheme *runtime.Scheme, owner client.Object, component sandboxComponent, artifactStore *appsv1alpha1.ArtifactStoreSpec) (bffDeploymentSummary, error) {
	name := dependencyResourceName(owner.GetName(), "bff")
	selector := pipelineComponentSelector(owner, "bff")
	resources, err := componentResourceRequirements(component.Spec.Resources)
	if err != nil {
		return bffDeploymentSummary{}, fmt.Errorf("resolve BFF resources: %w", err)
	}
	command := component.Command
	if len(component.Spec.Command) != 0 {
		command = component.Spec.Command
	}
	if artifactStore != nil && artifactStore.CredentialsProvider != "" && artifactStore.Credentials == nil {
		return bffDeploymentSummary{}, fmt.Errorf("artifactStore.credentialsProvider cannot supply credentials to a BFF Deployment; configure artifactStore.credentials.secretRef instead")
	}
	envValues := mergedEnv(component.Spec.ExtraEnv, component.Env)
	if artifactStore != nil && artifactStore.Credentials != nil {
		delete(envValues, "ARTIFACT_STORE_ACCESS_KEY")
		delete(envValues, "ARTIFACT_STORE_SECRET_KEY")
	}
	env := environmentVariables(envValues)
	if artifactStore != nil && artifactStore.Credentials != nil {
		secretName := artifactStore.Credentials.SecretRef.Name
		if secretName == "" {
			return bffDeploymentSummary{}, fmt.Errorf("BFF artifactStore.credentials.secretRef.name is required")
		}
		for _, key := range []string{"ARTIFACT_STORE_ACCESS_KEY", "ARTIFACT_STORE_SECRET_KEY"} {
			env = append(env, corev1.EnvVar{Name: key, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: secretName}, Key: key}}})
		}
	}
	deployment := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: owner.GetNamespace()}}
	_, err = controllerutil.CreateOrUpdate(ctx, k8sClient, deployment, func() error {
		if deployment.ResourceVersion != "" && !metav1.IsControlledBy(deployment, owner) {
			return fmt.Errorf("deployment %s/%s already exists and is not managed by CareWright", deployment.Namespace, deployment.Name)
		}
		setPipelineResourceLabels(deployment, owner, "bff")
		deployment.Spec.Replicas = ptrTo(int32(1))
		deployment.Spec.Selector = &metav1.LabelSelector{MatchLabels: selector}
		deployment.Spec.Template.Labels = maps.Clone(selector)
		deployment.Spec.Template.Spec.Containers = []corev1.Container{{
			Name: "bff", Image: component.Spec.Image, Command: slices.Clone(command), Env: env, Resources: resources,
			Ports:          []corev1.ContainerPort{{Name: "http", ContainerPort: 8080, Protocol: corev1.ProtocolTCP}},
			ReadinessProbe: &corev1.Probe{ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: "/health", Port: intstr.FromString("http")}}, InitialDelaySeconds: 2, PeriodSeconds: 5},
		}}
		return controllerutil.SetControllerReference(owner, deployment, scheme)
	})
	if err != nil {
		return bffDeploymentSummary{}, fmt.Errorf("reconcile BFF Deployment: %w", err)
	}
	service := &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: owner.GetNamespace()}}
	_, err = controllerutil.CreateOrUpdate(ctx, k8sClient, service, func() error {
		if service.ResourceVersion != "" && !metav1.IsControlledBy(service, owner) {
			return fmt.Errorf("service %s/%s already exists and is not managed by CareWright", service.Namespace, service.Name)
		}
		setPipelineResourceLabels(service, owner, "bff")
		service.Spec.Type = corev1.ServiceTypeClusterIP
		service.Spec.Selector = maps.Clone(selector)
		service.Spec.Ports = []corev1.ServicePort{{Name: "http", Protocol: corev1.ProtocolTCP, Port: 80, TargetPort: intstr.FromString("http")}}
		return controllerutil.SetControllerReference(owner, service, scheme)
	})
	if err != nil {
		return bffDeploymentSummary{}, fmt.Errorf("reconcile BFF Service: %w", err)
	}
	// Remove the ingress exception previously needed for the sandboxed BFF.
	policy := &networkingv1.NetworkPolicy{}
	if err := k8sClient.Get(ctx, client.ObjectKey{Name: dependencyResourceName(owner.GetName(), "ui-to-bff"), Namespace: owner.GetNamespace()}, policy); err == nil {
		if metav1.IsControlledBy(policy, owner) {
			if err := k8sClient.Delete(ctx, policy); err != nil && !apierrors.IsNotFound(err) {
				return bffDeploymentSummary{}, fmt.Errorf("delete obsolete UI-to-BFF NetworkPolicy: %w", err)
			}
		}
	} else if !apierrors.IsNotFound(err) {
		return bffDeploymentSummary{}, fmt.Errorf("get obsolete UI-to-BFF NetworkPolicy: %w", err)
	}
	return bffDeploymentSummary{URL: bffServiceURL(owner), Ready: deployment.Status.ObservedGeneration >= deployment.Generation && deployment.Status.AvailableReplicas > 0}, nil
}

func bffServiceURL(owner client.Object) string {
	return fmt.Sprintf("http://%s.%s.svc.cluster.local", dependencyResourceName(owner.GetName(), "bff"), owner.GetNamespace())
}

// reconcileUIExposure runs the browser-facing UI as a regular Kubernetes workload.
func reconcileUIExposure(
	ctx context.Context,
	k8sClient client.Client,
	scheme *runtime.Scheme,
	owner client.Object,
	spec appsv1alpha1.ComponentSpec,
) (uiExposureSummary, error) {
	name := dependencyResourceName(owner.GetName(), "ui")
	selector := pipelineComponentSelector(owner, "ui")
	bffName := dependencyResourceName(owner.GetName(), "bff")

	resources, err := componentResourceRequirements(spec.Resources)
	if err != nil {
		return uiExposureSummary{}, fmt.Errorf("resolve UI resources: %w", err)
	}
	env := maps.Clone(spec.ExtraEnv)
	if env == nil {
		env = map[string]string{}
	}
	env["BFF_HOST"] = bffName

	deployment := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: owner.GetNamespace()}}
	_, err = controllerutil.CreateOrUpdate(ctx, k8sClient, deployment, func() error {
		if deployment.ResourceVersion != "" && !metav1.IsControlledBy(deployment, owner) {
			return fmt.Errorf("deployment %s/%s already exists and is not managed by CareWright", deployment.Namespace, deployment.Name)
		}
		setPipelineResourceLabels(deployment, owner, "ui")
		deployment.Spec.Replicas = ptrTo(int32(1))
		deployment.Spec.Selector = &metav1.LabelSelector{MatchLabels: selector}
		deployment.Spec.Template.Labels = maps.Clone(selector)
		deployment.Spec.Template.Spec.Containers = []corev1.Container{{
			Name:      "ui",
			Image:     spec.Image,
			Command:   slices.Clone(spec.Command),
			Env:       environmentVariables(env),
			Resources: resources,
			Ports: []corev1.ContainerPort{{
				Name: "http", ContainerPort: 8080, Protocol: corev1.ProtocolTCP,
			}},
			ReadinessProbe: &corev1.Probe{
				ProbeHandler:        corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: "/health", Port: intstr.FromString("http")}},
				InitialDelaySeconds: 2,
				PeriodSeconds:       5,
			},
		}}
		return controllerutil.SetControllerReference(owner, deployment, scheme)
	})
	if err != nil {
		return uiExposureSummary{}, fmt.Errorf("reconcile UI Deployment: %w", err)
	}

	service := &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: owner.GetNamespace()}}
	_, err = controllerutil.CreateOrUpdate(ctx, k8sClient, service, func() error {
		if service.ResourceVersion != "" && !metav1.IsControlledBy(service, owner) {
			return fmt.Errorf("service %s/%s already exists and is not managed by CareWright", service.Namespace, service.Name)
		}
		setPipelineResourceLabels(service, owner, "ui")
		service.Spec.Type = corev1.ServiceTypeClusterIP
		service.Spec.Selector = maps.Clone(selector)
		service.Spec.Ports = []corev1.ServicePort{{
			Name: "http", Protocol: corev1.ProtocolTCP, Port: 80, TargetPort: intstr.FromString("http"),
		}}
		return controllerutil.SetControllerReference(owner, service, scheme)
	})
	if err != nil {
		return uiExposureSummary{}, fmt.Errorf("reconcile UI Service: %w", err)
	}

	route := routeObject()
	route.SetName(name)
	route.SetNamespace(owner.GetNamespace())
	_, err = controllerutil.CreateOrUpdate(ctx, k8sClient, route, func() error {
		if route.GetResourceVersion() != "" && !metav1.IsControlledBy(route, owner) {
			return fmt.Errorf("route %s/%s already exists and is not managed by CareWright", route.GetNamespace(), route.GetName())
		}
		setPipelineResourceLabels(route, owner, "ui")
		route.Object["spec"] = map[string]any{
			"to": map[string]any{
				"kind":   "Service",
				"name":   service.Name,
				"weight": int64(100),
			},
			"port": map[string]any{"targetPort": "http"},
			"tls": map[string]any{
				"termination":                   "edge",
				"insecureEdgeTerminationPolicy": "Redirect",
			},
			"wildcardPolicy": "None",
		}
		return controllerutil.SetControllerReference(owner, route, scheme)
	})
	if err != nil {
		return uiExposureSummary{}, fmt.Errorf("reconcile UI Route: %w", err)
	}

	deploymentReady := deployment.Status.ObservedGeneration >= deployment.Generation && deployment.Status.AvailableReplicas > 0
	return uiExposureSummary{
		Created: true,
		Ready:   deploymentReady,
		URL:     routeURL(route),
	}, nil
}

func pipelineComponentSelector(owner client.Object, component string) map[string]string {
	return map[string]string{
		managedByLabel: managedByValue,
		pipelineLabel:  string(owner.GetUID()),
		componentLabel: component,
	}
}

func environmentVariables(values map[string]string) []corev1.EnvVar {
	keys := slices.Sorted(maps.Keys(values))
	result := make([]corev1.EnvVar, 0, len(keys))
	for _, key := range keys {
		result = append(result, corev1.EnvVar{Name: key, Value: values[key]})
	}
	return result
}

func componentResourceRequirements(resources appsv1alpha1.SandboxResources) (corev1.ResourceRequirements, error) {
	limits := corev1.ResourceList{}
	for name, value := range map[corev1.ResourceName]string{
		corev1.ResourceCPU: resources.CPU, corev1.ResourceMemory: resources.Memory,
	} {
		if value == "" {
			continue
		}
		quantity, err := resource.ParseQuantity(value)
		if err != nil {
			return corev1.ResourceRequirements{}, fmt.Errorf("parse %s quantity %q: %w", name, value, err)
		}
		limits[name] = quantity
	}
	if resources.GPU != nil && *resources.GPU > 0 {
		limits[corev1.ResourceName("nvidia.com/gpu")] = *resource.NewQuantity(int64(*resources.GPU), resource.DecimalSI)
	}
	if len(limits) == 0 {
		limits = nil
	}
	return corev1.ResourceRequirements{Limits: limits}, nil
}

func sandboxObject() *unstructured.Unstructured {
	object := &unstructured.Unstructured{}
	object.SetGroupVersionKind(sandboxGVK)
	return object
}

func routeObject() *unstructured.Unstructured {
	object := &unstructured.Unstructured{}
	object.SetGroupVersionKind(routeGVK)
	return object
}

func setPipelineResourceLabels(object client.Object, owner client.Object, component string) {
	labels := object.GetLabels()
	if labels == nil {
		labels = map[string]string{}
	}
	labels[managedByLabel] = managedByValue
	labels[pipelineLabel] = string(owner.GetUID())
	labels[componentLabel] = component
	object.SetLabels(labels)
}

func routeURL(route *unstructured.Unstructured) string {
	ingress, _, _ := unstructured.NestedSlice(route.Object, "status", "ingress")
	for _, item := range ingress {
		entry, ok := item.(map[string]any)
		if !ok {
			continue
		}
		host, _, _ := unstructured.NestedString(entry, "host")
		if host != "" {
			return "https://" + host
		}
	}
	return ""
}

func setUIExposureCondition(conditions *[]metav1.Condition, generation int64, exposure uiExposureSummary) {
	condition := metav1.Condition{
		Type:               "Exposed",
		Status:             metav1.ConditionFalse,
		ObservedGeneration: generation,
		Reason:             "UIDeploymentPending",
		Message:            "Waiting for the UI and BFF Deployments",
	}
	if exposure.Created && exposure.URL == "" {
		condition.Reason = "RoutePending"
		condition.Message = "The UI Deployment, Service, and Route exist; waiting for OpenShift to assign a route host"
	}
	if exposure.URL != "" && !exposure.Ready {
		condition.Reason = "UIProgressing"
		condition.Message = "The UI Route exists; waiting for the UI and BFF Deployments to become ready"
	}
	if exposure.URL != "" && exposure.Ready {
		condition.Status = metav1.ConditionTrue
		condition.Reason = "RouteReady"
		condition.Message = fmt.Sprintf("The UI is available at %s", exposure.URL)
	}
	meta.SetStatusCondition(conditions, condition)
}

func ptrTo[T any](value T) *T {
	return &value
}
