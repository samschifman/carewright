package controller

import (
	"os"
	"path/filepath"
	"slices"
	"testing"

	rbacv1 "k8s.io/api/rbac/v1"
	"sigs.k8s.io/yaml"
)

func TestManagerRoleAllowsCachedNetworkPolicyCleanup(t *testing.T) {
	manifest, err := os.ReadFile(filepath.Join("..", "..", "config", "rbac", "role.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var role rbacv1.ClusterRole
	if err := yaml.UnmarshalStrict(manifest, &role); err != nil {
		t.Fatal(err)
	}
	// reconcileBFFDeployment uses a cached Get, so its NetworkPolicy informer
	// needs list/watch even though cleanup itself only reads and deletes.
	verbs := map[string]bool{}
	for _, rule := range role.Rules {
		if slices.Contains(rule.APIGroups, "networking.k8s.io") && slices.Contains(rule.Resources, "networkpolicies") {
			for _, verb := range rule.Verbs {
				verbs[verb] = true
			}
		}
	}
	for _, verb := range []string{"get", "list", "watch", "delete"} {
		if !verbs[verb] {
			t.Errorf("manager role is missing NetworkPolicy %q permission", verb)
		}
	}
	for _, verb := range []string{"*", "create", "update", "patch"} {
		if verbs[verb] {
			t.Errorf("NetworkPolicy cleanup must not require %q permission", verb)
		}
	}
}
