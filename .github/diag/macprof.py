"""macOS runner profiling for the Blacksmith/Tenki A/B (benchmark fork only).

Subcommands: hostinfo, cpubench, spawnbench, diskbench DIR, netbench, sampler OUTDIR, summarize OUTDIR.
Output is public: IPs, serials and UUIDs are never printed (hostnames are masked).
"""
import ctypes
import ctypes.util
import fcntl
import json
import multiprocessing as mp
import os
import re
import shutil
import socket
import sqlite3
import statistics as st
import subprocess
import sys
import tempfile
import time

UUID = re.compile(r"[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
IP = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")


def mask(s):
    return IP.sub("<ip>", UUID.sub("<uuid>", s))


def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return mask((r.stdout + r.stderr).strip())
    except subprocess.TimeoutExpired:
        return f"<timeout {timeout}s>"


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(q / 100 * (len(v) - 1))))]


def lat(name, v, unit=1e6, u="us"):
    print(f"  {name:38s} n={len(v):4d}  p50 {pct(v,50)*unit:9.1f}{u}  p90 {pct(v,90)*unit:9.1f}{u}"
          f"  p99 {pct(v,99)*unit:9.1f}{u}  mean {st.mean(v)*unit:9.1f}{u}")


# ---------------------------------------------------------------- hostinfo
def hostinfo():
    keys = ["machdep.cpu.brand_string", "hw.model", "hw.ncpu", "hw.perflevel0.physicalcpu",
            "hw.perflevel0.name", "hw.perflevel1.physicalcpu", "hw.memsize", "kern.osproductversion",
            "kern.osversion", "vm.swapusage", "vm.loadavg", "kern.maxproc", "kern.maxfiles"]
    for k in keys:
        print(f"{k}: {sh('sysctl -n ' + k)}")
    boot = sh("sysctl -n kern.boottime")
    m = re.search(r"sec = (\d+)", boot)
    if m:
        print(f"uptime_s: {time.time() - int(m.group(1)):.0f}")
    print(f"hostname: {mask(socket.gethostname())}")
    print(f"memory_pressure: {sh('memory_pressure -Q')}")
    print(f"sudo -n: {'yes' if subprocess.run('sudo -n true', shell=True).returncode == 0 else 'no'}")
    print(f"thermal: {sh('pmset -g therm')}")
    print(f"spotlight /: {sh('mdutil -s / 2>&1 | tail -1')}")
    print(f"spotlight data: {sh('mdutil -s /System/Volumes/Data 2>&1 | tail -1')}")
    print("system extensions:\n" + sh("systemextensionsctl list 2>&1 | tail -n +2 | cut -c1-140"))
    print("python sqlite: " + sqlite3.sqlite_version)
    # git identity auto-detection (Tenki VM hostnames carry no domain)
    print("git default ident: " + sh("cd /tmp && env -u GIT_AUTHOR_EMAIL -u GIT_COMMITTER_EMAIL "
                                       "HOME=/nonexistent git var GIT_COMMITTER_IDENT 2>&1 | sed -E 's/[0-9]{9,}.*//'"))
    for label, code in (("getaddrinfo(own hostname)", "import socket;socket.getaddrinfo(socket.gethostname(),None)"),
                        ("getfqdn()", "import socket;socket.getfqdn()"),
                        ("getaddrinfo(localhost)", "import socket;socket.getaddrinfo('localhost',None)")):
        t0 = time.perf_counter()
        try:
            r = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, timeout=20)
            res = "ok" if r.returncode == 0 else "error"
        except subprocess.TimeoutExpired:
            res = "timeout"
        print(f"{label}: {res} {time.perf_counter() - t0:.3f}s")
    print("idle top processes (pcpu, rss KB, comm):")
    print(sh("ps -Ao pcpu=,rss=,comm= -r | head -15 | sed -E 's#  +# #g; s#/[^ ]*/##'"))
    if subprocess.run("sudo -n true", shell=True).returncode == 0:
        print("powermetrics:\n" + sh("sudo -n powermetrics -n 1 -i 500 --samplers cpu_power 2>&1 | head -40", 20))


# ---------------------------------------------------------------- cpubench
ITER = int(os.environ.get("PROF_ITER", "20000000"))


def _cpu_worker(barrier, q, n):
    barrier.wait()
    t0 = time.perf_counter()
    c0 = time.process_time()
    x = 1
    for _ in range(n):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
    q.put((time.perf_counter() - t0, time.process_time() - c0))


