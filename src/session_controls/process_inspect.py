"""Platform-specific process inspection.

Linux uses /proc/<pid>/{exe,stat,cmdline}.  The /proc/<pid>/stat parser is the
documented trap: the `comm` field is wrapped in parentheses but may itself
contain parentheses or whitespace, so a left-to-right split breaks.  We parse
from the closing paren of `comm` and treat everything between the first '('
and the last ')' as the literal name.

macOS uses libproc (proc_pidpath, proc_pidinfo with PROC_PIDTBSDINFO) and
sysctl with KERN_PROCARGS2 for the command-line.  These calls can fail by TCC
or by the inspecting process's launch context; failures are surfaced via
ProcessDescriptor.inspection_errors rather than silently dropped.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import platform
import struct
import subprocess
import time
from collections.abc import Iterable

from .identity import DescendantInfo, ProcessDescriptor

# ----- Linux ---------------------------------------------------------------

_CLK_TCK: float | None = None


def _clk_tck() -> float:
    global _CLK_TCK
    if _CLK_TCK is None:
        _CLK_TCK = float(os.sysconf("SC_CLK_TCK"))
    return _CLK_TCK


def _read_btime() -> float | None:
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except OSError:
        return None
    return None


def parse_proc_stat(raw: str) -> tuple[str, list[str]]:
    """Parse /proc/<pid>/stat. Returns (comm, fields_after_comm).

    Fields after `comm` are positional and 1-indexed in the kernel docs starting
    from `state` as field 3.  We return them as a list starting at index 0 ==
    field 3 (`state`).  So start_time (field 22) is fields_after_comm[19].

    The parser is right-to-left from the *last* ')' to handle `comm` values that
    contain parens/whitespace, e.g. "(weird)proc)" or "(my proc)".
    """
    open_paren = raw.find("(")
    close_paren = raw.rfind(")")
    if open_paren == -1 or close_paren == -1 or close_paren < open_paren:
        raise ValueError("malformed /proc/<pid>/stat: no comm parens")
    comm = raw[open_paren + 1 : close_paren]
    after = raw[close_paren + 1 :].split()
    return comm, after


def _read_linux(pid: int) -> ProcessDescriptor:
    errors: list[str] = []
    exe_path: str | None = None
    cmdline: tuple[str, ...] | None = None
    start_time: float | None = None
    ppid: int | None = None

    # exe
    try:
        exe_path = os.readlink(f"/proc/{pid}/exe")
    except OSError as e:
        errors.append(f"exe: {e.strerror or e}")

    # cmdline
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        if raw:
            parts = raw.split(b"\x00")
            cmdline = tuple(p.decode("utf-8", "replace") for p in parts if p)
    except OSError as e:
        errors.append(f"cmdline: {e.strerror or e}")

    # stat -> start_time + ppid
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat_raw = f.read()
        _, after = parse_proc_stat(stat_raw)
        # after[0] = state(3), so ppid is field 4 -> after[1], starttime field 22 -> after[19]
        if len(after) >= 20:
            ppid = int(after[1])
            ticks = int(after[19])
            btime = _read_btime()
            if btime is not None:
                start_time = btime + ticks / _clk_tck()
    except (OSError, ValueError) as e:
        errors.append(f"stat: {e}")

    return ProcessDescriptor(
        pid=pid,
        start_time=start_time,
        exe_path=exe_path,
        cmdline=cmdline,
        ppid=ppid,
        inspection_errors=tuple(errors),
    )


def linux_pid_namespace(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/ns/pid")
    except OSError:
        return None


# ----- macOS ---------------------------------------------------------------

# Constants from <sys/proc_info.h> and <sys/sysctl.h>.
_PROC_PIDPATHINFO_MAXSIZE = 4096
_PROC_PIDTBSDINFO = 3
_PROC_PIDTBSDINFO_SIZE = 232  # struct proc_bsdinfo
_CTL_KERN = 1
_KERN_PROCARGS2 = 49
# pbi_status zombie value from sys/proc.h
_SZOMB = 5


class _ProcBsdInfo(ctypes.Structure):
    # Subset of struct proc_bsdinfo we need.  We embed the full layout to size
    # correctly and read only the fields we use.
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


_libproc_lib = None
_libc_lib = None


def _libproc() -> ctypes.CDLL:
    global _libproc_lib
    if _libproc_lib is None:
        # libproc symbols are in libSystem on macOS; loading by name works.
        path = ctypes.util.find_library("proc") or "libproc.dylib"
        _libproc_lib = ctypes.CDLL(path, use_errno=True)
    return _libproc_lib


def _libc() -> ctypes.CDLL:
    global _libc_lib
    if _libc_lib is None:
        path = ctypes.util.find_library("c") or "libc.dylib"
        _libc_lib = ctypes.CDLL(path, use_errno=True)
    return _libc_lib


def _macos_pidpath(pid: int) -> tuple[str | None, str | None]:
    libproc = _libproc()
    libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    libproc.proc_pidpath.restype = ctypes.c_int
    buf = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
    ret = libproc.proc_pidpath(pid, buf, _PROC_PIDPATHINFO_MAXSIZE)
    if ret <= 0:
        e = ctypes.get_errno()
        return None, f"proc_pidpath: {os.strerror(e) if e else 'returned 0'}"
    return buf.value.decode("utf-8", "replace"), None


def _macos_bsdinfo(pid: int) -> tuple[_ProcBsdInfo | None, str | None]:
    libproc = _libproc()
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_pidinfo.restype = ctypes.c_int
    info = _ProcBsdInfo()
    ret = libproc.proc_pidinfo(
        pid,
        _PROC_PIDTBSDINFO,
        0,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if ret <= 0:
        e = ctypes.get_errno()
        return None, f"proc_pidinfo(PIDTBSDINFO): {os.strerror(e) if e else 'returned 0'}"
    if ret < ctypes.sizeof(info):
        return None, f"proc_pidinfo(PIDTBSDINFO): short read ({ret} of {ctypes.sizeof(info)})"
    return info, None


def _macos_argv(pid: int) -> tuple[tuple[str, ...] | None, str | None]:
    """Read argv via sysctl(KERN_PROCARGS2).

    Layout of the returned buffer:
      uint32_t argc
      <executable path, NUL-terminated, then optionally padded with NULs>
      argv[0]\0 argv[1]\0 ... argv[argc-1]\0
      env[0]\0 env[1]\0 ...
    """
    libc = _libc()
    libc.sysctl.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    libc.sysctl.restype = ctypes.c_int

    # KERN_PROCARGS2 sizing isn't portable; allocate a generous buffer.
    bufsize = 1 << 20  # 1 MiB
    buf = ctypes.create_string_buffer(bufsize)
    size = ctypes.c_size_t(bufsize)
    name = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
    ret = libc.sysctl(name, 3, buf, ctypes.byref(size), None, 0)
    if ret != 0:
        e = ctypes.get_errno()
        return None, f"sysctl(KERN_PROCARGS2): {os.strerror(e) if e else 'unknown'}"
    raw = buf.raw[: size.value]
    if len(raw) < 4:
        return None, "sysctl(KERN_PROCARGS2): short buffer"
    (argc,) = struct.unpack("I", raw[:4])
    rest = raw[4:]
    # Skip the executable path + any padding NULs.
    # First find first NUL (end of exe path).
    first_nul = rest.find(b"\x00")
    if first_nul == -1:
        return None, "sysctl(KERN_PROCARGS2): missing exe NUL"
    i = first_nul
    # Skip padding NULs
    while i < len(rest) and rest[i] == 0:
        i += 1
    # Now read argc strings
    argv: list[str] = []
    for _ in range(argc):
        end = rest.find(b"\x00", i)
        if end == -1:
            break
        argv.append(rest[i:end].decode("utf-8", "replace"))
        i = end + 1
    return tuple(argv), None


def _read_macos(pid: int) -> ProcessDescriptor:
    errors: list[str] = []
    exe_path, err = _macos_pidpath(pid)
    if err:
        errors.append(err)
    info, err = _macos_bsdinfo(pid)
    if err:
        errors.append(err)
    start_time: float | None = None
    ppid: int | None = None
    if info is not None:
        start_time = float(info.pbi_start_tvsec) + float(info.pbi_start_tvusec) / 1_000_000.0
        ppid = int(info.pbi_ppid)
    argv, err = _macos_argv(pid)
    if err:
        errors.append(err)

    return ProcessDescriptor(
        pid=pid,
        start_time=start_time,
        exe_path=exe_path,
        cmdline=argv,
        ppid=ppid,
        inspection_errors=tuple(errors),
    )


# ----- Public API ----------------------------------------------------------


def inspect(pid: int) -> ProcessDescriptor:
    """Return a ProcessDescriptor for `pid`. Inspection errors are reported
    via the descriptor's `inspection_errors` field rather than raised."""
    system = platform.system()
    if system == "Linux":
        return _read_linux(pid)
    if system == "Darwin":
        return _read_macos(pid)
    return ProcessDescriptor(
        pid=pid,
        start_time=None,
        exe_path=None,
        cmdline=None,
        ppid=None,
        inspection_errors=(f"unsupported platform: {system}",),
    )


