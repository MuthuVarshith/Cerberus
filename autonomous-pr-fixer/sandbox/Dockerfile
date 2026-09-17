# Cerberus sandbox image: the minimum the gates need inside the container.
# Build from autonomous-pr-fixer/:  docker build -t cerberus-sandbox:py3.11 sandbox/
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git coreutils \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "pytest>=8,<9"

# Workspaces are bind-mounted from the host, so their owner differs from the
# container user; git would otherwise refuse to operate on them.
RUN git config --system --add safe.directory '*' \
    && git config --system user.name "Cerberus" \
    && git config --system user.email "cerberus@localhost" \
    && git config --system core.autocrlf false

WORKDIR /workspace
