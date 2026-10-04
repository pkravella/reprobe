#!/usr/bin/env bash
# Runs the agent under strace and writes a syscall log the host will parse.
# "$@" is the agent command line the adapter produced.
#
# No capability is added for this. strace forks the agent itself and traces a
# direct child via PTRACE_TRACEME, which standard permissions already allow --
# CAP_SYS_PTRACE is only needed to attach to a process you do not own. Measured
# working under `--cap-drop ALL`; see docs/agents.md.
set -uo pipefail

if [ "$#" -eq 0 ]; then
    echo "reprobe-entrypoint: no agent command given" >&2
    exit 64
fi

TRACE_OUT="${REPROBE_STRACE_OUT:-/reprobe/strace.log}"
if ! mkdir -p "$(dirname "$TRACE_OUT")"; then
    echo "reprobe-entrypoint: cannot create $(dirname "$TRACE_OUT")" >&2
    exit 73
fi

# -f   follow forks (the agent shells out constantly)
# -qq  suppress process-exit chatter
# -yy  decode fds and socket addresses to paths/IPs (what makes the log usable)
# -s   keep enough of each string argument to see a path
# -ttt absolute timestamps, so events interleave with gateway and agent events
exec strace -f -qq -yy -ttt -s 256 \
    -e trace=execve,execveat,openat,open,connect,chmod,fchmod,fchmodat,unlink,unlinkat,rename,renameat,renameat2 \
    -o "$TRACE_OUT" \
    -- "$@"
