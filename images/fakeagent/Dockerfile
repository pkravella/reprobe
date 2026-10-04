FROM reprobe/base:dev
USER root
COPY agent.py /opt/reprobe-fake-agent.py
RUN printf '#!/bin/sh\nexec python3 /opt/reprobe-fake-agent.py "$@"\n' \
      > /usr/local/bin/reprobe-fake-agent \
    && chmod 0755 /usr/local/bin/reprobe-fake-agent
USER agent
# This lane pins a finding to the Reprobe version, not a vendor's. ARG so a
# release build can stamp the real version; dev otherwise.
ARG REPROBE_VERSION=dev
RUN echo "$REPROBE_VERSION" > /home/agent/.reprobe-agent-version
