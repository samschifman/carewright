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

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const defaultComponentPolicyKey = "policy.yaml"

// Match the filesystem grants in carewright's existing OpenShell deployment
// policies. Component-specific network access is merged by the SandboxRequest
// controller, and an explicit component policyRef takes precedence over this.
const defaultComponentPolicy = `version: 1
filesystem_policy:
  include_workdir: true
  read_only: [/usr, /lib, /lib64, /etc, /opt, /bin, /sbin]
  read_write: [/sandbox, /tmp, /app, /dev, /run, /var, /home]
`

func reconcileDefaultComponentPolicy(
	ctx context.Context,
	k8sClient client.Client,
	scheme *runtime.Scheme,
	owner client.Object,
	components []sandboxComponent,
) (*corev1.ConfigMapKeySelector, error) {
	needsDefault := false
	for _, component := range components {
		if component.Spec.PolicyRef == nil {
			needsDefault = true
			break
		}
	}
	if !needsDefault {
		return nil, nil
	}

	name := componentRequestName(owner.GetName(), "sandbox-policy-"+shortHash(string(owner.GetUID())))
	policy := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: owner.GetNamespace()}}
	_, err := controllerutil.CreateOrUpdate(ctx, k8sClient, policy, func() error {
		if policy.ResourceVersion != "" && !metav1.IsControlledBy(policy, owner) {
			return fmt.Errorf("ConfigMap %s already exists and is not controlled by this pipeline", name)
		}
		policy.Labels = maps.Clone(policy.Labels)
		if policy.Labels == nil {
			policy.Labels = map[string]string{}
		}
		policy.Labels[managedByLabel] = managedByValue
		policy.Labels[pipelineLabel] = string(owner.GetUID())
		policy.Data = map[string]string{defaultComponentPolicyKey: defaultComponentPolicy}
		return controllerutil.SetControllerReference(owner, policy, scheme)
	})
	if err != nil {
		return nil, fmt.Errorf("reconcile default sandbox policy ConfigMap %s: %w", name, err)
	}
	return &corev1.ConfigMapKeySelector{
		LocalObjectReference: corev1.LocalObjectReference{Name: name},
		Key:                  defaultComponentPolicyKey,
	}, nil
}
