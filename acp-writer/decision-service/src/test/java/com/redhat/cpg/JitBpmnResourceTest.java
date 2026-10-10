package com.redhat.cpg;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.List;
import java.util.Map;

import jakarta.ws.rs.core.Response;

import org.junit.jupiter.api.Test;

class JitBpmnResourceTest {

    private final JitBpmnResource resource = new JitBpmnResource();

    @Test
    void goldenProcessPassesEngineValidation() throws IOException {
        Response response = resource.validate(request(readBpmn("home-bp-monitoring.bpmn")));

        assertEquals(200, response.getStatus());
        Map<?, ?> body = (Map<?, ?>) response.getEntity();
        assertEquals(true, body.get("valid"));
        assertTrue(((List<?>) body.get("messages")).isEmpty());
    }

    @Test
    void engineInvalidFixturesAreRejected() throws IOException {
        for (String filename : List.of(
                "v-bad-dangling.bpmn",
                "v-bad-nocondition.bpmn",
                "v-bad-implicit-join.bpmn")) {
            Response response = resource.validate(request(readBpmn(filename)));

            assertEquals(200, response.getStatus(), filename);
            Map<?, ?> body = (Map<?, ?>) response.getEntity();
            assertEquals(false, body.get("valid"), filename);
            List<?> messages = (List<?>) body.get("messages");
            assertFalse(messages.isEmpty(), filename);
            assertTrue(messages.stream().map(item -> (Map<?, ?>) item)
                .anyMatch(message -> "ERROR".equals(message.get("severity"))), filename);
        }
    }

    @Test
    void malformedXmlIsReturnedAsAnErrorMessage() {
        Response response = resource.validate(request("<definitions"));

        assertEquals(200, response.getStatus());
        Map<?, ?> body = (Map<?, ?>) response.getEntity();
        assertEquals(false, body.get("valid"));
        List<?> messages = (List<?>) body.get("messages");
        assertEquals(1, messages.size());
        Map<?, ?> message = (Map<?, ?>) messages.get(0);
        assertEquals("ERROR", message.get("severity"));
        assertNotNull(message.get("text"));
    }

    @Test
    void malformedBase64IsRejected() {
        JitBpmnResource.ValidationRequest request = new JitBpmnResource.ValidationRequest();
        request.bpmn_xml_base64 = "not-base64";

        assertEquals(400, resource.validate(request).getStatus());
    }

    private static String readBpmn(String filename) throws IOException {
        try (InputStream input = JitBpmnResourceTest.class
                .getResourceAsStream("/bpmn/" + filename)) {
            assertNotNull(input, "missing BPMN fixture " + filename);
            return new String(input.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static JitBpmnResource.ValidationRequest request(String xml) {
        JitBpmnResource.ValidationRequest request = new JitBpmnResource.ValidationRequest();
        request.bpmn_xml_base64 = Base64.getEncoder().encodeToString(
            xml.getBytes(StandardCharsets.UTF_8));
        return request;
    }
}
