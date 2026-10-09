# Golden DMN corpus

These models are the hand-derived yardstick for the DMN generation benchmark.
They use DMN 1.4, CDATA for FEEL text, `definitions/@id` equal to
`decision_model_id(name)`, and a per-model target namespace beginning with
`https://redhat.com/cpg-to-acp/dmn/`. A multi-output decision uses
`variable/@typeRef="Any"`; the output columns carry the individual types.
Clinical sign-off happens in PR review. These files are the ingester's reference
corpus; they are not bundled into the decision service.

Regenerate the synthetic PDF and automation golden artifacts from the
`cpg-ingester` directory with:

```sh
python data/make_synthetic_pdf.py
python data/golden/automation/regenerate.py
```

The hypertension source document has two versions: `synthetic-hypertension-cpg.md`
is v1 and remains unchanged; `synthetic-hypertension-cpg-v2.md` adds the home
blood pressure monitoring and follow-up outreach sections used by automation
extraction. The decision-table content and name-derived DMN ids overlap, so a
fresh knowledge base for the automation slice loads v2 only; registering both
versions together is not supported.

Each row below is one DMN rule. The input and output cells are shown in table
order. `Assumption:` is deliberately literal in the final column because the
benchmark manifest uses it to exclude non-source branches from fidelity scores.

## Treatment Recommendation (`treatment-recommendation.dmn`)

| Rule id | Input cells | Output cells | Source (file:line, quoted sentence or table row) | Assumptions / notes |
|---|---|---|---|---|
| rule_1 | `>= 140, false, false` | `Start medication; Lisinopril; 10 mg daily; 4` | `synthetic-hypertension-cpg.md:85` “\| >= 140 \| No \| No \| Start medication \| Lisinopril \| 10 mg daily \| 4 \|” | Direct transcription; SBP is mmHg and the lower boundary is inclusive. |
| rule_2 | `>= 130, true, false` | `Start medication; Lisinopril; 10 mg daily; 2` | `synthetic-hypertension-cpg.md:86` “\| >= 130 \| Yes \| No \| Start medication \| Lisinopril \| 10 mg daily \| 2 \|” | Direct transcription. |
| rule_3 | `>= 130, false, true` | `Start medication; Lisinopril; 10 mg daily; 2` | `synthetic-hypertension-cpg.md:87` “\| >= 130 \| No \| Yes \| Start medication \| Lisinopril \| 10 mg daily \| 2 \|” | Direct transcription. |
| rule_4 | `>= 130, true, true` | `Start medication; Lisinopril; 10 mg daily; 2` | `synthetic-hypertension-cpg.md:88` “\| >= 130 \| Yes \| Yes \| Start medication \| Lisinopril \| 10 mg daily \| 2 \|” | Direct transcription. |
| rule_5 | `[130..139], false, false` | `Lifestyle modification only; -; -; 8` | `synthetic-hypertension-cpg.md:89` “\| 130-139 \| No \| No \| Lifestyle modification only \| — \| — \| 8 \|” | The source's inclusive integer range is represented as `[130..139]`. |
| rule_6 | `< 130, false, false` | `Lifestyle modification only; -; -; 12` | `synthetic-hypertension-cpg.md:90` “\| < 130 \| No \| No \| Lifestyle modification only \| — \| — \| 12 \|” | Direct transcription. |
| rule_7 | `< 130, true, false` | `Lifestyle modification only; -; -; 12` | `synthetic-hypertension-cpg.md:91` “\| < 130 \| Yes \| No \| Lifestyle modification only \| — \| — \| 12 \|” | Direct transcription. |
| rule_8 | `< 130, false, true` | `Lifestyle modification only; -; -; 12` | `synthetic-hypertension-cpg.md:92` “\| < 130 \| No \| Yes \| Lifestyle modification only \| — \| — \| 12 \|” | Direct transcription. |
| rule_9 | `< 130, true, true` | `Lifestyle modification only; -; -; 12` | `synthetic-hypertension-cpg.md:99` “Patients with elevated but sub-hypertensive blood pressure (SBP < 130) should focus on lifestyle modifications with follow-up in 12 weeks.” | Assumption: the source principle covers this missing comorbidity combination; the row completes the input space. |

