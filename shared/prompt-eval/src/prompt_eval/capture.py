"""Explicit hooks: wrap EACH provider attempt, including retries, in attempt()."""

from contextlib import contextmanager
from time import perf_counter

from .models import OperationalError, now
from .security import traced
from .storage import PersistenceError


class Attempt:
    def __init__(self, capture, number, request):
        self.capture = capture
        self.number = number
        self.request = request
        self.raw_response = None
        self.validation_errors = []
        self.usage_data = {}
        self.started_at = now()
        self.started = perf_counter()
        self.prefix = f"{capture.prefix}/attempt-{number}"

    @traced
    def response(self, value):
        self.capture.store.put(f"{self.prefix}/response.json", value)
        self.raw_response = value

    @traced
    def validation_error(self, value):
        self.capture.store.put(
            f"{self.prefix}/validation-{len(self.validation_errors) + 1}.json", value
        )
        self.validation_errors.append(value)

    @traced
    def usage(self, **values):
        if any(
            not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0 for v in values.values()
        ):
            raise ValueError("Usage values must be nonnegative numbers")
        self.usage_data.update(values)


class Capture:
    def __init__(self, store, stage, case_id, repetition):
        self.store = store
        self.stage = stage
        self.case_id = case_id
        self.repetition = repetition
        self.prefix = f"stages/{stage}/repetition-{repetition}/cases/{case_id}"
        self.attempts = 0
        self.errors = []

    @contextmanager
    def attempt(self, request):
        self.attempts += 1
        attempt = Attempt(self, self.attempts, request)
        self.store.put(f"{attempt.prefix}/request.json", request)
        failure = None
        try:
            yield attempt
        except PersistenceError:
            raise
        except Exception as exc:
            failure = OperationalError(
                kind="timeout" if isinstance(exc, TimeoutError) else "invocation",
                message=str(exc),
                stage=self.stage,
                case_id=self.case_id,
                repetition=self.repetition,
                attempt=attempt.number,
                exception_type=type(exc).__name__,
            )
            self.errors.append(failure)
            raise
        finally:
            if attempt.validation_errors and failure is None:
                failure = OperationalError(
                    kind="parse/contract",
                    message="Attempt validation failed",
                    stage=self.stage,
                    case_id=self.case_id,
                    repetition=self.repetition,
                    attempt=attempt.number,
                )
                self.errors.append(failure)
            self.store.put(
                f"{attempt.prefix}/attempt.json",
                {
                    "stage": self.stage,
                    "case_id": self.case_id,
                    "repetition": self.repetition,
                    "attempt": attempt.number,
                    "started_at": attempt.started_at,
                    "completed_at": now(),
                    "latency_ms": (perf_counter() - attempt.started) * 1000,
                    "request": request,
                    "response": attempt.raw_response,
                    "usage": attempt.usage_data,
                    "validation_errors": attempt.validation_errors,
                    "exception": failure.model_dump(mode="json") if failure else None,
                },
            )
