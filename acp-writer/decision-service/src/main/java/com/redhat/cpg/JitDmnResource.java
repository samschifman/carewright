package com.redhat.cpg;

import java.io.ByteArrayInputStream;
import java.io.StringReader;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import jakarta.ws.rs.Consumes;
import jakarta.ws.rs.POST;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;

import org.drools.io.InputStreamResource;
import org.kie.dmn.api.core.DMNContext;
import org.kie.dmn.api.core.DMNDecisionResult;
import org.kie.dmn.api.core.DMNModel;
import org.kie.dmn.api.core.DMNResult;
import org.kie.dmn.api.core.DMNRuntime;
import org.kie.dmn.core.internal.utils.DMNRuntimeBuilder;
import org.kie.dmn.api.core.DMNMessage;
import org.kie.dmn.validation.DMNValidator;
import org.kie.dmn.validation.DMNValidatorFactory;

@Path("/jit/dmn")
public class JitDmnResource {

    public static class JitRequest {
        public String dmn_xml_base64;
        public Map<String, Object> inputs;
    }

    public static class ValidationRequest {
        public String dmn_xml_base64;
    }

    /**
     * Validate a DMN document using the same KIE validator used by the engine.
     * Schema validation remains the ingester's responsibility; this endpoint
     * checks the model, compilation, and decision-table analysis layers.
     */
    @POST
    @Path("/validate")
    @Consumes(MediaType.APPLICATION_JSON)
    @Produces(MediaType.APPLICATION_JSON)
    public Response validate(ValidationRequest request) {
        if (request == null || request.dmn_xml_base64 == null) {
            return Response.status(400)
                .entity(Map.of("error", "dmn_xml_base64 is required"))
                .build();
        }

        final String dmnXml;
        try {
            dmnXml = new String(
                Base64.getDecoder().decode(request.dmn_xml_base64),
                StandardCharsets.UTF_8);
        } catch (IllegalArgumentException e) {
            return Response.status(400)
                .entity(Map.of("error", "dmn_xml_base64 is not valid Base64"))
                .build();
        }

        List<String> unsupported = DmnSafetyGuard.findUnsupported(dmnXml);
        if (unsupported.equals(List.of("unparseable DMN"))) {
            return Response.status(422)
                .entity(engineError("DMN compilation errors", "unparseable DMN"))
                .build();
        }
        if (!unsupported.isEmpty()) {
            return Response.status(400)
                .entity(Map.of("error", "unsupported-dmn-feature", "messages", unsupported))
                .build();
        }

        try {
            DMNValidator validator = DMNValidatorFactory.newValidator();
            List<DMNMessage> messages = validator.validate(
                new StringReader(dmnXml),
                DMNValidator.Validation.VALIDATE_MODEL,
                DMNValidator.Validation.VALIDATE_COMPILATION,
                DMNValidator.Validation.ANALYZE_DECISION_TABLE);

            List<Map<String, Object>> responseMessages = messages.stream()
                .map(JitDmnResource::validationMessage)
                .toList();
            boolean valid = messages.stream()
                .noneMatch(message -> message.getSeverity() == DMNMessage.Severity.ERROR);

            return Response.ok(Map.of(
                "valid", valid,
                "messages", responseMessages)).build();
        } catch (Exception e) {
            return Response.status(500)
                .entity(Map.of("error", "DMN validation failed: " + e.getMessage()))
                .build();
        }
    }

    private static Map<String, Object> validationMessage(DMNMessage message) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("severity", message.getSeverity().name());
        result.put("text", message.getMessage());
        if (message.getSourceId() != null) {
            result.put("source_id", message.getSourceId());
        }
        return result;
    }

    private static Map<String, Object> engineError(String error, String text) {
        return Map.of(
            "error", error,
            "messages", List.of(Map.of(
                "severity", DMNMessage.Severity.ERROR.name(),
                "text", text == null ? error : text)));
    }

    @POST
    @Consumes(MediaType.APPLICATION_JSON)
    @Produces(MediaType.APPLICATION_JSON)
    public Response evaluate(JitRequest request) {
        if (request == null || request.dmn_xml_base64 == null || request.inputs == null) {
            return Response.status(400)
                .entity(Map.of("error", "dmn_xml_base64 and inputs are required"))
                .build();
        }

        final byte[] dmnBytes;
        try {
            dmnBytes = Base64.getDecoder().decode(request.dmn_xml_base64);
        } catch (IllegalArgumentException e) {
            return Response.status(400)
                .entity(Map.of("error", "dmn_xml_base64 is not valid Base64"))
                .build();
        }

        List<String> unsupported = DmnSafetyGuard.findUnsupported(
            new String(dmnBytes, StandardCharsets.UTF_8));
        if (unsupported.equals(List.of("unparseable DMN"))) {
            return Response.status(422)
                .entity(engineError("DMN compilation errors", "unparseable DMN"))
                .build();
        }
        if (!unsupported.isEmpty()) {
            return Response.status(400)
                .entity(Map.of("error", "unsupported-dmn-feature", "messages", unsupported))
                .build();
        }

        try {
            var resource = new InputStreamResource(
                new ByteArrayInputStream(dmnBytes));

            DMNRuntime runtime;
            try {
                runtime = DMNRuntimeBuilder.fromDefaults()
                    .buildConfiguration()
                    .fromResources(java.util.Collections.singletonList(resource))
                    .getOrElseThrow(e -> new RuntimeException("Failed to build DMN runtime: " + e));
            } catch (Exception e) {
                return Response.status(422)
                    .entity(engineError("DMN compilation errors", e.getMessage()))
                    .build();
            }

            var models = runtime.getModels();
            if (models.isEmpty()) {
                return Response.status(400)
                    .entity(Map.of("error", "No DMN models found in the provided XML"))
                    .build();
            }

            DMNModel model = models.get(0);

            DMNContext context = runtime.newContext();
            for (Map.Entry<String, Object> entry : request.inputs.entrySet()) {
                context.set(entry.getKey(), entry.getValue());
            }

            DMNResult result = runtime.evaluateAll(model, context);

            if (result.hasErrors()) {
                return Response.status(422)
                    .entity(Map.of(
                        "error", "DMN evaluation errors",
                        "messages", result.getMessages().stream()
                            .map(JitDmnResource::validationMessage)
                            .toList()))
                    .build();
            }

            Map<String, Object> outputs = new LinkedHashMap<>();
            for (DMNDecisionResult dr : result.getDecisionResults()) {
                outputs.put(dr.getDecisionName(), dr.getResult());
            }

            return Response.ok(outputs).build();

        } catch (Exception e) {
            return Response.status(500)
                .entity(Map.of("error", "DMN evaluation failed: " + e.getMessage()))
                .build();
        }
    }
}
