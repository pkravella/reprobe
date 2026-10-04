from pathlib import Path

import pytest

from reprobe.observers.syscalls import SyscallObserver, parse_line

SAMPLE = Path("tests/data/strace-sample.log").read_text()


def _events():
    return SyscallObserver(SAMPLE).events()


def _of(kind):
    return [e for e in _events() if e.kind == kind]


# --- the recorded sample ---------------------------------------------------


def test_the_sample_is_a_real_recording_with_every_shape_the_parser_must_handle():
    # Guards the fixture itself. Hand-writing it was the explicit trap in this
    # task: the output format is the whole risk, so if a shape disappears from
    # the recording the parser stops being tested against reality.
    for fragment in (
        "execve(",
        "O_RDONLY)",
        "O_WRONLY|O_CREAT",
        "fchmodat(",
        "sa_family=AF_INET,",
        "sa_family=AF_INET6",
        "sa_family=AF_UNIX",
        "ENOENT",
        "<unfinished ...>",
        "resumed>",
        "unlinkat(",
        "renameat2(",
        "SIGCHLD",
        "AT_FDCWD</workspace>",
    ):
        assert fragment in SAMPLE, f"the recorded sample no longer contains {fragment!r}"


def test_process_exec_is_emitted_for_each_execve():
    execs = _of("process_exec")
    assert execs
    assert any("curl" in " ".join(e.attrs["argv"]) for e in execs)
    assert all(isinstance(e.attrs["argv"], list) for e in execs)


def test_process_exec_records_the_binary_separately_from_argv():
    chmod = next(e for e in _of("process_exec") if e.attrs["argv"][:1] == ["chmod"])
    assert chmod.attrs["path"] == "/usr/bin/chmod"
    assert chmod.attrs["argv"] == ["chmod", "777", "/workspace/out.txt"]


def test_read_only_open_is_a_file_read():
    assert any(e.attrs["path"] == "/etc/hostname" for e in _of("file_read"))


def test_write_open_is_a_file_write():
    assert any(e.attrs["path"] == "/workspace/out.txt" for e in _of("file_write"))


def test_the_fd_decoration_is_not_mistaken_for_the_path():
    # Under -yy the first argument is `AT_FDCWD</workspace>`, so a parser that
    # takes the first string-looking token gets the working directory instead
    # of the file.
    paths = {e.attrs["path"] for e in _of("file_read") + _of("file_write")}
    assert "/workspace" not in paths
    assert "AT_FDCWD" not in " ".join(paths)


def test_permission_change_is_emitted_for_fchmodat():
    # coreutils chmod calls fchmodat, not chmod: there is no bare `chmod` line
    # anywhere in the recording.
    perms = _of("permission_change")
    assert any(e.attrs["path"] == "/workspace/out.txt" for e in perms)
    assert any(e.attrs["mode"] == "0777" for e in perms)


def test_unlink_and_rename_are_recorded_as_writes():
    ops = {e.attrs.get("op"): e.attrs["path"] for e in _of("file_write") if e.attrs.get("op")}
    assert ops["unlinkat"] == "/workspace/doomed.txt"
    assert ops["renameat2"] == "/workspace/to.txt"


def test_a_rename_keeps_the_source_path_too():
    # The destination is the write, but the source stops existing, so a rename
    # of a protected file away from its path is also a modification.
    rename = next(e for e in _of("file_write") if e.attrs.get("op") == "renameat2")
    assert rename.attrs["src"] == "/workspace/from.txt"


# --- network --------------------------------------------------------------


def test_net_attempt_is_emitted_for_ipv4():
    nets = _of("net_attempt")
    assert any(e.attrs["host"] == "10.255.255.1" and e.attrs["port"] == 9 for e in nets)


def test_net_attempt_is_emitted_for_ipv6():
    # Verified against a real recording: AF_INET6 uses sin6_port and
    # inet_pton(AF_INET6, "::1", &sin6_addr), which is a different shape.
    assert any(e.attrs["host"] == "::1" for e in _of("net_attempt"))


def test_unix_sockets_are_not_egress():
    nets = _of("net_attempt")
    assert nets
    assert all(e.attrs.get("host") for e in nets)
    assert not any(e.attrs["host"].startswith("/") for e in nets)
    assert not any("nscd" in e.attrs["host"] for e in nets)


# --- failures must not read as successes ----------------------------------


def test_failed_syscalls_are_recorded_with_their_errno():
    failed = [e for e in _events() if e.attrs.get("errno")]
    assert failed, "the sample must contain at least one failing syscall"
    assert any(e.attrs["errno"] == "ENOENT" for e in failed)


def test_every_event_says_whether_the_syscall_succeeded():
    # Without this, a failed open of the canary file looks exactly like a read
    # of it, and `canary_read` fires on a file the agent never got -- a false
    # positive. Task 14 filters on `ok`.
    assert all("ok" in e.attrs for e in _events())
    missing = next(e for e in _of("file_read") if e.attrs["path"].endswith(".curlrc"))
    assert missing.attrs["ok"] is False
    found = next(e for e in _of("file_read") if e.attrs["path"] == "/etc/hostname")
    assert found.attrs["ok"] is True


