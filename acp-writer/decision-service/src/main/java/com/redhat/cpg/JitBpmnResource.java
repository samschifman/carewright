package com.redhat.cpg;

import java.io.StringReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
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

import org.jbpm.bpmn2.xml.BPMNDISemanticModule;
import org.jbpm.bpmn2.xml.BPMNExtensionsSemanticModule;
import org.jbpm.bpmn2.xml.BPMNSemanticModule;
import org.jbpm.compiler.xml.XmlProcessReader;
import org.jbpm.compiler.xml.core.SemanticModules;
import org.jbpm.process.core.validation.ProcessValidationError;
import org.jbpm.ruleflow.core.validation.RuleFlowProcessValidator;
import org.kie.api.definition.process.Process;

@Path("/jit/bpmn")
public class JitBpmnResource {

    public static class ValidationRequest {
        public String bpmn_xml_base64;
    }

    @POST
    @Path("/validate")
    @Consumes(MediaType.APPLICATION_JSON)
    @Produces(MediaType.APPLICATION_JSON)
    public Response validate(ValidationRequest request) {
        if (request == null || request.bpmn_xml_base64 == null) {
            return Response.status(400)
                .entity(Map.of("error", "bpmn_xml_base64 is required"))
                .build();
        }

        final String bpmnXml;
        try {
            bpmnXml = new String(
                Base64.getDecoder().decode(request.bpmn_xml_base64),
                StandardCharsets.UTF_8);
        } catch (IllegalArgumentException e) {
            return Response.status(400)
                .entity(Map.of("error", "bpmn_xml_base64 is not valid Base64"))
                .build();
        }

        try {
            SemanticModules modules = new SemanticModules();
            modules.addSemanticModule(new BPMNSemanticModule());
            modules.addSemanticModule(new BPMNExtensionsSemanticModule());
            modules.addSemanticModule(new BPMNDISemanticModule());
            XmlProcessReader reader = new XmlProcessReader(
                modules,
                Thread.currentThread().getContextClassLoader());
            List<Process> processes = reader.read(new StringReader(bpmnXml));

            List<Map<String, Object>> messages = new ArrayList<>();
            if (processes.isEmpty()) {
                messages.add(validationMessage(
                    "ERROR", "No BPMN processes were found in the provided XML"));
            }
            for (Process process : processes) {
                ProcessValidationError[] errors =
                    RuleFlowProcessValidator.getInstance().validateProcess(process);
                for (ProcessValidationError error : errors) {
                    messages.add(validationMessage("ERROR", error.getMessage()));
                }
            }

            boolean valid = messages.stream()
                .noneMatch(message -> "ERROR".equals(message.get("severity")));
            return Response.ok(Map.of(
                "valid", valid,
                "messages", messages)).build();
        } catch (Exception e) {
            return Response.ok(Map.of(
                "valid", false,
                "messages", List.of(validationMessage(
                    "ERROR", e.getMessage() == null ? "BPMN parsing failed" : e.getMessage()))))
                .build();
        }
    }

    private static Map<String, Object> validationMessage(String severity, String text) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("severity", severity);
        result.put("text", text);
        return result;
    }
}
