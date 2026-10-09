"""Optional MLflow tracing shim for the automation contract package."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, overload

F = TypeVar("F", bound=Callable[..., Any])

try:
    from mlflow import trace as trace
except ImportError:
    @overload
    def trace(func: F, /) -> F: ...

    @overload
    def trace(*, name: str | None = None, **kwargs: Any) -> Callable[[F], F]: ...

    def trace(func: F | None = None, /, **kwargs: Any) -> F | Callable[[F], F]:
        """Return the original function whether used as ``@trace`` or ``@trace(...)``."""
        del kwargs

        def decorate(target: F) -> F:
            return target

        if func is not None:
            return decorate(func)
        return decorate