def cpubench():
    ncpu = os.cpu_count()
    counts = sorted({1, 2, 4, 6, 8, 10, 12, 14, 16, ncpu})
    ctx = mp.get_context("spawn")
    print(f"pure-Python LCG loop, {ITER/1e6:.0f}M iterations per worker; per-worker rate in M iter/s")
    print(f"{'workers':>7} {'median':>7} {'slowest':>7} {'fastest':>7} {'aggregate':>9} {'vs 1':>6}  per-worker rates (sorted)")
    base = None
    res = {}
    for n in counts + [1]:
        rates = []
        for _rep in range(2):
            barrier, q = ctx.Barrier(n), ctx.Queue()
            ps = [ctx.Process(target=_cpu_worker, args=(barrier, q, ITER)) for _ in range(n)]
            for p in ps:
                p.start()
            out = [q.get() for _ in ps]
            for p in ps:
                p.join()
            rates.append(sorted(ITER / w / 1e6 for w, _c in out))
        best = max(rates, key=lambda r: sum(r))  # the less disturbed repetition
        med = st.median(best)
        base = base or med
        res[n] = best
        print(f"{n:7d} {med:7.2f} {best[0]:7.2f} {best[-1]:7.2f} {sum(best):9.1f} {med/base:6.2f}  "
              + " ".join(f"{r:.1f}" for r in best))
    print("CPUBENCH_JSON " + json.dumps({str(k): v for k, v in res.items()}))


# ---------------------------------------------------------------- spawnbench
def _timeit(fn, n):
    out = []
    for i in range(n):
        t0 = time.perf_counter()
        fn(i)
        out.append(time.perf_counter() - t0)
    return out


def _spawn_many(args):
    n, = args
    t0 = time.perf_counter()
    for _ in range(n):
        subprocess.run(["/usr/bin/true"])
    return time.perf_counter() - t0


