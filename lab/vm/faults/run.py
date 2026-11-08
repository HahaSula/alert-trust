"""故障矩陣：python3 run.py rf1|rf1b|rf2。rf1：vmstorage 掛、vmselect 掛；rf1b：全部唯讀、vminsert 掛；rf2：vmstorage 掛、vmagent 掛。每 5 秒記錄 vmalert、Alertmanager 與各元件 metric，故障依序執行。"""
import json, os, sys, time, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import (Compose, Recorder, getjson, metrics, msum, mmax, vmalert_alerts, vmalert_rule_health,
                 am_alerts, wait_http, HOST)

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
MODE = sys.argv[1] if len(sys.argv) > 1 else "rf1"
RF, P = {"rf1": ("1", "2002"), "rf1b": ("1", "2002"), "rf2": ("2", "2004")}[MODE]
S = float(os.environ.get("SCALE", "1"))  # 縮短時間用，正式跑 1
port = lambda i: int(P + str(i))


def short(alerts):
    if alerts is None:
        return None
    return {k: {"firing": v.get("firing", 0), "pending": v.get("pending", 0)} for k, v in alerts.items()}


def snapshot():
    sel, ins, agent = metrics(port(0)), metrics(port(1)), metrics(port(2))
    va = metrics(port(3))
    st = [metrics(port(i)) for i in (6, 7, 8)]
    return {
        "vmalert_default": short(vmalert_alerts(port(3))),
        "vmalert_resend": short(vmalert_alerts(port(4))),
        "health_default": vmalert_rule_health(port(3)),
        "am": am_alerts(port(5)),
        "partial_results_total": msum(sel, "vm_partial_results_total"),
        "rule_errors_total": msum(va, "vmalert_alerting_rules_errors_total"),
        "iteration_missed_total": msum(va, "vmalert_iteration_missed_total"),
        "storage_reachable": msum(ins, "vm_rpc_vmstorage_is_reachable"),
        "read_only": [mmax(m, "vm_storage_is_read_only") for m in st],
        "pending_bytes": msum(agent, "vmagent_remotewrite_pending_data_bytes"),
        "up": {"vmselect": sel is not None, "vminsert": ins is not None, "vmagent": agent is not None,
               "vmstorage": [m is not None for m in st]},
    }


def count_query(q, at):
    _, j = getjson(f"http://{HOST}:{port(0)}/select/0/prometheus/api/v1/query?time={at}&query=" + urllib.parse.quote(q))
    try:
        return float(j["data"]["result"][0]["value"][1])
    except Exception:
        return None


def main():
    c = Compose(f"at-vm-faults-{MODE}", os.path.join(HERE, "compose.yml"), env={"RF": RF, "P": P})
    c.down()
    rec = Recorder(os.path.join(RES, f"faults-{MODE}.jsonl"))
    run = lambda sec: rec.sample_for(sec * S, snapshot)
    try:
        c("up", "-d", "--quiet-pull")
        wait_http(f"http://{HOST}:{port(3)}/-/healthy")
        rec.mark("warmup", rf=RF)
        run(330)
        if MODE in ("rf1", "rf2"):
            # (a)/(b) 一台 vmstorage 掛
            rec.mark("vmstorage-3-down"); c("stop", "vmstorage-3"); run(240)
            rec.mark("vmstorage-3-up"); c("start", "vmstorage-3"); run(180)
        if MODE == "rf1":
            # (c) vmselect 掛，同時比較 resendDelay 預設與 3m
            rec.mark("vmselect-down"); c("stop", "vmselect"); run(240)
            rec.mark("vmselect-up"); c("start", "vmselect"); run(180)
            return
        if MODE == "rf1b":
            # (d) 所有 vmstorage 唯讀：調高 -storage.minFreeDiskSpaceBytes 後重建
            rec.mark("readonly-on")
            c("up", "-d", "vmstorage-1", "vmstorage-2", "vmstorage-3", env={"MINFREE": "1000000000000000"})
            run(240)
            rec.mark("readonly-off")
            c("up", "-d", "vmstorage-1", "vmstorage-2", "vmstorage-3", env={"MINFREE": "10000000"})
            run(180)
            # (e) vminsert 掛
            down_name = "vminsert"
        else:
            down_name = "vmagent"
        t_down = time.time()
        rec.mark(f"{down_name}-down"); c("stop", down_name); run(400)
        t_up = time.time()
        rec.mark(f"{down_name}-up"); c("start", down_name); run(150)
        # 寫入故障期間的資料事後是否補回：故障期間 lab_item{id="0"} 的 sample 數（scrape 10s）
        span = int(t_up - t_down)
        n = count_query(f'count_over_time(lab_item{{id="0"}}[{span}s])', int(t_up))
        rec.mark("backfill-check", window_s=span, samples=n, expected=round(span / 10))
    finally:
        rec.f.close()
        if os.environ.get("KEEP") != "1":
            c.down()


if __name__ == "__main__":
    main()
