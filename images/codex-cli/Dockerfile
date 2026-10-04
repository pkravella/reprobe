FROM reprobe/base:dev
ARG CODEX_VERSION=latest
USER root
RUN npm install -g @openai/codex@${CODEX_VERSION}
USER agent
# See images/claude-code/Dockerfile for why this must not swallow a failure.
RUN codex --version > /home/agent/.reprobe-agent-version \
    && test -s /home/agent/.reprobe-agent-version \
    && cat /home/agent/.reprobe-agent-version