## Monitoring Plan (`monitoring-plan.dmn`)

| Rule id | Input cells | Output cells | Source (file:line, quoted sentence or table row) | Assumptions / notes |
|---|---|---|---|---|
| rule_1 | `"Start medication", true` | `Basic Metabolic Panel; 2` | `synthetic-hypertension-cpg.md:113` “\| Start medication \| Yes \| Basic Metabolic Panel \| 2 \|” | Direct transcription. |
| rule_2 | `"Start medication", false` | `Basic Metabolic Panel; 4` | `synthetic-hypertension-cpg.md:114` “\| Start medication \| No \| Basic Metabolic Panel \| 4 \|” | Direct transcription. |
| rule_3 | `"Lifestyle modification only", true` | `-; null` | `synthetic-hypertension-cpg.md:115` “\| Lifestyle modification only \| Yes \| — \| — \|” | Direct transcription; null means no lab timing. |
| rule_4 | `"Lifestyle modification only", false` | `-; null` | `synthetic-hypertension-cpg.md:116` “\| Lifestyle modification only \| No \| — \| — \|” | Direct transcription. |

## Glycemic Treatment Decision (`diabetes-treatment.dmn`)

| Rule id | Input cells | Output cells | Source (file:line, quoted sentence or table row) | Assumptions / notes |
|---|---|---|---|---|
| rule_combination | `>= 9` | `Combination therapy` | `synthetic-diabetes-cpg.md:21` “\| >= 9 \| Combination therapy \|” | Direct transcription; HbA1c is percent. |
| rule_metformin | `< 9` | `Metformin plus lifestyle` | `synthetic-diabetes-cpg.md:22` “\| < 9 \| Metformin plus lifestyle \|” | Direct transcription. |

## Diabetes Monitoring Plan (`diabetes-monitoring.dmn`)

| Rule id | Input cells | Output cells | Source (file:line, quoted sentence or table row) | Assumptions / notes |
|---|---|---|---|---|
| rule_combination_followup | `"Combination therapy"` | `3` | `synthetic-diabetes-cpg.md:34` “\| Combination therapy \| 3 \|” | Direct transcription; months. |
| rule_metformin_followup | `"Metformin plus lifestyle"` | `6` | `synthetic-diabetes-cpg.md:35` “\| Metformin plus lifestyle \| 6 \|” | Direct transcription; months. |

## Glycemic Escalation Monitoring (`glycemic-escalation-monitoring.dmn`)

| Rule id | Input cells | Output cells | Source (file:line, quoted sentence or table row) | Assumptions / notes |
|---|---|---|---|---|
| rule_escalate_combination | `"Metformin plus lifestyle", >= 2` | `Escalate to combination therapy` | `synthetic-diabetes-cpg.md:48` “\| Metformin plus lifestyle \| >= 2 \| Escalate to combination therapy \|” | Direct transcription; extraction uses LOINC `4548-4` as an explicit implementation assumption because the CPG names HbA1c but no code. |
| rule_continue_current | `"Metformin plus lifestyle", < 2` | `Continue current plan and reassess at the next scheduled interval` | `synthetic-diabetes-cpg.md:49` “\| Metformin plus lifestyle \| < 2 \| Continue current plan and reassess at the next scheduled interval \|” | Direct transcription. |
| rule_not_applicable | `not("Metformin plus lifestyle"), -` | `Not applicable` | `synthetic-diabetes-cpg.md:50` “\| Any other treatment \| - \| Not applicable \|” | Direct transcription; `not(...)` represents “Any other treatment” for the declared string input. |

## Adult Lipoprotein Screening and Nonfasting Follow-up (`adult-lipoprotein-screening.dmn`)