def spawnbench():
    d = tempfile.mkdtemp(prefix="spawn-", dir=os.environ.get("PROF_DIR", "/var/tmp"))
    run = lambda *a, **k: subprocess.run(*a, capture_output=True, **k)  # noqa: E731
    print("serial, one process at a time:")
    lat("exec /usr/bin/true", _timeit(lambda i: run(["/usr/bin/true"]), 300), 1e3, "ms")
    lat("bash -c :", _timeit(lambda i: run(["/bin/bash", "-c", ":"]), 200), 1e3, "ms")

    def fresh(i):
        p = os.path.join(d, f"s{i}.sh")
        with open(p, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(p, 0o755)
        run([p])
    lat("new shell script: write+chmod+exec", _timeit(fresh, 200), 1e3, "ms")
    lat("same shell script again", _timeit(lambda i: run([os.path.join(d, "s0.sh")]), 200), 1e3, "ms")

    def fresh_macho(i):
        p = os.path.join(d, f"echo{i}")
        shutil.copyfile("/bin/echo", p)
        os.chmod(p, 0o755)
        run([p, "x"])
    lat("new copy of Mach-O /bin/echo + exec", _timeit(fresh_macho, 40), 1e3, "ms")
    lat("same Mach-O copy again", _timeit(lambda i: run([os.path.join(d, "echo0"), "x"]), 100), 1e3, "ms")
    py = os.environ.get("HERMES_PYTHON") or sys.executable
    lat("python -c pass", _timeit(lambda i: run([py, "-c", "pass"]), 20), 1e3, "ms")
    lat("python -I -S -c pass", _timeit(lambda i: run([py, "-I", "-S", "-c", "pass"]), 20), 1e3, "ms")
    lat("python -c 'import pytest'", _timeit(lambda i: run([py, "-c", "import pytest"]), 10), 1e3, "ms")
    env = dict(os.environ, GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@b", GIT_COMMITTER_NAME="a",
               GIT_COMMITTER_EMAIL="a@b")

    def git(i):
        r = os.path.join(d, f"g{i}")
        os.mkdir(r)
        run(["git", "init", "-q", r], env=env)
        with open(os.path.join(r, "f"), "w") as f:
            f.write("x\n")
        run(["git", "-C", r, "add", "f"], env=env)
        run(["git", "-C", r, "commit", "-q", "-m", "x"], env=env)
    lat("git init+add+commit", _timeit(git, 20), 1e3, "ms")
    lat("sudo -n true", _timeit(lambda i: run(["sudo", "-n", "true"]), 20), 1e3, "ms")
    ncpu = os.cpu_count()
    with mp.get_context("spawn").Pool(ncpu) as pool:
        t0 = time.perf_counter()
        pool.map(_spawn_many, [(150,)] * ncpu)
        el = time.perf_counter() - t0
    print(f"  parallel exec /usr/bin/true: {ncpu} workers x 150 = {150*ncpu/el:.0f} spawns/s ({el:.2f}s)")
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- diskbench
F_FULLFSYNC, F_NOCACHE, F_BARRIERFSYNC = 51, 48, 85


def diskbench(base):
    os.makedirs(base, exist_ok=True)
    d = tempfile.mkdtemp(prefix="disk-", dir=base)
    print(f"dir: {d.replace(os.path.expanduser('~'), '~')}  ({sh('df -h ' + d + ' | tail -1')})")
    buf = os.urandom(4096)
    for name, sync, n in (("4k write + fsync", os.fsync, 500),
                          ("4k write + F_BARRIERFSYNC", lambda fd: fcntl.fcntl(fd, F_BARRIERFSYNC), 300),
                          ("4k write + F_FULLFSYNC", lambda fd: fcntl.fcntl(fd, F_FULLFSYNC), 200)):
        fd = os.open(os.path.join(d, "sync.bin"), os.O_RDWR | os.O_CREAT, 0o644)
        out = []
        for i in range(n):
            os.pwrite(fd, buf, (i % 256) * 4096)
            t0 = time.perf_counter()
            sync(fd)
            out.append(time.perf_counter() - t0)
        os.close(fd)
        lat(name, out)
    for jm, sync in (("WAL", "FULL"), ("WAL", "NORMAL"), ("DELETE", "FULL")):
        p = os.path.join(d, f"t-{jm}-{sync}.db")
        c = sqlite3.connect(p, isolation_level=None)
        c.execute(f"PRAGMA journal_mode={jm}")
        c.execute(f"PRAGMA synchronous={sync}")
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        out = []
        for i in range(600):
            t0 = time.perf_counter()
            c.execute("BEGIN")
            c.execute("INSERT INTO t(v) VALUES (?)", ("x" * 200,))
            c.execute("COMMIT")
            out.append(time.perf_counter() - t0)
        c.close()
        lat(f"sqlite commit {jm}/{sync}", out)
    # sequential throughput
    p = os.path.join(d, "seq.bin")
    chunk = os.urandom(1 << 20)
    total = 2048
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    t0 = time.perf_counter()
    for _ in range(total):
        os.write(fd, chunk)
    t1 = time.perf_counter()
    os.fsync(fd)
    t2 = time.perf_counter()
    fcntl.fcntl(fd, F_FULLFSYNC)
    t3 = time.perf_counter()
    os.close(fd)
    print(f"  seq write 2 GiB: buffered {total/(t1-t0):.0f} MiB/s, fsync {t2-t1:.2f}s, F_FULLFSYNC {t3-t2:.2f}s,"
          f" end-to-end {total/(t3-t0):.0f} MiB/s")
    fd = os.open(p, os.O_RDONLY)
    fcntl.fcntl(fd, F_NOCACHE, 1)
    t0 = time.perf_counter()
    n = 0
    while True:
        b = os.read(fd, 1 << 20)
        if not b:
            break
        n += len(b)
    os.close(fd)
    print(f"  seq read 2 GiB (F_NOCACHE): {n/(1<<20)/(time.perf_counter()-t0):.0f} MiB/s")
    os.unlink(p)
    # metadata
    m = os.path.join(d, "meta")
    os.mkdir(m)
    small = b"x" * 1024
    t0 = time.perf_counter()
    for i in range(5000):
        with open(os.path.join(m, f"f{i}"), "wb") as f:
            f.write(small)
    t1 = time.perf_counter()
    for i in range(5000):
        os.stat(os.path.join(m, f"f{i}"))
    t2 = time.perf_counter()
    for i in range(5000):
        os.unlink(os.path.join(m, f"f{i}"))
    t3 = time.perf_counter()
    print(f"  metadata: create+write 1KiB {5000/(t1-t0):.0f}/s, stat {5000/(t2-t1):.0f}/s, unlink {5000/(t3-t2):.0f}/s")
    t0 = time.perf_counter()
    for i in range(300):
        q = os.path.join(m, f"d{i}")
        os.makedirs(os.path.join(q, "a", "b"))
        shutil.rmtree(q)
    print(f"  mkdir -p a/b + rmtree: {(time.perf_counter()-t0)/300*1e3:.2f} ms each")
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- netbench
URLS = [
    ("pypi simple index (small)", "https://pypi.org/simple/pip/"),
    ("github.com (small)", "https://github.com/"),
    ("uv 0.5.0 release asset (~15 MB)", "https://github.com/astral-sh/uv/releases/download/0.5.0/uv-aarch64-apple-darwin.tar.gz"),
    ("npm typescript 5.6.3 tarball (~4 MB)", "https://registry.npmjs.org/typescript/-/typescript-5.6.3.tgz"),
]


def netbench():
    for host in ("github.com", "pypi.org", "files.pythonhosted.org", "registry.npmjs.org"):
        out = []
        for _ in range(3):
            t0 = time.perf_counter()
            try:
                socket.getaddrinfo(host, 443)
            except OSError:
                pass
            out.append(time.perf_counter() - t0)
        print(f"  dns {host:24s} first {out[0]*1e3:7.1f}ms  then {min(out[1:])*1e3:6.1f}ms")
    fmt = "%{time_namelookup} %{time_connect} %{time_appconnect} %{time_starttransfer} %{time_total} %{size_download} %{http_code}"
    for label, url in URLS:
        rows = []
        for _ in range(3):
            r = subprocess.run(["curl", "-sSL", "-o", "/dev/null", "-w", fmt, url], capture_output=True, text=True, timeout=120)
            rows.append(r.stdout.split())
        best = min(rows, key=lambda r: float(r[4]) if len(r) == 7 else 1e9)
        if len(best) != 7:
            print(f"  {label}: failed")
            continue
        dns, conn, tls, ttfb, tot, size, code = best
        mb = float(size) / 1e6
        print(f"  {label:38s} http {code} best of 3: connect {float(conn)*1e3:6.0f}ms tls {float(tls)*1e3:6.0f}ms"
              f" ttfb {float(ttfb)*1e3:6.0f}ms total {float(tot):6.2f}s {mb:6.1f}MB {mb/max(float(tot)-float(ttfb),1e-3):7.1f}MB/s"
              f"  (all: {' '.join('%.2f' % float(r[4]) for r in rows if len(r) == 7)}s)")


# ---------------------------------------------------------------- sampler
def _cpu_ticks():
    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    libc.mach_host_self.restype = ctypes.c_uint
    host = libc.mach_host_self()
    task = ctypes.c_uint.in_dll(libc, "mach_task_self_")
    libc.host_processor_info.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                                         ctypes.POINTER(ctypes.POINTER(ctypes.c_uint)), ctypes.POINTER(ctypes.c_uint)]
    libc.vm_deallocate.argtypes = [ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]

    def read():
        n, cnt = ctypes.c_uint(), ctypes.c_uint()
        info = ctypes.POINTER(ctypes.c_uint)()
        if libc.host_processor_info(host, 2, ctypes.byref(n), ctypes.byref(info), ctypes.byref(cnt)) != 0:
            return None
        vals = [info[i] for i in range(n.value * 4)]
        libc.vm_deallocate(task.value, ctypes.cast(info, ctypes.c_void_p).value, cnt.value * 4)
        return [vals[i * 4:(i + 1) * 4] for i in range(n.value)]  # user, system, idle, nice
    return read


