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
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The operator embeds component-owned workflows but omits flow.specVersion,
// which is not part of the SonataFlow CR representation. Keep the remaining
// workflow contracts in lockstep with their component sources.
func TestEmbeddedWorkflowsMatchComponentSources(t *testing.T) {
	workflows := []struct {
		name     string
		path     string
		embedded string
	}{
		{
			name:     "CPG Ingester",
			path:     filepath.Join("..", "..", "..", "cpg-ingester", "deploy", "orchestrator", "cpg-ingester-workflow.yaml"),
			embedded: cpgIngesterWorkflowYAML,
		},
		{
			name:     "Care Plan Writer",
			path:     filepath.Join("..", "..", "..", "acp-writer", "deploy", "orchestrator", "acp-writer-workflow.yaml"),
			embedded: carePlanWriterWorkflowYAML,
		},
	}

	for _, workflow := range workflows {
		t.Run(workflow.name, func(t *testing.T) {
			source, err := os.ReadFile(workflow.path)
			if err != nil {
				t.Fatal(err)
			}
			const specVersion = "    specVersion: \"0.8\"\n"
			if !strings.Contains(string(source), specVersion) {
				t.Fatalf("component workflow %s lacks the expected specVersion line", workflow.path)
			}
			want := strings.Replace(string(source), specVersion, "", 1)
			if workflow.embedded != want {
				t.Fatalf("embedded %s workflow differs from %s; sync the operator copy", workflow.name, workflow.path)
			}
		})
	}
}