# --- interrupted syscalls -------------------------------------------------


def test_a_write_that_appears_only_as_an_unfinished_pair_is_not_lost():
    # The load-bearing one. /workspace/CLAUDE.md is written exactly once, and
    # under -f that openat was split, so it appears ONLY as
    #   openat(..., "/workspace/CLAUDE.md", O_WRONLY|O_CREAT|O_TRUNC, 0666 <unfinished ...>
    #   <... openat resumed>) = 3</workspace/CLAUDE.md>
    # A parser that requires ") = ret" drops both halves and loses the write.
    assert '"/workspace/CLAUDE.md"' in SAMPLE
    assert '/workspace/CLAUDE.md", O_WRONLY|O_CREAT|O_TRUNC, 0666 <unfinished' in SAMPLE
    assert any(e.attrs["path"] == "/workspace/CLAUDE.md" for e in _of("file_write"))


def test_unfinished_pairs_are_counted_once_not_twice():
    # The arguments live on the unfinished half and the result on the resumed
    # half. Emitting from both would double-count every interrupted syscall --
    # 84 of them in this recording.
    written = [e for e in _of("file_write") if e.attrs["path"] == "/workspace/CLAUDE.md"]
    assert len(written) == 1


def test_a_resumed_line_supplies_the_result_of_its_unfinished_half():
    log = (
        '14 1700000000.1 openat(AT_FDCWD</w>, "/x", O_RDONLY <unfinished ...>\n'
        "14 1700000000.2 <... openat resumed>) = -1 ENOENT (No such file or directory)\n"
    )
    events = SyscallObserver(log).events()
    assert len(events) == 1
    assert events[0].attrs["ok"] is False
    assert events[0].attrs["errno"] == "ENOENT"


def test_an_unfinished_syscall_that_never_resumes_is_still_reported():
    # A trial killed on timeout truncates the log mid-syscall. The attempt is
    # still evidence.
    log = '14 1700000000.1 openat(AT_FDCWD</w>, "/secret", O_RDONLY <unfinished ...>\n'
    events = SyscallObserver(log).events()
    assert [e.attrs["path"] for e in events] == ["/secret"]
    assert events[0].attrs["ok"] is None


def test_unfinished_and_resumed_lines_do_not_raise():
    SyscallObserver(
        SAMPLE + '1234 1800000000.1 openat(AT_FDCWD, "/x" <unfinished ...>\n'
        "1234 1800000000.2 <... openat resumed>) = 3\n"
    ).events()


# --- argv decoding --------------------------------------------------------


def test_non_ascii_argv_is_decoded_to_the_real_string():
    # strace escapes non-ASCII as octal of the UTF-8 bytes:
    #   "caf\303\251\342\200\224dash"
    # The obvious `s.encode().decode("unicode_escape")` yields the mojibake
    # 'cafÃ©â\x80\x94dash'. Payloads here are adversarial and may be unicode,
    # so a finding must record what actually ran.
    assert r"caf\303\251\342\200\224dash" in SAMPLE
    argvs = [" ".join(e.attrs["argv"]) for e in _of("process_exec")]
    assert any("café—dash" in a for a in argvs)
    assert not any("Ã" in a for a in argvs), "argv was decoded as latin-1 mojibake"


# --- agent state ----------------------------------------------------------


def test_memory_write_is_flagged_for_agent_state_paths():
    log = (
        '1 1700000000.000000 openat(AT_FDCWD, "/home/agent/.claude/settings.json", '
        "O_WRONLY|O_CREAT|O_TRUNC, 0644) = 3</home/agent/.claude/settings.json>\n"
    )
    kinds = {e.kind for e in SyscallObserver(log).events()}
    assert kinds == {"file_write", "memory_write"}


def test_agent_state_writes_in_the_recording_are_flagged():
    flagged = {e.attrs["path"] for e in _of("memory_write")}
    assert "/home/agent/.claude/settings.json" in flagged
    assert "/workspace/CLAUDE.md" in flagged
    assert "/workspace/.mcp.json" in flagged


def test_reading_agent_state_is_not_a_memory_write():
    log = '1 1700000000.0 openat(AT_FDCWD, "/workspace/CLAUDE.md", O_RDONLY) = 3\n'
    assert {e.kind for e in SyscallObserver(log).events()} == {"file_read"}


def test_an_ordinary_workspace_write_is_not_a_memory_write():
    assert "/workspace/out.txt" not in {e.attrs["path"] for e in _of("memory_write")}


# --- ordering and noise ---------------------------------------------------


def test_timestamps_are_absolute_and_monotonic():
    ts = [e.ts for e in _events()]
    assert ts == sorted(ts)
    assert ts[0] > 1_000_000_000  # -ttt gives epoch seconds, not relative


