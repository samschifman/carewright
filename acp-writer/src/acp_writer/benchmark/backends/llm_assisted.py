"""LLM-assisted benchmark backend — concept resolver + pipeline + agent fallback.

Layered resolution with fall-through semantics:
1. Structured intent (if provided) → deterministic execution
2. Concept resolver (deterministic) → positive/insufficient short-circuit;
   negative booleans fall through to LLM (not definitive without pipeline)
3. LLM query plan synthesis → execute plan; numeric answers to boolean-intent
   questions fall through to the agent for judgment completion
4. LLM agent with concept-based tools → open-vocabulary fallback
5. Answer guardrails → verify EVERY answer at the choke point before returning
"""

import logging
import os
import re
from datetime import date
from typing import Any

from acp_writer.benchmark.backends.current import CurrentImplementationBackend
from acp_writer.benchmark.models import QAAnswer
from acp_writer.llm import get_llm
from acp_writer.tools.concept_resolver import resolve as resolve_concept
from acp_writer.tools.ips_serializer import serialize_ips
from acp_writer.tools.query_planner import generate_query_plan

logger = logging.getLogger(__name__)


_BOOLEAN_LEADS = re.compile(
    r"^(is|are|does|do|has|have|should|was|were|can|could|would|will)\b",
    re.IGNORECASE,
)


def question_intent(question: str) -> str:
    """Classify question intent: boolean, numeric, or open.

    Deterministic, no LLM call. Logged on every answer.
    """
    q = question.strip().rstrip("?").strip()
    if _BOOLEAN_LEADS.match(q):
        return "boolean"
    lower = q.lower()
    if lower.startswith(("what is", "what are", "how much", "how many", "when")):
        return "numeric"
    return "open"


class LLMAssistedBackend(CurrentImplementationBackend):
    """Extends CurrentImplementationBackend with LLM-assisted resolution layers."""

    name: str = "llm-assisted"

    def __init__(self):
        self._llm = None

    def _get_llm(self):
        if self._llm is None:
            base_url = os.environ.get("LLM_BASE_URL", os.environ.get("LITELLM_URL", "http://localhost:4000"))
            model = os.environ.get("LLM_MODEL", "default")
            self._llm = get_llm(
                base_url=base_url,
                model=model,
                max_retries=3,
                request_timeout=60,
            )
        return self._llm

    def answer(
        self,
        question: str,
        bundle: dict[str, Any],
        reference_date: date,
        structured_intent: dict[str, Any] | None = None,
    ) -> QAAnswer:
        from acp_writer.tools.answer_guardrails import verify_answer, check_definitive_miss
        from acp_writer.tools.bundle_inventory import build_bundle_inventory

        inventory = build_bundle_inventory(bundle)

        intent = question_intent(question)

        if structured_intent is not None:
            result = super().answer(question, bundle, reference_date, structured_intent)
            result.answered_by = "structured_intent"
            return verify_answer(result, question, bundle, inventory, question_intent=intent)

        resolved = resolve_concept(question)
        if resolved:
            result = self._execute_resolved(resolved, bundle, reference_date)
            if result.value is True or result.insufficient_data:
                result.answered_by = "resolver"
                return verify_answer(result, question, bundle, inventory, question_intent=intent)
            if result.value is not False:
                result.answered_by = "resolver"
                return verify_answer(result, question, bundle, inventory, question_intent=intent)

        return self._llm_resolve(question, bundle, reference_date, inventory)

    def _llm_resolve(
        self, question: str, bundle: dict, reference_date: date, inventory: "BundleInventory",
    ) -> QAAnswer:
        """Try LLM query plan synthesis, falling back to agent."""
        from acp_writer.tools.answer_guardrails import verify_answer, check_definitive_miss

        condensed = serialize_ips(bundle)
        llm = self._get_llm()
        inventory_text = inventory.render_for_llm()
        intent = question_intent(question)
        logger.debug("Question intent: %s for '%s'", intent, question[:60])

        plan = generate_query_plan(question, condensed, reference_date, llm, inventory_text=inventory_text)
        retrieved_value = None
        if plan:
            result = super().answer(
                question, bundle, reference_date,
                structured_intent=plan,
            )
            if not result.insufficient_data:
                if intent == "boolean" and not isinstance(result.value, bool):
                    logger.debug("Intent mismatch: boolean question got %s (%s) — falling through to agent",
                                type(result.value).__name__, result.value)
                    retrieved_value = result.value
                else:
                    result.answered_by = "query_plan"
                    return verify_answer(result, question, bundle, inventory, question_intent=intent)

        from acp_writer.tools.qa_agent import agent_answer

        agent_result = agent_answer(
            question, bundle, reference_date, llm,
            extra_context=f"Retrieved value: {retrieved_value}" if retrieved_value is not None else None,
        )
        tool_ledger = agent_result.get("tool_ledger", [])

        result = QAAnswer(
            value=agent_result.get("answer"),
            kind=self._infer_kind(agent_result.get("answer")),
            provenance=agent_result.get("provenance", []),
            insufficient_data=agent_result.get("insufficient_data", False),
            error=agent_result.get("error"),
            answered_by="agent",
        )

        if intent == "boolean" and not isinstance(result.value, bool) and not result.insufficient_data:
            logger.info("Intent-type mismatch at choke point: boolean question, %s answer — downgrading",
                       type(result.value).__name__)
            result = QAAnswer(
                value=None, kind="insufficient_data", provenance=result.provenance,
                insufficient_data=True, error="guardrail:intent_type_mismatch",
                answered_by="guardrail_downgrade",
                resolution_basis="intent_type_mismatch: boolean question got non-boolean answer",
            )
            return result

        result = verify_answer(result, question, bundle, inventory,
                               tool_ledger=tool_ledger, question_intent=intent)

        if result.answered_by != "guardrail_downgrade":
            result = check_definitive_miss(tool_ledger, result)

        return result

    @staticmethod
    def _infer_kind(value: Any) -> str:
        if value is None:
            return "insufficient_data"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        return "code"
