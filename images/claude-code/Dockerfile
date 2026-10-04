FROM reprobe/base:dev
ARG CLAUDE_CODE_VERSION=latest
USER root
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}
USER agent
# Record the resolved version into the image so the adapter can read it offline.
# No `|| true`: every exported finding is pinned to this string (R11), so a
# failed probe must fail the build rather than pin the finding to an error
# message.
RUN claude --version > /home/agent/.reprobe-agent-version \
    && test -s /home/agent/.reprobe-agent-version \
    && cat /home/agent/.reprobe-agent-version
