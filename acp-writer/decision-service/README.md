# Decision service

The Quarkus decision service exposes KIE validation and execution endpoints for
DMN, plus validation for the supported Kogito BPMN profile. BPMN validation is
structural and engine-oriented; it does not deploy or execute the process.

## Endpoints

- `POST /jit/dmn/validate` validates a base64-encoded DMN document.
- `POST /jit/dmn` evaluates a base64-encoded DMN document with the supplied
  input map.
- `POST /jit/bpmn/validate` validates a base64-encoded BPMN document.

The BPMN request body is `{"bpmn_xml_base64":"<base64 XML>"}`. Validation
returns `{"valid":true|false,"messages":[{"severity":"ERROR|WARNING",
"text":"..."}]}`. Invalid base64 is a `400`; BPMN parse and engine validation
findings are returned as `200` with `valid:false` and error messages.

`JitBpmnResource` parses BPMN with `XmlProcessReader` and the BPMN,
`drools:`-extension, and BPMN-DI semantic modules, then validates each process
with `RuleFlowProcessValidator`. The dependency tree confirms that the KIE
10.2.0 `org.kie.kogito` artifacts resolve the APIs: `jbpm-bpmn2` contains the
three semantic modules, `jbpm-flow-builder` contains `XmlProcessReader`, and
`jbpm-flow` contains `RuleFlowProcessValidator`.

## Security

Both the DMN and BPMN `/jit` endpoints parse caller-supplied XML. They are
intended for internal-network use only. External-entity handling in the KIE
readers has not been audited.

### Unsupported DMN features

Both DMN endpoints reject Java and PMML function definitions, PMML imports,
and FEEL external functions with `400` and `error: "unsupported-dmn-feature"`.
The safety check parses and inspects the model before KIE compiles it, so these
features cannot load Java classes during validation or evaluation. Nested
expressions and decision-table cells are checked as well. A conservative text
check also rejects `external` followed by optional whitespace and `{`, even in
free text; the word `external` on its own is allowed.

## Build and test

From the repository root, run the tests with the project’s Java 17 Maven image
and build the runtime image with Podman:

```sh
podman run --rm \
  -v "$PWD/acp-writer/decision-service":/src -w /src \
  -v "$HOME/.m2":/root/.m2 \
  docker.io/library/maven:3.9-eclipse-temurin-17 mvn -q test

podman build -t cpg-decision-service:dev \
  -f acp-writer/decision-service/deploy/Containerfile \
  acp-writer/decision-service
```