def sampler(out):
    os.makedirs(out, exist_ok=True)
    read = _cpu_ticks()
    prev = read()
    fcpu = open(os.path.join(out, "percpu.csv"), "w")
    fps = open(os.path.join(out, "ps.txt"), "w")
    fload = open(os.path.join(out, "load.csv"), "w")
    i = 0
    while not os.path.exists(os.path.join(out, "STOP")):
        time.sleep(1)
        cur = read()
        t = time.time()
        busy = []
        for a, b in zip(prev, cur):
            d = [y - x for x, y in zip(a, b)]
            tot = sum(d) or 1
            busy.append(100.0 * (d[0] + d[1] + d[3]) / tot)
        fcpu.write(f"{t:.1f}," + ",".join(f"{x:.0f}" for x in busy) + "\n")
        fcpu.flush()
        prev = cur
        fload.write(f"{t:.1f},{os.getloadavg()[0]:.2f}\n")
        if i % 3 == 0:
            r = subprocess.run(["ps", "-Ao", "pid=,time=,comm="], capture_output=True, text=True)
            fps.write(f"@{t:.1f}\n" + r.stdout)
            fps.flush()
        i += 1


def _secs(s):
    p = s.replace("-", ":").split(":")
    v = 0.0
    for x in p:
        v = v * 60 + float(x)
    return v