| Rule id | Input cells | Output cells | Source (file:line, quoted sentence or table row) | Assumptions / notes |
|---|---|---|---|---|
| rule_1 | `>= 20, -, true, >= 200, -` | `Obtain follow-up fasting lipoprotein profile` | `atp-iii-cholesterol-cpg.md:218` “In all adults aged 20 years or older, a fasting lipoprotein profile (total cholesterol, LDL cholesterol, high density lipoprotein (HDL) cholesterol, and triglyceride) should be obtained once every 5 years. If the testing opportunity is nonfasting, only the values for total cholesterol and HDL cholesterol will be usable. In such a case, if total cholesterol is ≥ 200 mg/dL or HDL is &lt;40 mg/dL, a followup lipoprotein profile is needed for  appropriate management based on LDL.” | Direct transcription; cholesterol values are mg/dL. |
| rule_2 | `>= 20, -, true, -, < 40` | `Obtain follow-up fasting lipoprotein profile` | `atp-iii-cholesterol-cpg.md:218` “...if total cholesterol is ≥ 200 mg/dL or HDL is &lt;40 mg/dL, a followup lipoprotein profile is needed...” | Direct transcription. |
| rule_3 | `>= 20, >= 5, false, -, -` | `Obtain fasting lipoprotein profile` | `atp-iii-cholesterol-cpg.md:218` “...a fasting lipoprotein profile ... should be obtained once every 5 years.” | Direct transcription; `Years Since Last...` is a derived number of years. |
| rule_4 | `>= 20, < 5, false, -, -` | `No routine test currently due` | `atp-iii-cholesterol-cpg.md:218` “...should be obtained once every 5 years.” | Assumption: the complement of the stated interval is treated as not currently due. There is no extraction primitive for time since the last observation. |
| rule_5 | `>= 20, -, true, < 200, >= 40` | `No follow-up fasting profile required` | `atp-iii-cholesterol-cpg.md:218` “If the testing opportunity is nonfasting, only the values for total cholesterol and HDL cholesterol will be usable.” | Assumption: the source is silent on this nonfasting normal branch, so it is given an explicit neutral outcome. |

## LDL Cholesterol Classification (`ldl-cholesterol-classification.dmn`)

| Rule id | Input cells | Output cells | Source (file:line, quoted sentence or table row) | Assumptions / notes |
|---|---|---|---|---|
| rule_1 | `< 100` | `Optimal` | `atp-iii-cholesterol-cpg.md:224` “\| <100 \| Optimal \|” | Direct transcription; mg/dL. |
| rule_2 | `[100..130)` | `Near optimal/above optimal` | `atp-iii-cholesterol-cpg.md:225` “\| 100-129 \| Near optimal/above optimal \|” | Direct transcription of integer mg/dL band as a half-open interval. |
| rule_3 | `[130..160)` | `Borderline high` | `atp-iii-cholesterol-cpg.md:226` “\| 130-159 \| Borderline high \|” | Direct transcription. |
| rule_4 | `[160..190)` | `High` | `atp-iii-cholesterol-cpg.md:227` “\| 160-189 \| High \|” | Direct transcription. |
| rule_5 | `>= 190` | `Very high` | `atp-iii-cholesterol-cpg.md:228` “\| ≥ 190 \| Very high \|” | Direct transcription. |

## Total Cholesterol Classification (`total-cholesterol-classification.dmn`)

| Rule id | Input cells | Output cells | Source (file:line, quoted sentence or table row) | Assumptions / notes |
|---|---|---|---|---|
| rule_1 | `< 200` | `Desirable` | `atp-iii-cholesterol-cpg.md:230` “\| <200 \| Desirable \|” | Direct transcription; mg/dL. |
| rule_2 | `[200..240)` | `Borderline high` | `atp-iii-cholesterol-cpg.md:231` “\| 200-239 \| Borderline high \|” | Direct transcription. |
| rule_3 | `>= 240` | `High` | `atp-iii-cholesterol-cpg.md:232` “\| ≥ 240 \| High \|” | Direct transcription. |

## HDL Cholesterol Classification (`hdl-cholesterol-classification.dmn`)

