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

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

var _ = Describe("Kubernetes Sandbox lookup", func() {
	newSandbox := func(namespace, name, sandboxID string) *unstructured.Unstructured {
		sandbox := sandboxObject()
		sandbox.SetNamespace(namespace)
		sandbox.SetName(name)
		sandbox.SetLabels(map[string]string{openshellSandboxIDLabel: sandboxID})
		return sandbox
	}

	It("resolves the actual Kubernetes name using the OpenShell ID and namespace", func() {
		matching := newSandbox("carewright", "default--cpg-ingester-ui", "sandbox-123")
		otherID := newSandbox("carewright", "default--other-ui", "sandbox-456")
		otherNamespace := newSandbox("elsewhere", "default--other-ui", "sandbox-123")
		k8sClient := fake.NewClientBuilder().WithScheme(runtime.NewScheme()).
			WithObjects(matching, otherID, otherNamespace).Build()

		found, err := kubernetesSandboxByID(context.Background(), k8sClient, "carewright", "sandbox-123")
		Expect(err).NotTo(HaveOccurred())
		Expect(found).NotTo(BeNil())
		Expect(found.GetName()).To(Equal("default--cpg-ingester-ui"))
	})

	It("returns no resource while the ID or Sandbox is unavailable", func() {
		k8sClient := fake.NewClientBuilder().WithScheme(runtime.NewScheme()).Build()

		found, err := kubernetesSandboxByID(context.Background(), k8sClient, "carewright", "")
		Expect(err).NotTo(HaveOccurred())
		Expect(found).To(BeNil())

		found, err = kubernetesSandboxByID(context.Background(), k8sClient, "carewright", "sandbox-123")
		Expect(err).NotTo(HaveOccurred())
		Expect(found).To(BeNil())
	})

	It("rejects ambiguous IDs rather than selecting an arbitrary Sandbox", func() {
		first := newSandbox("carewright", "default--first-ui", "sandbox-123")
		second := newSandbox("carewright", "default--second-ui", "sandbox-123")
		k8sClient := fake.NewClientBuilder().WithScheme(runtime.NewScheme()).
			WithObjects(first, second).Build()

		found, err := kubernetesSandboxByID(context.Background(), k8sClient, "carewright", "sandbox-123")
		Expect(found).To(BeNil())
		Expect(err).To(MatchError(ContainSubstring("multiple Kubernetes Sandboxes")))
	})
})