def summarize(out):
    rows = [list(map(float, l.split(","))) for l in open(os.path.join(out, "percpu.csv")) if l.strip()]
    if not rows:
        print("no samples")
        return
    ncpu = len(rows[0]) - 1
    per = [st.mean(r[i + 1] for r in rows) for i in range(ncpu)]
    tot = [sum(r[1:]) / 100 for r in rows]  # busy vCPUs per sample
    print(f"samples: {len(rows)} x 1s, vCPUs {ncpu}")
    print("per-vCPU mean busy %: " + " ".join(f"{x:.0f}" for x in per))
    print(f"busy vCPUs (sum of busy/100): mean {st.mean(tot):.1f}  p50 {pct(tot,50):.1f}  p90 {pct(tot,90):.1f}  max {max(tot):.1f}")
    hist = [0] * (ncpu + 1)
    for r in rows:
        hist[sum(1 for x in r[1:] if x >= 80)] += 1
    print("seconds with k vCPUs >=80% busy: " + " ".join(f"{k}:{c}" for k, c in enumerate(hist) if c))
    loads = [float(l.split(",")[1]) for l in open(os.path.join(out, "load.csv")) if l.strip()]
    print(f"load avg (1m): mean {st.mean(loads):.1f} max {max(loads):.1f}")
    # per-command CPU seconds from ps snapshots (processes living >= 3 s)
    first, last, names = {}, {}, {}
    for block in open(os.path.join(out, "ps.txt")).read().split("@")[1:]:
        lines = block.splitlines()[1:]
        for l in lines:
            p = l.split(None, 2)
            if len(p) < 3:
                continue
            pid, cpu, comm = p[0], _secs(p[1]), os.path.basename(p[2].split()[0])
            first.setdefault(pid, cpu)
            last[pid] = cpu
            names[pid] = comm
    agg = {}
    for pid, c in last.items():
        agg[names[pid]] = agg.get(names[pid], 0) + c - first[pid]
    print("CPU seconds by command during the window (long-lived processes only):")
    for k, v in sorted(agg.items(), key=lambda x: -x[1])[:15]:
        print(f"  {v:8.1f}s  {k[:40]}")
    p = os.path.join(out, "vm_stat.txt")
    if os.path.exists(p):
        ls = [l.split() for l in open(p).read().splitlines()[1:]]
        hdr, data = ls[0], [r for r in ls[2:] if r and r[0].isdigit() and len(r) == len(ls[0])]
        if data:
            col = lambda n: [int(r[hdr.index(n)]) for r in data]  # noqa: E731
            pg = 16384 / (1 << 20)
            print(f"vm_stat over {len(data)} x 2s: free min {min(col('free'))*pg:.0f} MiB,"
                  f" compressor max {max(col('cmprssed'))*pg:.0f} MiB, pageouts {sum(col('pageout'))},"
                  f" swapins {sum(col('swapins'))}, swapouts {sum(col('swapouts'))},"
                  f" compressions {sum(col('comprs'))}, decompressions {sum(col('dcomprs'))}")
    p = os.path.join(out, "iostat.txt")
    if os.path.exists(p):
        rows = []
        for l in open(p).read().splitlines()[3:]:
            v = l.split()
            if len(v) >= 3 and all(re.fullmatch(r"[\d.]+", x) for x in v[:3]):
                rows.append([float(x) for x in v[:3]])
        if rows:
            print(f"iostat disk0 over {len(rows)} x 2s: MB/s mean {st.mean(r[2] for r in rows):.1f}"
                  f" max {max(r[2] for r in rows):.1f}; tps mean {st.mean(r[1] for r in rows):.0f}"
                  f" max {max(r[1] for r in rows):.0f}; KB/t mean {st.mean(r[0] for r in rows):.1f}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd in ("diskbench", "sampler", "summarize"):
        globals()[cmd](sys.argv[2])
    else:
        globals()[cmd]()
