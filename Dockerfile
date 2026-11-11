# syntax=docker/dockerfile:1

# One image for the workflow agents: the container contract of each agent. One
# entrypoint; AGENT_NAME names the agent that `serve` starts (README, "The
# image contract").

# Base image pinned by tag and digest (Debian 13 "trixie", Python 3.13).
ARG PYTHON_IMAGE=python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# ---- runtime ----
FROM ${PYTHON_IMAGE}

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="tnwks-workflow-agents" \
      org.opencontainers.image.description="Workflow agents with the container contract" \
      org.opencontainers.image.source="https://github.com/iT3E/tnwks-workflow-agents" \
      org.opencontainers.image.url="https://github.com/iT3E/tnwks-workflow-agents" \
      org.opencontainers.image.vendor="tnwks" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

RUN --mount=type=bind,source=requirements.txt,target=/tmp/requirements.txt \
    --mount=type=bind,source=constraints.txt,target=/tmp/constraints.txt \
    pip install --no-cache-dir --root-user-action=ignore --disable-pip-version-check \
      -r /tmp/requirements.txt -c /tmp/constraints.txt \
 && groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin app

# Code only, owned by root and read-only for the app user. A sandbox policy
# must list /app as a read-only path, or the agent cannot read its code.
COPY src/ /app/src/

# The redaction setting of Strands: no prompt, answer or tool text in a span.
# The code sets the same default, for a runtime that drops the image env.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    TMPDIR=/tmp \
    OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental,gen_ai_unredacted_attributes=

WORKDIR /app
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python3", "/app/src/entrypoint.py"]
CMD ["serve"]
