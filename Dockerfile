# syntax=docker/dockerfile:1

# One image for each part of the run path of a workflow agent: the container
# contract of each agent, the run receiver, the run job and the sweep. One
# entrypoint; its first argument names the part, and AGENT_NAME names the agent
# that `serve` starts (README, "The image contract").

# Base image pinned by tag and digest (Debian 13 "trixie", Python 3.13).
ARG PYTHON_IMAGE=python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# ---- the OpenShell CLI, for the run job and the sweep ----
FROM ${PYTHON_IMAGE} AS openshell
ARG TARGETARCH
# The CLI release must match the release of the sandbox gateway chart in
# tnwks-ops. A new version needs the two checksums of its release
# (openshell-checksums-sha256.txt): the build fails until they match.
# renovate: datasource=github-releases depName=NVIDIA/OpenShell
ARG OPENSHELL_VERSION=v0.1.7
ARG OPENSHELL_SHA256_AMD64=5c1adfcae797eac9b2e8b9280c66c1f75e4e84d9e31ce6938c5b994314ef6098
ARG OPENSHELL_SHA256_ARM64=696b523c263895b69eaa7c7e6afbb19b8af6c65cd79c7ffac7b685b6e3aa23da
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=x86_64; sum="${OPENSHELL_SHA256_AMD64}" ;; \
      arm64) arch=aarch64; sum="${OPENSHELL_SHA256_ARM64}" ;; \
      *) echo "no OpenShell CLI for ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    file="openshell-${arch}-unknown-linux-musl.tar.gz"; \
    python3 -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
      "https://github.com/NVIDIA/OpenShell/releases/download/${OPENSHELL_VERSION}/${file}" "/tmp/${file}"; \
    echo "${sum}  /tmp/${file}" | sha256sum -c -; \
    tar -xzf "/tmp/${file}" -C /usr/local/bin openshell; \
    chmod 0755 /usr/local/bin/openshell; \
    /usr/local/bin/openshell --version

# ---- runtime ----
FROM ${PYTHON_IMAGE}

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="tnwks-workflow-agents" \
      org.opencontainers.image.description="Workflow agents with the container contract, their run receiver and their run job" \
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

COPY --from=openshell /usr/local/bin/openshell /usr/local/bin/openshell

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
