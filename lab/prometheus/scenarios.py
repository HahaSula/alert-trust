#!/usr/bin/env python3
import json, sys, threading, time, lab
from lab import sh, c, wait
METRICS = {
 "scrape_interval_p99": 'prometheus_target_interval_length_seconds{job="prom",interval="15s",quantile="0.99"}',
 "scrape_dur_big": 'scrape_duration_seconds{job="big"}',
 "up_big": 'up{job="big"}', "up_small": 'up{job="small"}',
 "rule_load_last_dur": 'prometheus_rule_group_last_duration_seconds{job="prom",rule_group=~".*;load"}',
 "rule_missed_5m": 'sum(increase(prometheus_rule_group_iterations_missed_total{job="prom"}[5m]))',
 "rule_failed_5m": 'sum(increase(prometheus_rule_evaluation_failures_total{job="prom"}[5m]))',
 "rule_eval_age": 'max(time() - prometheus_rule_group_last_evaluation_timestamp_seconds{job="prom"})',
 "queue_time_p90": 'prometheus_engine_query_duration_seconds{job="prom",slice="queue_time",quantile="0.9"}',
 "queries_inflight": 'prometheus_engine_queries{job="prom"}',
 "fsync_p99": 'prometheus_tsdb_wal_fsync_duration_seconds{job="prom",quantile="0.99"}',
 "append_rate": 'rate(prometheus_tsdb_head_samples_appended_total{job="prom",type="float"}[1m])',
 "wal_bytes": 'prometheus_tsdb_wal_storage_size_bytes{job="prom"}',
 "blocks": 'prometheus_tsdb_blocks_loaded{job="prom"}',
 "reloads_5m": 'increase(prometheus_tsdb_reloads_total{job="prom"}[5m])',
 "compact_failed_5m": 'increase(prometheus_tsdb_compactions_failed_total{job="prom"}[5m])',
 "cpu_core": 'rate(process_cpu_seconds_total{job="prom"}[1m])',
 "rss": 'process_resident_memory_bytes{job="prom"}',
 "gc_p100": 'go_gc_duration_seconds{job="prom",quantile="1"}',
 "throttle_ratio": 'rate(container_cpu_cfs_throttled_periods_total[1m]) / rate(container_cpu_cfs_periods_total[1m])',
 "working_set": 'container_memory_working_set_bytes',
 "notify_queue": 'prometheus_notifications_queue_length{job="prom"}',
 "notify_err_rate": 'sum(rate(prometheus_notifications_errors_total{job="prom"}[2m]))',
 "notify_latency_p99": 'max(prometheus_notifications_latency_seconds{job="prom",quantile="0.99"})',
 "am_discovered": 'prometheus_notifications_alertmanagers_discovered{job="prom"}',
}
class Run:
    def __init__(s, name, fresh=True, **kw):
        s.name = name; s.phases = []
        if fresh: lab.down(name)
        lab.up(name, **kw); s.t = time.time(); print(f"{name} up", flush=True)
    def phase(s, label, sec, act=None):
        if act: act()
        st = time.time(); wait(sec, f"{s.name}:{label}"); s.phases.append((label, st, time.time()))
    def finish(s, down=True, extra=None):
        out = {"name": s.name, "phases": {}}
        for lb, a, b in s.phases:
            p = {"dur": round(b - a), "alerts_first_fire_s": lab.alerts(s.name, a, b), "sink": lab.sink_gaps(s.name, a, b), "metrics": {}}
            for k, e in METRICS.items():
                try:
                    e2 = e.replace('container_cpu_cfs_throttled_periods_total', f'container_cpu_cfs_throttled_periods_total{{name="mom-{s.name}-prom"}}').replace('container_cpu_cfs_periods_total', f'container_cpu_cfs_periods_total{{name="mom-{s.name}-prom"}}').replace('container_memory_working_set_bytes', f'container_memory_working_set_bytes{{name="mom-{s.name}-prom"}}')
                    st = lab.stats(s.name, e2, a, b)
                except Exception as ex:
                    st = {"err": str(ex)[:80]}
                p["metrics"][k] = {x: (round(v, 4) if isinstance(v, float) else v) for x, v in st.items()}
            out["phases"][lb] = p
        if extra: out["extra"] = extra
        lab.save(s.name, out)
        if down: lab.down(s.name)
        print(f"{s.name} done", flush=True)
