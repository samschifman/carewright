package com.redhat.cpg;

import java.io.StringReader;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Pattern;

import org.kie.dmn.backend.marshalling.v1x.DMNMarshallerFactory;
import org.kie.dmn.model.api.DMNModelInstrumentedBase;
import org.kie.dmn.model.api.Definitions;
import org.kie.dmn.model.api.FunctionDefinition;
import org.kie.dmn.model.api.FunctionKind;
import org.kie.dmn.model.api.Import;
import org.kie.dmn.model.api.LiteralExpression;
import org.kie.dmn.model.api.UnaryTests;

/** Inspects the parsed model without compiling or loading external functions. */
public final class DmnSafetyGuard {
    private static final Pattern EXTERNAL = Pattern.compile("(?s)\\bexternal\\s*\\{");

    private DmnSafetyGuard() {
    }

    public static List<String> findUnsupported(String dmnXml) {
        List<String> findings = new ArrayList<>();
        // Also cover extension constructs that the model walker does not know.
        if (EXTERNAL.matcher(dmnXml).find()) {
            findings.add("external function in FEEL at XML document");
        }

        final Definitions definitions;
        try {
            definitions = DMNMarshallerFactory.newDefaultMarshaller()
                .unmarshal(new StringReader(dmnXml));
        } catch (RuntimeException e) {
            return List.of("unparseable DMN");
        }
        if (definitions == null) {
            return List.of("unparseable DMN");
        }

        // The marshaller populates the instrumented child tree for every parsed
        // element, including nested contexts, tables, bindings and boxed FEEL.
        // Traverse only children (never parent links), once per object.
        Set<DMNModelInstrumentedBase> visited = Collections.newSetFromMap(new IdentityHashMap<>());
        ArrayDeque<DMNModelInstrumentedBase> pending = new ArrayDeque<>();
        pending.add(definitions);
        while (!pending.isEmpty()) {
            DMNModelInstrumentedBase element = pending.removeFirst();
            if (!visited.add(element)) {
                continue;
            }
            String location = element.getIdentifierString();
            if (element instanceof Import imported && imported.getImportType() != null
                    && imported.getImportType().toUpperCase(Locale.ROOT).contains("PMML")) {
                findings.add("PMML import at " + location);
            }
            if (element instanceof FunctionDefinition function && function.getKind() != FunctionKind.FEEL) {
                findings.add("functionDefinition kind=" + function.getKind() + " at " + location);
            }
            String text = element instanceof LiteralExpression literal ? literal.getText()
                : element instanceof UnaryTests tests ? tests.getText() : null;
            if (text != null && EXTERNAL.matcher(text).find()) {
                findings.add("external function in FEEL at " + location);
            }
            pending.addAll(element.getChildren());
        }
        return findings;
    }
}
