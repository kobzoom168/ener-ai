from __future__ import annotations

import contextvars

_trace_id_var = contextvars.ContextVar("current_trace_id", default=None)
_conversation_id_var = contextvars.ContextVar("current_conversation_id", default=None)
_source_var = contextvars.ContextVar("current_source", default=None)
_project_id_var = contextvars.ContextVar("current_project_id", default=None)


def set_trace_context(
    trace_id: str,
    conversation_id: str | None = None,
    source: str | None = None,
    project_id: int | None = None,
):
    return (
        _trace_id_var.set(str(trace_id or "").strip() or None),
        _conversation_id_var.set(str(conversation_id or "").strip() or None),
        _source_var.set(str(source or "").strip() or None),
        _project_id_var.set(project_id),
    )


def reset_trace_context(token_or_tokens) -> None:
    if token_or_tokens is None:
        return
    if not isinstance(token_or_tokens, tuple):
        token_or_tokens = (token_or_tokens,)
    vars_with_tokens = (
        (_trace_id_var, token_or_tokens[0] if len(token_or_tokens) > 0 else None),
        (_conversation_id_var, token_or_tokens[1] if len(token_or_tokens) > 1 else None),
        (_source_var, token_or_tokens[2] if len(token_or_tokens) > 2 else None),
        (_project_id_var, token_or_tokens[3] if len(token_or_tokens) > 3 else None),
    )
    for var, token in vars_with_tokens:
        if token is None:
            continue
        try:
            var.reset(token)
        except Exception:
            pass


def get_trace_context() -> dict:
    return {
        "trace_id": _trace_id_var.get(),
        "conversation_id": _conversation_id_var.get(),
        "source": _source_var.get(),
        "project_id": _project_id_var.get(),
    }
