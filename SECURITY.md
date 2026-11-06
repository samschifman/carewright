# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability in CPG-to-ACP, please report it responsibly by opening a GitHub issue or contacting the maintainers directly. Do not include exploit details in public issues — provide a summary and we will coordinate privately.

## Medical Disclaimer

CPG-to-ACP is a **demonstration system** for transforming Clinical Practice Guidelines into actionable care plans using AI. The synthetic CPG, patient data, and generated care plans included in this repository are **for demonstration and testing purposes only**.

**This software must not be used for:**
- Clinical diagnosis, treatment, or patient care decisions
- Real patient data processing without appropriate regulatory compliance
- Any purpose requiring FDA clearance, CE marking, or equivalent medical device certification

The example scenarios use synthetic clinical practice guidelines and hand-crafted patient data. Any resemblance to real patients, clinical outcomes, or published guidelines is for illustrative purposes only.

## Secrets Management

- API keys and credentials must be stored in `.env` files (gitignored) or environment variables, never in source code.
- `.env.example` files document the required environment variables with placeholder values.
- No secrets are embedded in container images, configuration files, or test fixtures.
- The placeholder API key `sk-change-me` in the codebase is not a real credential.

## Deployment Hardening

CPG-to-ACP is experimental and restricted to synthetic data within a verified peer-workload boundary. Browser routes require scoped sessions and CSRF for mutations; internal mutating routes require receiver service credentials, and approved FHIR publication requires a signed artifact-bound review. Deployment isolation and OpenShell gateway authentication must pass environment probes before use. See [the security design and accepted gaps](docs/security.md).

## Dependency Policy

This project uses the Apache-2.0 license. Dependencies should be compatible with Apache-2.0 (permissive licenses). Avoid adding copyleft (GPL, AGPL, LGPL) dependencies without discussion.
