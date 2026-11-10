"""Allocate memory slowly until the container's cgroup memory.current reaches a target, then hold.

Env: STEPS="55:120,60:600" -> reach 55 MiB and hold 120 s, then 60 MiB and hold 600 s.
     CHUNK_MB (default 1), SLEEP (seconds between chunks, default 0.5).
     MEASURE=current (default): compare against memory.current (includes page cache, which grows to the limit).
     MEASURE=anon: compare against the "anon" line of memory.stat (memory the kernel cannot reclaim without swap).
Pages are written so they count as anonymous memory (not page cache).
"""
import os, sys, time

CG = "/sys/fs/cgroup/memory.current"
chunk = int(float(os.environ.get("CHUNK_MB", "1")) * 1024 * 1024)
pause = float(os.environ.get("SLEEP", "0.5"))
steps = [tuple(int(x) for x in s.split(":")) for s in os.environ.get("STEPS", "55:600").split(",")]
held = []

MEASURE = os.environ.get("MEASURE", "current")

def current():
    if MEASURE == "anon":
        with open("/sys/fs/cgroup/memory.stat") as f:
            for line in f:
                k, v = line.split()
                if k == "anon":
                    return int(v)
    with open(CG) as f:
        return int(f.read())

for target_mb, hold_s in steps:
    target = target_mb * 1024 * 1024
    while current() < target:
        b = bytearray(chunk)
        for i in range(0, chunk, 4096):
            b[i] = 1
        held.append(b)
        time.sleep(pause)
    print(f"{time.strftime('%H:%M:%S')} reached {MEASURE}={current()} target={target} hold={hold_s}s", flush=True)
    time.sleep(hold_s)
print("done", flush=True)
