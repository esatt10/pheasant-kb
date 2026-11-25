"""Kernel isolation for the trusted parser program, before it reads publisher bytes."""

from __future__ import annotations

import ctypes
import errno
import sys

# The worker receives only stdin/stdout/stderr. Disallow all path resolution,
# sockets, execution, descriptor duplication and creation by default.
_ALLOWED = """
read write close fstat lseek brk mmap mmap2 mremap munmap mprotect madvise
futex futex_time64 rseq getrandom getpid gettid getrusage clock_gettime
clock_gettime64 sched_yield rt_sigaction rt_sigprocmask rt_sigreturn
sigaltstack restart_syscall exit exit_group
""".split()


def confine() -> None:
    if sys.platform != "linux":
        raise RuntimeError("sandbox requires Linux")

    import resource

    resource.setrlimit(resource.RLIMIT_AS, (320 * 1024**2, 320 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (7, 8))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    # PR_SET_NO_NEW_PRIVS is required before an unprivileged seccomp filter.
    if libc.prctl(38, 1, 0, 0, 0):
        raise RuntimeError("could not disable privilege escalation")
    seccomp = ctypes.CDLL("libseccomp.so.2")
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    # SCMP_ACT_ERRNO(EPERM) and SCMP_ACT_ALLOW.
    context = seccomp.seccomp_init(0x00050000 | errno.EPERM)
    if not context:
        raise RuntimeError("could not initialize syscall filter")
    try:
        for syscall in _ALLOWED:
            number = seccomp.seccomp_syscall_resolve_name(syscall.encode("ascii"))
            if number >= 0 and seccomp.seccomp_rule_add(context, 0x7FFF0000, number, 0):
                raise RuntimeError("could not restrict parser system calls")
        if seccomp.seccomp_load(context):
            raise RuntimeError("could not activate parser syscall filter")
    finally:
        seccomp.seccomp_release(context)
