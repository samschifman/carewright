"""A single binding shared by module execution and canonical adapter imports."""

from contextlib import contextmanager
from contextvars import ContextVar

from prompt_eval.models import RunConfig

configuration = ContextVar("recommendation_run_configuration", default=None)


@contextmanager
def bind_config(config: RunConfig):
    token = configuration.set(config)
    try:
        yield
    finally:
        configuration.reset(token)