def is_alive(pid: int) -> bool:
    """True iff `pid` exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        # EPERM means it exists but we can't signal it; anything else: treat as gone.
        return e.errno == errno.EPERM
    system = platform.system()
    if system == "Linux":
        try:
            with open(f"/proc/{pid}/stat") as f:
                _, after = parse_proc_stat(f.read())
            if after and after[0] == "Z":
                return False
        except (OSError, ValueError):
            pass
    elif system == "Darwin":
        info, _err = _macos_bsdinfo(pid)
        if info is not None and int(info.pbi_status) == _SZOMB:
            return False
    return True


def walk_ancestry(pid: int, max_depth: int = 32) -> Iterable[ProcessDescriptor]:
    """Yield process descriptors from `pid` up through ancestors (skipping pid 0/1)."""
    seen: set[int] = set()
    cur: int | None = pid
    depth = 0
    while cur and cur > 1 and cur not in seen and depth < max_depth:
        seen.add(cur)
        desc = inspect(cur)
        yield desc
        cur = desc.ppid
        depth += 1


# Known harness-spawned processes — Claude Code internals that aren't
# user-initiated work and would be noise in the descendants list. Matched
# on exe basename. Conservative — only entries we're confident are
# harness-only on the platforms we support. Anything not on this list
# stays visible (the description tells Claude to surface uncertain
# entries to the user rather than guess).
_HARNESS_PROCESS_NAMES: frozenset[str] = frozenset({"caffeinate"})


def list_descendants(target_pid: int, exclude_pid: int) -> list[DescendantInfo]:
    """Return descendants of `target_pid` (recursive), minus `exclude_pid`'s subtree
    and minus known-harness processes.

    Used to surface processes that would be orphaned by an end_session call —
    sibling MCP servers, run_in_background bash jobs, sub-agent processes.
    `exclude_pid` is typically our own server PID: our subtree dies with the
    parent (stdio EOF) and our sacrificial children are short-lived, so they're
    noise rather than orphan candidates.

    Each surviving descendant is wrapped in a `DescendantInfo` carrying its
    `depth` (BFS hops from target — direct children are depth=1) and
    `uptime_seconds` (time.time() − start_time, or None when start_time
    wasn't readable). These let Claude attribute the descendant — direct
    children spawned during the session look different from grandchildren
    of long-running user-managed work.

    Known-harness filtering: processes spawned by Claude Code itself for its
    own purposes (e.g. `caffeinate` to prevent sleep on macOS) are dropped
    so they don't muddle the user-vs-harness attribution. The filter is
    conservative — anything not on the allowlist stays visible.

    Reads `ps -A -o pid=,ppid=` for cross-platform simplicity. Returns an empty
    list if ps is unavailable or fails.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-A", "-o", "pid=,ppid="],
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            child = int(parts[0])
            parent = int(parts[1])
        except ValueError:
            continue
        children.setdefault(parent, []).append(child)

    # Walk exclude_pid's subtree — those are our own processes, not orphans.
    excluded: set[int] = set()
    stack = [exclude_pid]
    while stack:
        cur = stack.pop()
        if cur in excluded:
            continue
        excluded.add(cur)
        stack.extend(children.get(cur, []))

    # BFS from target_pid so we naturally accumulate hop-depth as we go.
    depths: dict[int, int] = {}
    queue: list[tuple[int, int]] = [(c, 1) for c in children.get(target_pid, [])]
    while queue:
        pid, depth = queue.pop(0)
        if pid in depths or pid in excluded:
            continue
        depths[pid] = depth
        for c in children.get(pid, []):
            queue.append((c, depth + 1))

    now = time.time()
    results: list[DescendantInfo] = []
    for pid, depth in depths.items():
        descriptor = inspect(pid)
        # Drop known-harness processes by exe basename. A descriptor whose
        # exe_path is unreadable (None) stays visible — better to surface an
        # ambiguous entry than to hide it on incomplete information.
        if _basename_of(descriptor.exe_path) in _HARNESS_PROCESS_NAMES:
            continue
        uptime = (now - descriptor.start_time) if descriptor.start_time is not None else None
        results.append(DescendantInfo(descriptor=descriptor, depth=depth, uptime_seconds=uptime))
    return results


def _basename_of(path: str | None) -> str:
    if not path:
        return ""
    return os.path.basename(path)
