"""Common result types shared by the automation validation ladder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["ERROR", "WARNING"]


@dataclass
class Finding:
    rung: str
    severity: Severity
    code: str
    message: str
    element_id: str | None = None


@dataclass
class LadderResult:
    ok: bool
    findings: list[Finding]
    rungs_passed: list[str]
    deferred: list[str] = field(default_factory=list)

    def errors(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "ERROR"]

    def warnings(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "WARNING"]

    def merge(self, other: LadderResult) -> LadderResult:
        """Merge another rung result into this result and return this object."""
        self.findings.extend(other.findings)
        for rung in other.rungs_passed:
            if rung not in self.rungs_passed:
                self.rungs_passed.append(rung)
        for item in other.deferred:
            if item not in self.deferred:
                self.deferred.append(item)
        self.ok = self.ok and other.ok and not self.errors()
        return self
