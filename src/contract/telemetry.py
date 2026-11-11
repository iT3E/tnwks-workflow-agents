"""Traces of a workflow agent: no text of a run in a span, and a flush before each answer.

Strands puts the prompt, the answer and the tool text in span attributes unless
OTEL_SEMCONV_STABILITY_OPT_IN holds the token `gen_ai_unredacted_attributes=`
with an empty list. That is a rule, so the code sets it and removes any other
value of the token (charter R1). A sandbox that runs one time ends before the
batch exporter sends its spans, so the contract flushes them before it answers.
"""

import logging
import os

log = logging.getLogger(__name__)

OPT_IN = "OTEL_SEMCONV_STABILITY_OPT_IN"
REDACT = "gen_ai_unredacted_attributes="
LATEST = "gen_ai_latest_experimental"

_provider = None


def enforce_redaction() -> str:
    """Set the redaction token of Strands, whatever the environment holds. Returns the new value."""
    tokens = [t.strip() for t in os.environ.get(OPT_IN, "").split(",") if t.strip()]
    tokens = [t for t in tokens if not t.startswith("gen_ai_unredacted_attributes")]
    if LATEST not in tokens:
        tokens.insert(0, LATEST)
    tokens.append(REDACT)
    value = ",".join(tokens)
    os.environ[OPT_IN] = value
    return value


def setup(service_name: str) -> bool:
    """Export spans over OTLP when OTEL_EXPORTER_OTLP_ENDPOINT is set. Returns True when it does."""
    global _provider
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    if _provider is not None:
        return True
    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
    from strands.telemetry import StrandsTelemetry

    _provider = StrandsTelemetry().setup_otlp_exporter().tracer_provider
    log.info("trace export on, service %s", os.environ["OTEL_SERVICE_NAME"])
    return True


def flush(timeout_millis: int = 5000) -> bool:
    """Send each finished span now. Returns False when the export did not finish in time."""
    provider = _provider
    if provider is None:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
    force_flush = getattr(provider, "force_flush", None)
    if force_flush is None:
        return True
    try:
        done = bool(force_flush(timeout_millis))
    except Exception:  # an export error must not lose the answer
        log.exception("span flush failed")
        return False
    if not done:
        log.warning("span flush did not finish in %d ms", timeout_millis)
    return done
