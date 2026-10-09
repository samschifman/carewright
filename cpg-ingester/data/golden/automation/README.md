# Hypertension automation goldens

The four `*.ir.json` files are the editable source artifacts. The matching
`*.bpmn` files are compiled by `shared/cpg-contracts`; each `*.template.json`
contains the validated `AutomationTemplate` contract, including the canonical
IR version, capabilities, validation rungs, and deferred invariant.

`linked_recommendation_ids` contains the literal `<placeholder-rec-id>` in these
fixtures. It is a documented placeholder, not a recommendation identifier that
can be published or registered. Replace it with the recommendation ID produced
by the corresponding ingestion run when creating deliverable templates.

From this directory, run:

```bash
python regenerate.py
python regenerate.py --check
```

The script reads the golden DMNs for their variable names and namespaces, runs
the static validation ladder against the v2 guideline, and writes the BPMN and
template JSON outputs. `kogito_checked` remains false because these artifacts
have not yet been sent through the decision-service engine validation endpoint.
The IR and BPMN DMN I/O names stay identical to the DMN variable names; generated
XML IDs encode spaces and other non-ID characters as underscores.

The files cover the home blood pressure reminder and escalation process, ACEi
laboratory follow-up, medication follow-up visit scheduling, and lifestyle
reassessment. Together they exercise every capability in catalog v1, as well
as one DMN decision task and one clinician user task.

The ACEi template is triggered by Treatment Recommendation `Action = Start
medication`. Its required `treatment_action` and `has_kidney_disease` parameters
are bound from the approved plan at synthesis. The Monitoring Plan then returns
the BMP order and a 2- or 4-week timing for medication rows; null timing routes
to the explicit `not_required` outcome. A missing BMP result notifies the
clinician directly.

The medication follow-up template is triggered by Treatment Recommendation
`Follow Up Weeks = 2` or `4`. It requests the visit and checks for the order
immediately, without waiting through the full interval. The lifestyle template
is triggered by `Follow Up Weeks = 8` or `12`; its required
`reassessment_interval` is bound from that DMN output. It sends lifestyle
education grounded in section 3.4, requests the follow-up visit, waits for the
interval, then presents home readings to a clinician. The medication follow-up
elements cite the recommendation to review the seven-day home average two to
four weeks after a medication start or change. These interval values are
instance bindings; they are not stored as patient-specific values in the
template IRs.

The artifacts passed the Kogito BPMN validator, and the ACEi BPMN was also
built in the Quarkus probe with `monitoring-plan.dmn` available as a resource.
The template JSON still reports `kogito_checked: false` because the artifacts
have not been sent through the decision-service validation endpoint.
