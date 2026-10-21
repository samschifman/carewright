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

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const openshellSandboxIDLabel = "openshell.ai/sandbox-id"

// kubernetesSandboxByID resolves an OpenShell sandbox ID to its Kubernetes
// Sandbox resource. The gateway's logical sandbox name is not necessarily the
// Kubernetes resource name (shared workspaces prefix it with the workspace).
// A missing ID or resource means the Sandbox has not appeared yet.
//
//nolint:unparam // Namespace must remain explicit so this lookup is reusable across namespaces.
func kubernetesSandboxByID(
	ctx context.Context,
	k8sClient client.Client,
	namespace string,
	sandboxID string,
) (*unstructured.Unstructured, error) {
	if sandboxID == "" {
		return nil, nil
	}

	sandboxes := &unstructured.UnstructuredList{}
	sandboxes.SetGroupVersionKind(schema.GroupVersionKind{Group: sandboxGVK.Group, Version: sandboxGVK.Version, Kind: "SandboxList"})
	if err := k8sClient.List(ctx, sandboxes,
		client.InNamespace(namespace),
		client.MatchingLabels{openshellSandboxIDLabel: sandboxID},
	); err != nil {
		return nil, fmt.Errorf("list Kubernetes Sandboxes for OpenShell ID %q: %w", sandboxID, err)
	}

	switch len(sandboxes.Items) {
	case 0:
		return nil, nil
	case 1:
		return &sandboxes.Items[0], nil
	default:
		return nil, fmt.Errorf("multiple Kubernetes Sandboxes in namespace %q have OpenShell ID %q", namespace, sandboxID)
	}
}