BIG = 300000
def blind():
    r = Run("blind", big=BIG, slow_delay=0)
    stop = lambda x: (lambda: sh("docker", "stop", "-t", "1", c("blind", x)))
    start = lambda x: (lambda: sh("docker", "start", c("blind", x)))
    r.phase("base", 240)
    r.phase("target_small_down", 240, stop("target-small")); r.phase("rec1", 180, start("target-small"))
    r.phase("big_target_down", 180, stop("target-big")); r.phase("rec2", 150, start("target-big"))
    r.phase("am_down", 240, stop("am")); r.phase("rec3", 180, start("am"))
    def killprom():
        sh("docker", "update", "--restart", "no", c("blind", "prom")); sh("docker", "stop", "-t", "1", c("blind", "prom"))
    r.phase("prom_down", 180, killprom); r.phase("rec4", 180, start("prom"))
    r.finish()
def cpu(name, flags):
    r = Run(name, big=BIG, slow_delay=0, prom_args=flags, docker=("--cpus", "1"))
    r.phase("base_1cpu", 300)
    r.phase("cpu_0.3", 480, lambda: sh("docker", "update", "--cpus", "0.3", c(name, "prom")))
    r.phase("cpu_0.1", 300, lambda: sh("docker", "update", "--cpus", "0.1", c(name, "prom")))
    r.phase("recover", 240, lambda: sh("docker", "update", "--cpus", "1", c(name, "prom")))
    r.finish()
def iops():
    n = "iops"; r = Run(n, big=BIG, slow_delay=0)
    r.phase("base", 300)
    r.phase("iops_50", 360, lambda: lab.set_iops(n, 50, 4194304))
    r.phase("iops_10", 360, lambda: lab.set_iops(n, 10, 1048576))
    r.phase("recover", 240, lambda: lab.set_iops(n))
    r.finish()
def query():
    n = "query"; r = Run(n, big=BIG, slow_delay=0, prom_args=("--query.max-concurrency=2", "--query.timeout=20s"))
    r.phase("base", 360)
    stop = threading.Event()
    def hammer():
        q = lab.enc('sum by (id) (avg_over_time(demo_filler[10m]))')
        while not stop.is_set():
            subprocess.run(["docker", "exec", c(n, "meta"), "wget", "-qO-", "-T", "60", f"http://prom:9090/api/v1/query?query={q}"], capture_output=True)
    import subprocess
    r.phase("hammer", 360, lambda: [threading.Thread(target=hammer, daemon=True).start() for _ in range(6)])
    stop.set()
    r.phase("recover", 180)
    rules = lab.api(n, "/api/v1/rules", role="prom")["data"]["groups"]
    r.finish(extra={"rule_health": [(x["name"], x["health"], x.get("lastError", "")[:90]) for g in rules for x in g["rules"]]})
def pending(name, image, qo=False):
    r = Run(name, image=image, big=2000, slow_delay=100, query_offset=qo)
    r.phase("run", 1500)
    ex = {}
    b = time.time()
    for an in ("KnownTrueCanary", "SlowTargetNoOffset", "SlowTargetExprOffset", "SlowTargetQueryOffset"):
        fs = lab.q(name, f'changes(ALERTS_FOR_STATE{{alertname="{an}"}}[20m])')
        fire = lab.qr(name, f'ALERTS{{alertname="{an}",alertstate="firing"}}', r.t, b, 15)
        pend = lab.qr(name, f'ALERTS{{alertname="{an}",alertstate="pending"}}', r.t, b, 15)
        ex[an] = {"for_state_changes_20m": fs[0]["value"][1] if fs else None,
                  "firing_points": sum(len(x["values"]) for x in fire), "pending_points": sum(len(x["values"]) for x in pend),
                  "first_firing_after_s": round(float(fire[0]["values"][0][0]) - r.t) if fire else None}
    r.finish(extra=ex)
