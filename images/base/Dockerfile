# The agent-agnostic sandbox base.
#
# node:22-bookworm-slim, not debian:bookworm-slim + apt nodejs:
# @anthropic-ai/claude-code 2.1.289 declares `engines: {node: ">=22.0.0"}` and
# Debian bookworm ships Node 18.20.4. npm installs anyway with only an
# EBADENGINE warning, and the CLI was measured working on 18 -- so this is a
# latent risk, not a break. It is still the wrong runtime to take into a gate
# that requires zero harness failures over 100 trials. Still Debian 12, so
# every apt package below is the one the plan intended.
FROM node:26-bookworm-slim

# strace: the syscall observer (Task 10). git/curl/jq/python3: what a coding
# task needs. build-essential: native npm/pip deps. ca-certificates: so TLS to
# the mock gateway works with its injected CA.
RUN apt-get update && apt-get install -y --no-install-recommends \
        strace git curl ca-certificates jq python3 python3-venv \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# An unprivileged user, so a trial cannot trivially escalate inside the box.
RUN useradd --create-home --shell /bin/bash agent

# Both directories must be writable by `agent`, who is not root. /reprobe holds
# the strace log the entrypoint writes; /workspace is where Task 8 materialises
# the fixture. Without the chown the entrypoint's `mkdir -p` fails, strace
# cannot open its output file, and the agent never runs.
RUN mkdir -p /reprobe /workspace && chown agent:agent /reprobe /workspace

WORKDIR /workspace
COPY entrypoint.sh /usr/local/bin/reprobe-entrypoint
RUN chmod 0755 /usr/local/bin/reprobe-entrypoint
USER agent
ENTRYPOINT ["/usr/local/bin/reprobe-entrypoint"]
