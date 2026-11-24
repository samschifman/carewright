"""Conservative one-to-one identity alignment; never a clinical truth judgment."""

from collections import Counter

from cpg_contracts import Recommendation
from prompt_eval.models import Model
from prompt_eval.security import traced

from .models import EvaluationCase
from .text import contains, tokens


class MatchResult(Model):
    pairs: tuple[tuple[int, int], ...]
    missing: tuple[int, ...]
    extras: tuple[int, ...]
    duplicates: tuple[int, ...]
    ambiguous: dict[int, tuple[int, ...]]
    evidence: dict[int, str]


@traced
def match(case: EvaluationCase, produced: list[Recommendation]) -> MatchResult:
    annotations = {a.recommendation_id: a for a in case.annotations}
    candidates = {}
    evidence = {}
    for i, row in enumerate(produced):
        edges = []
        if row.source_cpg == case.source_cpg and row.section == case.section:
            for j, golden in enumerate(case.golden.recommendations):
                annotation = annotations[golden.id]
                if row.id == golden.id:
                    edges.append(j)
                    evidence[i] = "stable-id"
                elif (
                    row.source_location
                    and row.source_location.source_text
                    and tokens(row.source_location.source_text)
                    == tokens(annotation.source_quote)
                    and any(
                        contains(row.content, action)
                        for action in annotation.action_aliases
                    )
                ):
                    edges.append(j)
                    evidence.setdefault(i, "source-and-action")
        # Stable IDs take priority over fallback candidates for that output.
        stable = [j for j in edges if case.golden.recommendations[j].id == row.id]
        candidates[i] = stable or edges
    incoming = Counter(j for edges in candidates.values() for j in edges)
    pairs = tuple(
        (j, i)
        for i, edges in candidates.items()
        if len(edges) == 1
        for j in edges
        if incoming[j] == 1
    )
    paired_outputs = {i for _, i in pairs}
    paired_goldens = {j for j, _ in pairs}
    ambiguous = {
        i: tuple(edges)
        for i, edges in candidates.items()
        if edges and i not in paired_outputs
    }
    seen_ids, seen_content = set(), set()
    duplicates = []
    for i, row in enumerate(produced):
        identity = (row.source_cpg, row.section, row.id)
        content = (row.source_cpg, row.section, tokens(row.content))
        if identity in seen_ids or content in seen_content:
            duplicates.append(i)
        seen_ids.add(identity)
        seen_content.add(content)
    return MatchResult(
        pairs=pairs,
        missing=tuple(
            j
            for j in range(len(case.golden.recommendations))
            if j not in paired_goldens
        ),
        extras=tuple(i for i in range(len(produced)) if i not in paired_outputs),
        duplicates=tuple(duplicates),
        ambiguous=ambiguous,
        evidence=evidence,
    )
