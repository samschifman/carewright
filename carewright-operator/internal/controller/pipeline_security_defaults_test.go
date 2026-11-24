package controller

import (
	"os"
	"path/filepath"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"sigs.k8s.io/yaml"
)

var _ = Describe("pipeline security API defaults", func() {
	It("enables omitted options and preserves independent explicit opt-outs", func() {
		cases := []struct {
			name           string
			security       map[string]interface{}
			managed, files bool
		}{
			{"omitted", nil, true, true},
			{"empty", map[string]interface{}{}, true, true},
			{"external-secrets", map[string]interface{}{"managedSecrets": false}, false, true},
			{"external-delivery", map[string]interface{}{"localCredentialFiles": false}, true, false},
			{"disabled", map[string]interface{}{"managedSecrets": false, "localCredentialFiles": false}, false, false},
		}
		for _, kind := range []string{"careplanwriter", "cpgingester"} {
			data, err := os.ReadFile(filepath.Join("..", "..", "config", "samples", "apps_v1alpha1_"+kind+".yaml"))
			Expect(err).NotTo(HaveOccurred())
			for _, test := range cases {
				object := &unstructured.Unstructured{}
				Expect(yaml.Unmarshal(data, &object.Object)).To(Succeed())
				object.SetName("security-" + kind + "-" + test.name)
				object.SetNamespace("default")
				if test.security == nil {
					unstructured.RemoveNestedField(object.Object, "spec", "security")
				} else {
					Expect(unstructured.SetNestedMap(object.Object, test.security, "spec", "security")).To(Succeed())
				}
				Expect(k8sClient.Create(ctx, object)).To(Succeed())
				managed, found, err := unstructured.NestedBool(object.Object, "spec", "security", "managedSecrets")
				Expect(err).NotTo(HaveOccurred())
				Expect(found).To(BeTrue())
				Expect(managed).To(Equal(test.managed))
				files, found, err := unstructured.NestedBool(object.Object, "spec", "security", "localCredentialFiles")
				Expect(err).NotTo(HaveOccurred())
				Expect(found).To(BeTrue())
				Expect(files).To(Equal(test.files))
				Expect(k8sClient.Delete(ctx, object)).To(Succeed())
			}
		}
	})
})