def disk(name, image):
    vol = f"mom-{name}-tv"
    lab.down(name)
    sh("docker", "rm", "-f", c(name, "holder"), check=False); sh("docker", "volume", "rm", vol, check=False)
    sh("docker", "volume", "create", "--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs", "--opt", "o=size=2g,uid=65534,gid=65534,mode=0777", vol)
    sh("docker", "run", "-d", "--name", c(name, "holder"), "--label", "mom-lab=1", "-v", f"{vol}:/prometheus", "python:3.12-alpine", "sleep", "86400")
    def snap(label):
        o = sh("docker", "exec", c(name, "holder"), "sh", "-c", "df -m /prometheus | tail -1 | awk '{print $3\"MB used\"}'; du -sm /prometheus/wal /prometheus/chunks_head | tr '\\n' ' '; echo; ls /prometheus/wal | wc -l; ls -d /prometheus/01* 2>/dev/null | wc -l; ls -a /prometheus | grep -c tmp", check=False)
        snaps[label] = o.split("\n"); print(name, label, snaps[label], flush=True)
    snaps = {}
    r = Run(name, fresh=False, image=image, big=150000, slow_delay=0, data=vol, block_rules=True,
            prom_args=("--storage.tsdb.min-block-duration=5m", "--storage.tsdb.max-block-duration=5m", "--storage.tsdb.retention.time=10m"))
    r.phase("base", 900); snap("end_base")
    fill = lambda: sh("docker", "exec", c(name, "holder"), "sh", "-c", "dd if=/dev/zero of=/prometheus/filler bs=1M 2>&1 | tail -1", check=False)
    r.phase("disk_full", 600, fill); snap("end_full")
    r.phase("space_freed", 1080, lambda: sh("docker", "exec", c(name, "holder"), "rm", "-f", "/prometheus/filler")); snap("end_freed")
    r.phase("restarted", 420, lambda: sh("docker", "restart", "-t", "5", c(name, "prom"))); snap("end_restart")
    extra = {"snaps": snaps}
    r.finish(extra=extra, down=False)
    log = sh("bash", "-c", f"docker logs {c(name,'prom')} 2>&1")
    pats = ["no space left", "write to WAL", "Scrape commit failed", "compaction failed", "persist head block", "create checkpoint", "WAL truncation in Compact", "unexpected full record", "Encountered WAL read error", "Starting corruption repair", "Successfully repaired WAL", "reloadBlocks", "Deleting obsolete block", "panic", "Rule sample appending failed", "Evaluating rule failed"]
    extra["log_counts"] = {p: log.count(p) for p in pats}
    with open(f"{lab.RES}/{name}/extra.json", "w") as f: json.dump(extra, f, indent=1, ensure_ascii=False)
    lab.down(name); sh("docker", "rm", "-f", c(name, "holder"), check=False); sh("docker", "volume", "rm", vol, check=False)
def mem(name, flags, start_limit, squeeze=None):
    r = Run(name, big=BIG, slow_delay=0, prom_args=flags, docker=("--memory", start_limit, "--memory-swap", start_limit))
    count = lambda: int(sh("docker", "inspect", "-f", "{{.RestartCount}}", c(name, "prom")))
    ex = {}
    r.phase("base", 420); ex["restarts_after_base"] = count()
    if squeeze:
        def sq(v): return lambda: sh("docker", "update", "--memory", v, "--memory-swap", v, c(name, "prom"), check=False)
        r.phase("squeezed", 480, sq(squeeze)); ex["restarts_after_squeeze"] = count()
        ex["oom_killed"] = sh("docker", "inspect", "-f", "{{.State.OOMKilled}}", c(name, "prom"))
        r.phase("recover", 420, sq(start_limit)); ex["restarts_end"] = count()
    else:
        r.phase("steady", 480); ex["restarts_end"] = count()
    ex["for_state_changes"] = [(x["metric"].get("alertname"), x["value"][1]) for x in lab.q(name, 'changes(ALERTS_FOR_STATE[25m])')]
    r.finish(extra=ex)
