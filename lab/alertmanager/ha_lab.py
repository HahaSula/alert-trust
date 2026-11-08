"""exp-am-ha：由 run.py ha 呼叫。兩台 0.34.2 叢集，再加一台 0.27.0 做混版。"""
import os
import shutil
import time

P1, P2, P3, PF = 21193, 21194, 21195, 21180
BAD = "route:\n  receiver: does-not-exist\nreceivers:\n  - name: a\n"


def run(m):
    here = m.HERE
    run_dir = os.path.join(here, ".run")
    for n in ("am1", "am2"):
        d = os.path.join(run_dir, n)
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o755)
        shutil.copy(os.path.join(here, "configs", "ha.yml"), os.path.join(d, "am.yml"))
        os.chmod(os.path.join(d, "am.yml"), 0o644)
    cf = ("docker", "compose", "-f", "compose-ha.yml")
    log = []

    def note(step, **kw):
        row = {"t": m.t(), "step": step, **kw}
        log.append(row)
        print(row)

    def view(port):
        x = m.metrics(port, r"^(alertmanager_(cluster_(members|failed_peers|peers_left)|config_(last_reload_successful|hash)|notifications_total\{integration=\"webhook\"|nflog_snapshot_size)|process_start_time_seconds)")
        x["healthy"] = m.http(f"http://localhost:{port}/-/healthy")[0]
        return x

    def hits():
        s = m.fake_stats(PF).get("hits", {})
        return {k.split("tag=")[1]: v for k, v in s.items() if not k.startswith("t:")}

    def members_inconsistent(scraped):
        # 和 rules/alertmanager/meta.yml 的 AlertmanagerMembersInconsistent 同一個比較（不含 for）：
        # max(members) < count(被抓到的實例)
        vals = [v["alertmanager_cluster_members"] for v in scraped if "alertmanager_cluster_members" in v]
        return {"members": vals, "count": len(vals), "fires": any(x < len(vals) for x in vals)}

    try:
        m.sh(*cf, "up", "-d", "fake", "am1", "am2")
        m.wait_ready([P1, P2])
        for _ in range(30):
            if view(P1).get("alertmanager_cluster_members") == 2 and view(P2).get("alertmanager_cluster_members") == 2:
                break
            time.sleep(1)
        note("cluster formed", am1=view(P1), am2=view(P2))

        for p in (P1, P2):  # ruler 會送給每一台
            m.post_alerts(p, [{"alertname": "HaA"}])
        time.sleep(15)
        note("HaA sent to both; webhook hits", hits=hits(), am1=view(P1), am2=view(P2))

        m.sh(*cf, "stop", "am2")
        for i in range(8):
            time.sleep(5)
            v1 = view(P1)
            note(f"am2 stopped +{5 * (i + 1)}s", am1=v1, members_inconsistent=members_inconsistent([v1]))

        start_before = view(P1).get("process_start_time_seconds")
        with open(os.path.join(run_dir, "am1", "am.yml"), "w") as f:
            f.write(BAD)
        code, body = m.http(f"http://localhost:{P1}/-/reload", data=b"", method="POST")
        time.sleep(2)
        note("invalid config reload on am1", reload_code=code, reload_body=body[:300], am1=view(P1))
        m.post_alerts(P1, [{"alertname": "HaB"}])
        time.sleep(8)
        note("new alert HaB after failed reload (old config should still route it to tag=b)", hits=hits(), am1=view(P1))
        shutil.copy(os.path.join(here, "configs", "ha.yml"), os.path.join(run_dir, "am1", "am.yml"))
        code, body = m.http(f"http://localhost:{P1}/-/reload", data=b"", method="POST")
        time.sleep(2)
        note("good config reload", reload_code=code, am1=view(P1))

        before = hits()
        m.sh(*cf, "restart", "am1")
        m.wait_ready([P1])
        m.post_alerts(P1, [{"alertname": "HaA"}, {"alertname": "HaB"}])
        time.sleep(20)
        note("am1 restarted with its data volume; HaA/HaB re-sent", hits_before=before, hits=hits(),
             start_before=start_before, am1=view(P1))

        # 對照組：不保留資料目錄（SIGKILL 加新的 volume），同樣的 alert 會再通知一次
        before = hits()
        m.sh(*cf, "kill", "am1")
        m.sh(*cf, "rm", "-f", "am1")
        m.sh("docker", "volume", "rm", "at-am-ha_am1data", check=False)
        m.sh(*cf, "up", "-d", "am1")
        m.wait_ready([P1])
        m.post_alerts(P1, [{"alertname": "HaA"}, {"alertname": "HaB"}])
        time.sleep(20)
        note("control: am1 recreated without its data volume; HaA/HaB re-sent", hits_before=before, hits=hits(),
             am1=view(P1))

        # 混版：0.34.2（am1）加 0.27.0（am3old）
        m.sh(*cf, "--profile", "mixed", "up", "-d", "am3old")
        m.wait_ready([P3])
        for _ in range(30):
            if view(P1).get("alertmanager_cluster_members") == 2 and view(P3).get("alertmanager_cluster_members") == 2:
                break
            time.sleep(1)
        for p in (P1, P3):
            m.post_alerts(p, [{"alertname": "HaC"}])
        time.sleep(30)
        note("mixed cluster 0.34.2 + 0.27.0; HaC sent to both", hits=hits(), am1=view(P1), am3old=view(P3))
        m.save("ha/steps.json", log)
    finally:
        for s in ("am1", "am2", "am3old"):
            m.save(f"ha/log-{s}.txt", m.sh(*cf, "--profile", "mixed", "logs", "--no-color", s, check=False).stdout)
        m.sh(*cf, "--profile", "mixed", "down", "-v", "--remove-orphans", check=False)
        shutil.rmtree(run_dir, ignore_errors=True)
