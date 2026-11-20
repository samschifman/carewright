package com.redhat.cpg;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.Map;

import jakarta.ws.rs.core.Response;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class JitDmnResourceTest {

    private final JitDmnResource resource = new JitDmnResource();

    @Test
    void validModelPassesEngineValidation() {
        Response response = resource.validate(request(validModel()));

        assertEquals(200, response.getStatus());
        Map<?, ?> body = (Map<?, ?>) response.getEntity();
        assertEquals(true, body.get("valid"));
    }

    @Test
    void feelSyntaxFailureIsReportedAsInvalid() {
        Response response = resource.validate(request(validModel().replace(">= 18", ">=")));

        assertEquals(200, response.getStatus());
        Map<?, ?> body = (Map<?, ?>) response.getEntity();
        assertEquals(false, body.get("valid"));
        assertNotNull(body.get("messages"));
        assertFalse(((java.util.List<?>) body.get("messages")).isEmpty());
        assertTrue(((java.util.List<?>) body.get("messages")).stream()
            .map(Object::toString)
            .anyMatch(message -> message.contains("FEEL") || message.contains(">=")));
    }

    @Test
    void tableGapIsReportedAsWarningNotError() {
        Response response = resource.validate(request(validModel()));

        Map<?, ?> body = (Map<?, ?>) response.getEntity();
        assertEquals(true, body.get("valid"));
        assertTrue(((java.util.List<?>) body.get("messages")).stream()
            .map(Object::toString)
            .anyMatch(message -> message.contains("WARN")
                && message.toLowerCase().contains("gap")));
    }

    @Test
    void unresolvedNameIsReportedAsInvalid() {
        Response response = resource.validate(request(validModel().replace(
            "<text>age</text>", "<text>unknownAge</text>")));

        assertEquals(200, response.getStatus());
        Map<?, ?> body = (Map<?, ?>) response.getEntity();
        assertEquals(false, body.get("valid"));
        assertTrue(((java.util.List<?>) body.get("messages")).stream()
            .map(Object::toString)
            .anyMatch(message -> message.contains("unknownAge") || message.contains("not found")));
    }

    @Test
    void malformedBase64IsRejected() {
        Response response = resource.validate(new JitDmnResource.ValidationRequest() {{
            dmn_xml_base64 = "not-base64";
        }});

        assertEquals(400, response.getStatus());
    }

    @Test
    void compileFailureUsesStructuredUnprocessableEntityResponse() {
        JitDmnResource.JitRequest request = new JitDmnResource.JitRequest();
        request.dmn_xml_base64 = Base64.getEncoder().encodeToString(
            "<definitions xmlns=\"https://www.omg.org/spec/DMN/20211108/MODEL/\">"
                .getBytes(StandardCharsets.UTF_8));
        request.inputs = Map.of("age", 21);

        Response response = resource.evaluate(request);

        assertEquals(422, response.getStatus());
        Map<?, ?> body = (Map<?, ?>) response.getEntity();
        assertEquals("DMN compilation errors", body.get("error"));
        assertFalse(((java.util.List<?>) body.get("messages")).isEmpty());
    }

    @Test
    void malformedBase64OnEvaluateIsBadRequest() {
        JitDmnResource.JitRequest request = new JitDmnResource.JitRequest();
        request.dmn_xml_base64 = "not-base64";
        request.inputs = Map.of();

        assertEquals(400, resource.evaluate(request).getStatus());
        assertEquals(400, resource.evaluate(null).getStatus());
    }

    @ParameterizedTest
    @ValueSource(strings = {"Java", "PMML"})
    void externalFunctionKindsAreRejectedBeforeCompilation(String kind) {
        String bkm = """
            <businessKnowledgeModel id="unsafe_bkm" name="External function">
              <encapsulatedLogic id="unsafe_function" kind="%s">
                <formalParameter name="x" typeRef="string"/>
                <context>
                  <contextEntry><variable name="class"/>
                    <literalExpression><text>"java.lang.System"</text></literalExpression>
                  </contextEntry>
                  <contextEntry><variable name="method signature"/>
                    <literalExpression><text>"getenv(java.lang.String)"</text></literalExpression>
                  </contextEntry>
                </context>
              </encapsulatedLogic>
            </businessKnowledgeModel>
            """.formatted(kind);
        assertUnsupported(validModel().replace("</definitions>", bkm + "</definitions>"),
            "functionDefinition kind=" + kind.toUpperCase());
    }

    @Test
    void feelExternalFunctionIsRejectedOnBothEndpoints() {
        String xml = literalModel("""
            function(x) external { java: { class: "java.lang.System",
            method signature: "getenv(java.lang.String)" } }
            """);
        assertUnsupported(xml, "external function in FEEL");
    }

    @Test
    void nestedJavaFunctionIsRejected() {
        String xml = literalModel("1").replace(
            "<literalExpression><text>1</text></literalExpression>", """
            <context><contextEntry><variable name="nested"/>
              <functionDefinition id="nested_java" kind="Java">
                <literalExpression><text>1</text></literalExpression>
              </functionDefinition>
            </contextEntry></context>
            """);
        assertUnsupported(xml, "functionDefinition kind=JAVA");
    }

    @Test
    void pmmlImportIsRejectedCaseInsensitively() {
        String xml = validModel().replace("<decision id=", """
            <import id="pmml_import" name="Scoring" namespace="urn:test:scoring"
                    importType="application/pMmL+xml"/>
            <decision id=""");
        assertUnsupported(xml, "PMML import");
    }

    @Test
    void decodedTableCellCannotHideExternalKeywordFromTextScan() {
        String xml = validModel().replace("<![CDATA[\"adult\"]]>", "function(x) &#101;xternal &#123; java: {} }");
        assertUnsupported(xml, "external function in FEEL at output_entry_1");
    }

    @Test
    void externalWordInDescriptionIsAllowed() {
        String xml = validModel().replace("<decision id=", """
            <description>Data obtained from external systems.</description>
            <decision id=""");
        assertEquals(200, resource.validate(request(xml)).getStatus());
        assertEquals(200, resource.evaluate(evaluationRequest(xml)).getStatus());
    }

    @Test
    void ordinaryFeelFunctionIsAllowed() {
        assertTrue(DmnSafetyGuard.findUnsupported(literalModel("function(x) x + 1")).isEmpty());
    }

    private void assertUnsupported(String xml, String finding) {
        for (Response response : java.util.List.of(
                resource.validate(request(xml)), resource.evaluate(evaluationRequest(xml)))) {
            assertEquals(400, response.getStatus());
            Map<?, ?> body = (Map<?, ?>) response.getEntity();
            assertEquals("unsupported-dmn-feature", body.get("error"));
            assertTrue(((java.util.List<?>) body.get("messages")).stream()
                .anyMatch(message -> message.toString().contains(finding)), body.toString());
        }
    }

    private static JitDmnResource.JitRequest evaluationRequest(String xml) {
        JitDmnResource.JitRequest request = new JitDmnResource.JitRequest();
        request.dmn_xml_base64 = Base64.getEncoder().encodeToString(xml.getBytes(StandardCharsets.UTF_8));
        request.inputs = Map.of("age", 21);
        return request;
    }

    private static String literalModel(String text) {
        return """
            <definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
                         id="literal" name="Literal" namespace="urn:test:literal">
              <decision id="decision" name="Result">
                <variable name="Result"/>
                <literalExpression><text>%s</text></literalExpression>
              </decision>
            </definitions>
            """.formatted(text);
    }

    private static JitDmnResource.ValidationRequest request(String xml) {
        JitDmnResource.ValidationRequest request = new JitDmnResource.ValidationRequest();
        request.dmn_xml_base64 = Base64.getEncoder().encodeToString(
            xml.getBytes(StandardCharsets.UTF_8));
        return request;
    }

    private static String validModel() {
        return """
            <?xml version="1.0" encoding="UTF-8"?>
            <definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
                         id="definitions_1" name="Simple" namespace="https://example.test/simple">
              <decision id="decision_1" name="Adult status">
                <variable id="variable_1" name="Adult status" typeRef="string"/>
                <informationRequirement>
                  <requiredInput href="#inputData_1"/>
                </informationRequirement>
                <decisionTable id="table_1" hitPolicy="UNIQUE">
                  <input id="input_1">
                    <inputExpression id="expression_1" typeRef="number"><text>age</text></inputExpression>
                  </input>
                  <output id="output_1" name="status" typeRef="string"/>
                  <rule id="rule_1">
                    <inputEntry id="entry_1"><text><![CDATA[>= 18]]></text></inputEntry>
                    <outputEntry id="output_entry_1"><text><![CDATA["adult"]]></text></outputEntry>
                  </rule>
                </decisionTable>
              </decision>
              <inputData id="inputData_1" name="age">
                <variable id="input_variable_1" name="age" typeRef="number"/>
              </inputData>
            </definitions>
            """;
    }
}