def iops2():
    n = "iops2"; ex = {"probe": {}}
    r = Run(n, big=BIG, slow_delay=0, block_rules=True, docker=("--memory", "3g", "--memory-swap", "3g"),
            prom_args=("--storage.tsdb.min-block-duration=5m", "--storage.tsdb.max-block-duration=5m", "--storage.tsdb.retention.time=30m"))
    def probe(label):
        o = sh("docker", "exec", c(n, "prom"), "sh", "-c", "time dd if=/dev/zero of=/prometheus/probe bs=1M count=16 conv=fsync 2>&1 | tail -1; rm -f /prometheus/probe", check=False)
        e = subprocess.run(["docker", "exec", c(n, "prom"), "sh", "-c", "S=$(date +%s); dd if=/dev/zero of=/prometheus/probe bs=1M count=16 conv=fsync 2>/dev/null; E=$(date +%s); echo $((E-S)); rm -f /prometheus/probe"], capture_output=True, text=True).stdout.strip()
        ex["probe"][label] = e; print("probe", label, e, flush=True)
    import subprocess
    r.phase("base", 720); probe("base")
    r.phase("iops_50", 720, lambda: lab.set_iops(n, 50, 4194304)); probe("iops_50")
    r.phase("iops_10", 720, lambda: lab.set_iops(n, 10, 1048576)); probe("iops_10")
    r.phase("recover", 300, lambda: lab.set_iops(n))
    r.finish(extra=ex)
def query2():
    n = "query2"; import subprocess
    r = Run(n, big=BIG, slow_delay=0, prom_args=("--query.max-concurrency=2", "--query.timeout=20s"))
    r.phase("base", 360)
    stop = threading.Event(); ex = {"for_changes": {}}
    def hammer():
        q = lab.enc('sum by (id) (avg_over_time(demo_filler[10m]))')
        while not stop.is_set():
            subprocess.run(["docker", "exec", c(n, "meta"), "wget", "-qO-", "-T", "60", f"http://prom:9090/api/v1/query?query={q}"], capture_output=True)
    r.phase("hammer", 360, lambda: [threading.Thread(target=hammer, daemon=True).start() for _ in range(6)])
    for row in lab.q(n, 'changes(ALERTS_FOR_STATE[10m])'): ex["for_changes"][row["metric"].get("alertname")] = row["value"][1]
    ex["alerts_pending_series"] = [row["metric"] for row in lab.q(n, 'ALERTS{alertstate="pending"}')]
    stop.set(); r.phase("recover", 180)
    rules = lab.api(n, "/api/v1/rules", role="prom")["data"]["groups"]
    ex["rule_detail"] = [(x["name"], x["health"], x.get("evaluationTime"), x.get("lastError", "")[:90]) for g in rules for x in g["rules"]]
    r.finish(extra=ex)
