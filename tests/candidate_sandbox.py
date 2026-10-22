"""Linux/amd64 verification sandbox; setup failure is fatal, never a fallback."""
import ctypes
import errno
import os
from pathlib import Path
import platform
import resource

def restrict(source, scratch, public_inputs):
    if os.geteuid() != 0 or platform.machine() != 'x86_64':
        raise RuntimeError('Verifier requires root bootstrap in a Linux/amd64 container')
    if Path(source).is_symlink() or str(Path(source).resolve()) != source:
        raise RuntimeError('Candidate source directory must not be a symlink')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise RuntimeError('Landlock ABI >= 3 is required for candidate isolation')
    READ = (1 << 0) | (1 << 2) | (1 << 3)
    ALL = (1 << 15) - 1
    if abi >= 5:
        ALL |= 1 << 15
    class Ruleset(ctypes.Structure):
        _fields_ = [('handled_access_fs', ctypes.c_uint64)]
    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]
    attr = Ruleset(ALL)
    rules = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if rules < 0:
        raise OSError(ctypes.get_errno(), 'landlock_create_ruleset')
    def allow(path, rights):
        path = Path(path)
        if not path.exists():
            return
        if not path.is_dir():
            rights &= ~((1 << 3) | sum(1 << i for i in range(4, 14)))
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = PathRule(rights, fd)
            if libc.syscall(445, rules, 1, ctypes.byref(rule), 0) != 0:
                raise OSError(ctypes.get_errno(), 'landlock_add_rule: ' + str(path))
        finally:
            os.close(fd)
    for path in ('/usr', '/lib', '/lib64', '/bin', '/etc/ld.so.cache', '/etc/localtime'):
        allow(path, READ)
    allow(source, READ)
    allow(public_inputs, READ)
    allow(scratch, ALL)
    allow('/dev/null', (1 << 1) | (1 << 2))
    allow('/dev/urandom', 1 << 2)
    # No tests, solution, logs, parent process /proc, host mounts, or sockets.
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'PR_SET_NO_NEW_PRIVS')
    if libc.syscall(446, rules, 0) != 0:
        raise OSError(ctypes.get_errno(), 'landlock_restrict_self')
    os.close(rules)
    # BPF avoids any system libseccomp dependency. Check architecture before
    # syscall numbers; deny x32 ABI, networking, tracing, signals to other
    # processes and namespace/privilege escapes. Threads remain available.
    class Filter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]
    class Program(ctypes.Structure):
        _fields_ = [('len', ctypes.c_ushort), ('filter', ctypes.POINTER(Filter))]
    denied = [41, 42, 43, 49, 50, 53, 62, 101, 109, 112, 155, 161, 165, 166,
              200, 234, 246, 248, 249, 250, 272, 297, 298, 303,
              304, 308, 310, 311, 321, 323, 424, 425, 426, 427, 438]
    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xc000003e),
                    (0x06, 0, 0, 0x80000000), (0x20, 0, 0, 0),
                    (0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000)]
    for number in denied:
        instructions.extend([(0x15, 0, 1, number), (0x06, 0, 0, 0x50000 | errno.EPERM)])
    instructions.append((0x06, 0, 0, 0x7fff0000))
    filters = (Filter * len(instructions))(*(Filter(*x) for x in instructions))
    program = Program(len(instructions), filters)
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'PR_SET_SECCOMP')
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    os.chdir(scratch)
    return int(abi)
