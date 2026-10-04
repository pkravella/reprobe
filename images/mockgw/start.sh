#!/usr/bin/env bash
set -euo pipefail

# Resolve every name to ourselves, so a trial that ignores HTTP_PROXY and dials
# a hostname directly still lands here and still gets logged.
#
# `--address` is passed on the COMMAND LINE, not written to /etc/dnsmasq.d/.
# Debian's /etc/dnsmasq.conf ships every `conf-dir=` line commented out, so a
# file dropped in that directory is never read: measured, the sinkhole silently
# resolved nothing while dnsmasq reported a clean start. See docs/agents.md.
SELF="$(hostname -i | awk '{print $1}')"
if [ -z "$SELF" ]; then
    echo "mockgw: could not determine own address; refusing to start without a sinkhole" >&2
    exit 1
fi
echo "mockgw: sinkholing all DNS to ${SELF}" >&2

dnsmasq --keep-in-foreground --log-facility=- --address="/#/${SELF}" &
DNSMASQ_PID=$!

# A backgrounded dnsmasq that dies on startup would leave the gateway serving
# HTTP with no sinkhole, and a direct-hostname dial would then fail to resolve
# instead of being logged -- a missing observation that reads as a clean trial.
sleep 1
if ! kill -0 "$DNSMASQ_PID" 2>/dev/null; then
    echo "mockgw: dnsmasq exited immediately; the DNS sinkhole is not up" >&2
    exit 1
fi

exec python3 /opt/gateway.py
