# One Dockerfile for all three services.
#
# They differ only in which package and requirements file gets copied, so three
# near-identical files would be three places to forget to change. Build with:
#
#   docker build --build-arg SERVICE=storefront -t storefront:local app/
#
# Context is app/ so the shared svc_common library is reachable; a per-service context
# could not see ../libs.

ARG PYTHON_VERSION=3.12-slim

FROM python:${PYTHON_VERSION} AS builder
ARG SERVICE
WORKDIR /build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Requirements first, on their own layer: application code changes on every commit,
# dependencies change a few times a project. Copying them together would reinstall the
# whole dependency tree for a one-line handler edit.
COPY services/${SERVICE}/requirements.txt ./requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


FROM python:${PYTHON_VERSION} AS runtime
ARG SERVICE
ARG GIT_SHA=dev

LABEL org.opencontainers.image.source="https://github.com/tani-mani-meow/k8s-slo-platform" \
      org.opencontainers.image.title="${SERVICE}" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SERVICE_NAME=${SERVICE} \
    SERVICE_VERSION=${GIT_SHA}

# Numeric uid so the Pod's runAsNonRoot check can verify it without resolving a name.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY libs/svc_common ./svc_common
COPY services/${SERVICE} ./${SERVICE}

# SERVICE is a build arg and unavailable at runtime, so bake the module path into a
# concrete command rather than relying on shell expansion in the entrypoint.
RUN printf '#!/bin/sh\nexec uvicorn %s.main:app --host 0.0.0.0 --port 8000 --no-access-log "$@"\n' \
        "${SERVICE}" > /usr/local/bin/run-service \
    && chmod +x /usr/local/bin/run-service \
    && rm -rf ./${SERVICE}/tests

USER 10001:10001
EXPOSE 8000

# Access logs are off: the metrics middleware already records every request, and under
# the load generator's constant traffic a line per request is pure noise in Loki.
ENTRYPOINT ["run-service"]