def latency(name, s, e):
    """Per alert: notification delay after the toggle's rising edge (edge at wall-clock multiples of 120s)."""
    rows = [json.loads(l) for l in open(f"{lab.RES}/{name}/notifications.jsonl")]; out = {}
    for an in ("ToggleFast", "ToggleInLoad"):
        delays = {}
        for r in rows:
            if r["alertname"] == an and r["status"] == "firing" and s <= r["t"] <= e:
                edge = int(r["t"] // 120) * 120
                delays.setdefault(edge, r["t"] - edge)
        # edges whose high phase fits in the window; missing means no notification in that cycle
        edges = [x for x in range(int(s // 120) * 120 + 120, int(e), 120) if x + 90 <= e]
        got = [delays[x] for x in edges if x in delays]
        out[an] = {"edges": len(edges), "notified": len(got), "avg_s": round(sum(got) / len(got), 1) if got else None, "max_s": round(max(got), 1) if got else None}
    return out
def cpuknee():
    n = "cpuknee"; r = Run(n, big=BIG, slow_delay=0, prom_args=("--enable-feature=auto-gomaxprocs",), docker=("--cpus", "1"))
    r.phase("warm", 480)
    steps = [1.0, 0.8, 0.6, 0.5, 0.45, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15]; lat = {}
    for q in steps:
        r.phase(f"q{q}", 300, lambda q=q: sh("docker", "update", "--cpus", str(q), c(n, "prom")))
        lb, a, b = r.phases[-1]; lat[lb] = latency(n, a + 60, b); print(lb, lat[lb], flush=True)
    r.phase("recover", 180, lambda: sh("docker", "update", "--cpus", "1", c(n, "prom")))
    r.finish(extra={"latency": lat})
def memknee():
    n = "memknee"; ex = {"steps": []}
    r = Run(n, big=BIG, slow_delay=0, prom_args=(), docker=("--memory", "6g", "--memory-swap", "6g"))
    count = lambda: int(sh("docker", "inspect", "-f", "{{.RestartCount}}", c(n, "prom")))
    start = lambda: float(sh("docker", "inspect", "-f", "{{.State.StartedAt}}", c(n, "prom")) and 0)
    ws = lambda: float(lab.q(n, f'container_memory_working_set_bytes{{name="mom-{n}-prom"}}')[0]["value"][1])
    r.phase("warm", 1500); base = ws(); ex["ws_gb_warm"] = round(base / 1e9, 2)
    ex["rss_gb_warm"] = round(float(lab.q(n, 'process_resident_memory_bytes{job="prom"}')[0]["value"][1]) / 1e9, 2)
    for ratio in (1.5, 1.3, 1.2, 1.1, 1.0, 0.9, 0.8):
        lim = f"{int(base * ratio / 1e6)}m"; before = count()
        def act(l=lim): sh("docker", "update", "--memory", l, "--memory-swap", l, c(n, "prom"), check=False)
        r.phase(f"limit_{ratio}", 240, act); running = count() - before
        sh("docker", "restart", "-t", "0", c(n, "prom"), check=False)
        lb = f"limit_{ratio}_after_unclean_restart"; b2 = count()
        r.phase(lb, 240); after = count() - b2
        ex["steps"].append({"ratio": ratio, "limit": lim, "restarts_running": running, "restarts_after_unclean_restart": after,
                            "corruptions": lab.q(n, 'prometheus_tsdb_wal_corruptions_total{job="prom"}')[0]["value"][1] if lab.q(n, 'prometheus_tsdb_wal_corruptions_total{job="prom"}') else None})
        print(ex["steps"][-1], flush=True)
        if after >= 3: break
    sh("docker", "update", "--memory", "6g", "--memory-swap", "6g", c(n, "prom"), check=False)
    r.finish(extra=ex)
def diskgap(name, image):
    """Space runs out because the head compaction itself needs room: leave little free space just before the head becomes compactable."""
    vol = f"mom-{name}-tv"
    lab.down(name); sh("docker", "rm", "-f", c(name, "holder"), check=False); sh("docker", "volume", "rm", vol, check=False)
    sh("docker", "volume", "create", "--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs", "--opt", "o=size=2g,uid=65534,gid=65534,mode=0777", vol)
    sh("docker", "run", "-d", "--name", c(name, "holder"), "--label", "mom-lab=1", "-v", f"{vol}:/prometheus", "python:3.12-alpine", "sleep", "86400")
    r = Run(name, fresh=False, image=image, big=150000, slow_delay=0, data=vol, block_rules=True,
            prom_args=("--storage.tsdb.min-block-duration=5m", "--storage.tsdb.max-block-duration=5m", "--storage.tsdb.retention.time=10m"))
    span = lambda: float(lab.q(name, 'prometheus_tsdb_head_max_time_seconds{job="prom"} - prometheus_tsdb_head_min_time_seconds{job="prom"}')[0]["value"][1])
    st = time.time(); wait(600, f"{name}:warm")
    while span() > 390 or span() < 300: time.sleep(5)   # head span 5.0-6.5m: compaction is roughly 1-2.5 minutes away
    r.phases.append(("base", st, time.time()))
    free = 60
    def fill():
        o = sh("docker", "exec", c(name, "holder"), "sh", "-c", f"A=$(df -m /prometheus | tail -1 | awk '{{print $4}}'); dd if=/dev/zero of=/prometheus/filler bs=1M count=$((A-{free})) 2>/dev/null; df -m /prometheus | tail -1 | awk '{{print $4}}'", check=False)
        print(name, "free MB after fill", o, flush=True)
    r.phase("nearly_full", 900, fill)
    ex = {"compaction_failed_total": lab.q(name, 'prometheus_tsdb_compactions_failed_total{job="prom"}')[0]["value"][1], "reloads_last_5m": lab.q(name, 'increase(prometheus_tsdb_reloads_total{job="prom"}[5m])')[0]["value"][1],
          "triggered": lab.q(name, 'prometheus_tsdb_compactions_triggered_total{job="prom"}')[0]["value"][1], "span_s": span(), "blocks": lab.q(name, 'prometheus_tsdb_blocks_loaded{job="prom"}')[0]["value"][1]}
    r.phase("space_freed", 300, lambda: sh("docker", "exec", c(name, "holder"), "rm", "-f", "/prometheus/filler"))
    r.finish(extra=ex, down=False)
    log = sh("bash", "-c", f"docker logs {c(name,'prom')} 2>&1")
    ex["log_counts"] = {p: log.count(p) for p in ["no space left", "persist head block", "compaction failed", "reloadBlocks", "unexpected full record", "Deleting obsolete block", "panic"]}
    with open(f"{lab.RES}/{name}/extra.json", "w") as f: json.dump(ex, f, indent=1)
    lab.down(name); sh("docker", "rm", "-f", c(name, "holder"), check=False); sh("docker", "volume", "rm", vol, check=False)
JOBS = {"blind": blind, "cpu8": lambda: cpu("cpu8", ()), "cpu1": lambda: cpu("cpu1", ("--enable-feature=auto-gomaxprocs",)), "iops": iops, "query": query,
        "pend251": lambda: pending("pend251", "prom/prometheus:v2.51.2"), "pend255": lambda: pending("pend255", "prom/prometheus:v2.55.1", True),
        "pend315": lambda: pending("pend315", "prom/prometheus:v3.15.1", True),
        "diskgap251": lambda: diskgap("diskgap251", "prom/prometheus:v2.51.2"), "diskgap313": lambda: diskgap("diskgap313", "prom/prometheus:v3.13.5"), "cpuknee": cpuknee, "memknee": memknee, "iops2": iops2, "query2": query2, "memsq": lambda: mem("memsq", (), "4g", "1100m"), "memoff": lambda: mem("memoff", (), "1600m"), "memon": lambda: mem("memon", ("--enable-feature=auto-gomemlimit",), "1600m"),
        "disk313": lambda: disk("disk313", "prom/prometheus:v3.13.5"), "pend313": lambda: pending("pend313", "prom/prometheus:v3.13.5", True),
        "disk251": lambda: disk("disk251", "prom/prometheus:v2.51.2"), "disk255": lambda: disk("disk255", "prom/prometheus:v2.55.1"), "disk315": lambda: disk("disk315", "prom/prometheus:v3.15.1")}
if __name__ == "__main__":
    JOBS[sys.argv[1]]()