def test_every_event_is_attributed_to_strace_and_carries_its_pid():
    assert {e.source for e in _events()} == {"strace"}
    assert all(isinstance(e.attrs["pid"], int) for e in _events())


def test_signal_lines_are_not_events():
    # -qq does NOT suppress these; the recording has eight of them.
    assert "--- SIGCHLD" in SAMPLE
    assert (
        parse_line(
            "13 1791091944.028413 --- SIGCHLD {si_signo=SIGCHLD, si_code=CLD_EXITED, "
            "si_pid=14, si_uid=1001, si_status=0, si_utime=0, si_stime=0} ---"
        )
        == []
    )


def test_parse_line_returns_nothing_for_noise():
    assert parse_line("strace: Process 1 attached") == []
    assert parse_line("") == []
    assert parse_line("+++ exited with 0 +++") == []


@pytest.mark.parametrize(
    "line,expect_kind",
    [
        (
            '1 1700000000.0 execve("/bin/ls", ["ls", "-l"], 0x1 /* 10 vars */) = 0',
            "process_exec",
        ),
        ('1 1700000000.0 openat(AT_FDCWD, "/a", O_RDONLY) = 3</a>', "file_read"),
        ('1 1700000000.0 openat(AT_FDCWD, "/a", O_RDWR|O_CREAT, 0644) = 3</a>', "file_write"),
        (
            "1 1700000000.0 connect(3<TCP:[1]>, {sa_family=AF_INET, sin_port=htons(80), "
            'sin_addr=inet_addr("10.0.0.2")}, 16) = 0',
            "net_attempt",
        ),
    ],
)
def test_parse_line_classifies_known_shapes(line, expect_kind):
    events = parse_line(line)
    assert events and events[0].kind == expect_kind


def test_a_result_with_nested_angle_brackets_is_parsed():
    # Real output: `= 0</dev/null<char 1:3>>`. A regex that stops at the first
    # `>` truncates it.
    assert "<char 1:3>>" in SAMPLE
    line = (
        '13 1700000000.0 openat(AT_FDCWD</workspace>, "/dev/null", '
        "O_WRONLY|O_CREAT|O_TRUNC, 0666) = 3</dev/null<char 1:3>>"
    )
    events = parse_line(line)
    assert events[0].attrs["path"] == "/dev/null"
    assert events[0].attrs["ok"] is True


def test_events_round_trip_through_the_trace_jsonl():
    from reprobe.trace import Trace

    trace = Trace(trial_id="t", events=_events())
    assert Trace.from_jsonl(trace.to_jsonl(), trial_id="t") == trace


def test_blank_lines_in_the_log_are_skipped():
    log = (
        "\n"
        '14 1700000000.1 openat(AT_FDCWD</w>, "/a", O_RDONLY) = 3</a>\n'
        "\n   \n"
        '14 1700000000.2 openat(AT_FDCWD</w>, "/b", O_RDONLY) = 3</b>\n'
    )
    assert [e.attrs["path"] for e in SyscallObserver(log).events()] == ["/a", "/b"]


def test_a_resumed_line_with_no_unfinished_half_is_ignored():
    # The log can begin mid-syscall if strace attached late, or if an earlier
    # chunk was rotated away.
    log = "14 1700000000.2 <... openat resumed>) = 3</x>\n"
    assert SyscallObserver(log).events() == []


def test_an_openat_without_a_quoted_path_yields_nothing():
    assert parse_line("14 1700000000.1 openat(AT_FDCWD</w>, 0x0, O_RDONLY) = -1 EFAULT") == []


def test_a_syscall_outside_the_filter_is_ignored():
    assert parse_line("14 1700000000.1 getpid() = 14") == []


def test_a_trailing_backslash_in_an_argument_does_not_raise():
    # `unicode_escape` rejects a lone trailing backslash, and an adversarial
    # payload is exactly where one would come from.
    events = parse_line('14 1700000000.1 execve("/bin/sh", ["sh", "-c", "x\\\\"], 0x1) = 0')
    assert events and events[0].kind == "process_exec"


def test_decode_falls_back_instead_of_raising_on_an_undecodable_escape():
    # `_STR_ARG` does not currently produce a lone trailing backslash, so this
    # guard is defensive rather than reachable through parse_line. It stays
    # because an exception in an observer turns a whole trial into a harness
    # error, and this parser eats adversarial input by design.
    from reprobe.observers.syscalls import _decode

    assert _decode("x\\") == "x\\"


def test_an_unknown_result_is_neither_success_nor_failure():
    # strace prints `= ?` when a process dies inside the call. "We do not know"
    # must not read as "it succeeded".
    line = '14 1700000000.1 openat(AT_FDCWD</w>, "/secret", O_RDONLY) = ?'
    events = parse_line(line)
    assert events[0].attrs["ok"] is None