| Rule id | Input cells | Output cells | Source (file:line, quoted sentence or table row) | Assumptions / notes |
|---|---|---|---|---|
| rule_1 | `< 40` | `Low` | `atp-iii-cholesterol-cpg.md:234` “\| <40 \| Low \|” | Direct transcription; mg/dL. |
| rule_2 | `[40..60)` | `Not classified` | `atp-iii-cholesterol-cpg.md:233-235` “\| HDL Cholesterol \| \|”; “\| <40 \| Low \|”; “\| ≥ 60 \| High \|” | Assumption: ATP III does not assign a category to 40–59 mg/dL; this explicit row is not an ATP III category. |
| rule_3 | `>= 60` | `High` | `atp-iii-cholesterol-cpg.md:235` “\| ≥ 60 \| High \|” | Direct transcription. |

## Triglyceride Classification (`triglyceride-classification.dmn`)

| Rule id | Input cells | Output cells | Source (file:line, quoted sentence or table row) | Assumptions / notes |
|---|---|---|---|---|
| rule_1 | `< 150` | `Normal` | `atp-iii-cholesterol-cpg.md:476` “\| ■ \| Normal triglycerides: <150 mg/dL \|” | Direct transcription. |
| rule_2 | `[150..200)` | `Borderline-high` | `atp-iii-cholesterol-cpg.md:478` “\| ■ Borderline-high triglycerides: \| 150-199 mg/dL \|” | Direct transcription; integer range represented half-open. |
| rule_3 | `[200..500)` | `High` | `atp-iii-cholesterol-cpg.md:479` “\| ■ High triglycerides: \| 200-499 mg/dL \|” | Direct transcription. |
| rule_4 | `>= 500` | `Very high` | `atp-iii-cholesterol-cpg.md:480` “\| ■ Very high triglycerides: \| ≥ 500 mg/dL \|” | Direct transcription. |

## LDL-Lowering Risk Category Assignment (`ldl-lowering-risk-category.dmn`)

| Rule id | Input cells | Output cells | Source (file:line, quoted sentence or table row) | Assumptions / notes |
|---|---|---|---|---|
| rule_1 | `true, -, -, -, -` | `CHD/CHD risk equivalent; <100 mg/dL` | `atp-iii-cholesterol-cpg.md:252,258,264` “CHD and CHD risk equivalents \| <100”; “The category of highest risk consists of CHD and CHD risk equivalents.” | Direct transcription. |
| rule_2 | `false, true, -, -, -` | `CHD/CHD risk equivalent; <100 mg/dL` | `atp-iii-cholesterol-cpg.md:260` “Other clinical forms of atherosclerotic disease...” | Direct transcription. |
| rule_3 | `false, false, true, -, -` | `CHD/CHD risk equivalent; <100 mg/dL` | `atp-iii-cholesterol-cpg.md:261,264` “Diabetes”; “Persons with CHD or CHD risk equivalents have the lowest LDL cholesterol goal (<100 mg/dL).” | Direct transcription. |
| rule_4 | `false, false, false, >= 2, > 20` | `CHD/CHD risk equivalent; <100 mg/dL` | `atp-iii-cholesterol-cpg.md:262` “Multiple risk factors that confer a 10-year risk for CHD &gt;20%.” | Direct transcription; risk is a percent and >20 is exclusive. |
| rule_5 | `false, false, false, >= 2, [10..20]` | `2+ risk factors with 10-year risk 10-20%; <130 mg/dL` | `atp-iii-cholesterol-cpg.md:266,274` “The second category consists of persons with multiple (2+) risk factors in whom 10-year risk for CHD is ≤ 20%.”; “...10-year risk for CHD of ... 10-20%...” | Direct transcription of the intermediate band, including 20. |
| rule_6 | `false, false, false, >= 2, < 10` | `2+ risk factors with 10-year risk <10%; <130 mg/dL` | `atp-iii-cholesterol-cpg.md:266,274` “The LDL cholesterol goal for persons with multiple (2+) risk factors is &lt;130 mg/dL.”; “...10-year risk for CHD of ... &lt;10%.” | Direct transcription. |
| rule_7 | `false, false, false, <= 1, -` | `0-1 risk factor; <160 mg/dL` | `atp-iii-cholesterol-cpg.md:268` “The third category consists of persons having 0-1 risk factor... Their LDL cholesterol goal is &lt;160 mg/dL.” | Direct transcription. |
