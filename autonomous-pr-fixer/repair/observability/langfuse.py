"""
Tracing for repair runs.

One repair run is one Langfuse trace whose root is an `agent` observation. Each model call
is a `generation` (model, token usage, cost, latency), each tool call a `tool`, each gate
submission a `chain` named `cerberus-verification`. Langfuse is observability only: the
verdict of record is always the gate's run.json.

Privacy: by default no prompt, completion, file content or test output is sent; only
metadata (sizes, names, codes, counts). Everything that is sent passes through `redact`.
Langfuse is enabled only when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Protocol

_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|passwd|credential|authorization|private[_-]?key|cookie)", re.I)
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|sk-ant-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abposr]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"
)
REDACTED = "[REDACTED]"


def _env_secrets() -> List[str]:
    vals = []
    for k, v in os.environ.items():
        if v and len(v) >= 8 and _SECRET_KEY.search(k):
            vals.append(v)
    return vals


def redact(value: Any, _secrets: Optional[List[str]] = None) -> Any:
    """Mask secret-looking keys, known secret formats and the values of secret environment variables."""
    secrets = _env_secrets() if _secrets is None else _secrets
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v, secrets)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        out = _SECRET_VALUE.sub(REDACTED, value)
        for s in secrets:
            if s in out:
                out = out.replace(s, REDACTED)
        return out
    return value


class Span(Protocol):
    def end(self, **fields: Any) -> None: ...


class Tracer(Protocol):
    trace_id: Optional[str]

    def start_run(self, run_id: str, metadata: Dict[str, Any]) -> None: ...
    def generation(self, name: str, model: str, input_tokens: int, output_tokens: int, cost: Optional[float],
                   latency_seconds: float, metadata: Dict[str, Any], error: Optional[str] = None) -> None: ...
    def tool(self, name: str, arguments: Dict[str, Any], ok: bool, duration_seconds: float, metadata: Dict[str, Any]) -> None: ...
    def verification(self, attempt: int, status: str, failure_code: Optional[str], duration_seconds: float,
                     metadata: Dict[str, Any]) -> None: ...
    def end_run(self, final_status: str, metadata: Dict[str, Any]) -> None: ...


class NoopTracer:
    trace_id: Optional[str] = None

    def start_run(self, run_id, metadata): pass
    def generation(self, name, model, input_tokens, output_tokens, cost, latency_seconds, metadata, error=None): pass
    def tool(self, name, arguments, ok, duration_seconds, metadata): pass
    def verification(self, attempt, status, failure_code, duration_seconds, metadata): pass
    def end_run(self, final_status, metadata): pass


class RecordingTracer:
    """In-memory tracer for tests; records exactly what would be sent (after redaction)."""

    def __init__(self):
        self.trace_id: Optional[str] = None
        self.events: List[Dict[str, Any]] = []

    def _add(self, kind: str, **fields: Any) -> None:
        self.events.append({"kind": kind, **redact(fields)})

    def start_run(self, run_id, metadata):
        self.trace_id = f"trace-{run_id}"
        self._add("run_start", run_id=run_id, metadata=metadata)

    def generation(self, name, model, input_tokens, output_tokens, cost, latency_seconds, metadata, error=None):
        self._add("generation", name=name, model=model, input_tokens=input_tokens, output_tokens=output_tokens,
                  cost=cost, latency_seconds=latency_seconds, metadata=metadata, error=error)

    def tool(self, name, arguments, ok, duration_seconds, metadata):
        self._add("tool", name=name, arguments=arguments, ok=ok, duration_seconds=duration_seconds, metadata=metadata)

    def verification(self, attempt, status, failure_code, duration_seconds, metadata):
        self._add("verification", attempt=attempt, status=status, failure_code=failure_code,
                  duration_seconds=duration_seconds, metadata=metadata)

    def end_run(self, final_status, metadata):
        self._add("run_end", final_status=final_status, metadata=metadata)


class LangfuseTracer:
    """Langfuse v3 SDK. `client` is injectable for tests."""

    def __init__(self, client: Any = None):
        if client is None:
            from langfuse import Langfuse
            client = Langfuse()  # reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
        self.client = client
        self.root: Any = None
        self.trace_id: Optional[str] = None

    def start_run(self, run_id: str, metadata: Dict[str, Any]) -> None:
        # Deterministic: the same run id always maps to the same trace id.
        self.trace_id = self.client.create_trace_id(seed=run_id)
        meta = redact(metadata)
        self.root = self.client.start_observation(
            trace_context={"trace_id": self.trace_id}, name="cerberus-repair", as_type="agent",
            input={"issue_id": meta.get("issue_id"), "issue_title": meta.get("issue_title")}, metadata=meta,
        )
        self.root.update_trace(name=f"cerberus-repair {run_id}", session_id=meta.get("experiment_id"),
                               metadata=meta, tags=["cerberus", "phase1"])

    def _child(self, name: str, as_type: str, **kwargs: Any) -> None:
        if self.root is None:
            return
        obs = self.root.start_observation(name=name, as_type=as_type, **kwargs)
        obs.end()

    def generation(self, name, model, input_tokens, output_tokens, cost, latency_seconds, metadata, error=None):
        cost_details = {"total": cost} if cost is not None else None
        self._child(
            name, "generation", model=model,
            usage_details={"input": input_tokens, "output": output_tokens, "total": input_tokens + output_tokens},
            cost_details=cost_details,
            metadata=redact({**metadata, "latency_seconds": round(latency_seconds, 3)}),
            level="ERROR" if error else None, status_message=redact(error) if error else None,
        )

    def tool(self, name, arguments, ok, duration_seconds, metadata):
        self._child(f"tool:{name}", "tool", input=redact(arguments),
                    metadata=redact({**metadata, "duration_seconds": duration_seconds}),
                    level=None if ok else "WARNING")

    def verification(self, attempt, status, failure_code, duration_seconds, metadata):
        self._child("cerberus-verification", "chain",
                    output={"status": status, "failure_code": failure_code},
                    metadata=redact({**metadata, "attempt": attempt, "duration_seconds": duration_seconds}))

    def end_run(self, final_status: str, metadata: Dict[str, Any]) -> None:
        if self.root is None:
            return
        meta = redact(metadata)
        self.root.update(output={"final_status": final_status}, metadata=meta)
        self.root.update_trace(output={"final_status": final_status}, metadata=meta)
        self.root.end()
        self.client.flush()


def langfuse_configured() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def create_tracer() -> Tracer:
    if not langfuse_configured():
        return NoopTracer()
    try:
        return LangfuseTracer()
    except Exception:  # tracing must never break a repair run
        return NoopTracer()
