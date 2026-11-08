"""lab/vm 共用的小工具：HTTP、/metrics 解析、docker compose、取樣紀錄。只用標準函式庫。"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = os.environ.get("LAB_HOST", "127.0.0.1")


def http(url, data=None, headers=None, timeout=5, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # 連不上、逾時
        return None, str(e)


def getjson(url, timeout=5):
    code, body = http(url, timeout=timeout)
    try:
        return code, json.loads(body)
    except Exception:
        return code, None


def metrics(port, path="/metrics"):
    """回傳 {(name, labels_str): value}；連不上回 None。"""
    code, body = http(f"http://{HOST}:{port}{path}", timeout=3)
    if code != 200:
        return None
    out = {}
    for line in body.splitlines():
        if not line or line[0] == "#":
            continue
        m = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)", line)
        if m:
            try:
                out[(m.group(1), m.group(2) or "")] = float(m.group(3))
            except ValueError:
                pass
    return out


def msum(ms, name, contains=""):
    if ms is None:
        return None
    vals = [v for (n, l), v in ms.items() if n == name and contains in l]
    return sum(vals) if vals else None


def mmax(ms, name, contains=""):
    if ms is None:
        return None
    vals = [v for (n, l), v in ms.items() if n == name and contains in l]
    return max(vals) if vals else None


def vmalert_alerts(port):
    """{alertname: {"firing": n, "pending": n, "activeAt": [..]}}；連不上回 None。"""
    code, j = getjson(f"http://{HOST}:{port}/api/v1/alerts")
    if code != 200 or not j:
        return None
    out = {}
    for a in j["data"]["alerts"]:
        d = out.setdefault(a["name"], {"firing": 0, "pending": 0, "activeAt": []})
        d[a["state"]] = d.get(a["state"], 0) + 1
        d["activeAt"].append(a.get("activeAt"))
    return out


def vmalert_rule_health(port):
    """{group: {"ok": n, "err": n, "lastError": str}}"""
    code, j = getjson(f"http://{HOST}:{port}/api/v1/rules")
    if code != 200 or not j:
        return None
    out = {}
    for g in j["data"]["groups"]:
        d = out.setdefault(g["name"], {"ok": 0, "err": 0, "lastError": ""})
        for r in g["rules"]:
            if r.get("health") == "ok":
                d["ok"] += 1
            else:
                d["err"] += 1
                d["lastError"] = (r.get("lastError") or "")[:200]
    return out


def am_alerts(port):
    """Alertmanager 目前 active 的 alert：{"alertname|vmalert": n}"""
    code, j = getjson(f"http://{HOST}:{port}/api/v2/alerts?active=true&silenced=false&inhibited=false")
    if code != 200 or j is None:
        return None
    out = {}
    for a in j:
        lab = a["labels"]
        k = f'{lab.get("alertname")}|{lab.get("vmalert", "")}'
        out[k] = out.get(k, 0) + 1
    return out


class Compose:
    def __init__(self, project, file, env=None):
        self.project, self.file = project, file
        self.env = dict(os.environ, **(env or {}))

    def __call__(self, *args, env=None, check=True):
        e = dict(self.env, **(env or {}))
        cmd = ["docker", "compose", "-p", self.project, "-f", self.file, *args]
        r = subprocess.run(cmd, env=e, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)}\n{r.stderr}")
        return r.stdout

    def down(self):
        self("--profile", "gen", "down", "-v", "--remove-orphans", check=False)


class Recorder:
    """每筆紀錄：{"t": epoch, "rel": 相對開始秒數, "phase": ..., **data}，寫成 JSON Lines。"""

    def __init__(self, path):
        self.path = path
        self.t0 = time.time()
        self.phase = "start"
        self.f = open(path, "w")
        self.marks = []

    def mark(self, phase, **extra):
        self.phase = phase
        rec = {"t": round(time.time(), 1), "rel": round(time.time() - self.t0, 1), "mark": phase, **extra}
        self.marks.append(rec)
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()
        print(f"[{rec['rel']:7.1f}s] == {phase} {extra if extra else ''}", flush=True)

    def write(self, data):
        rec = {"t": round(time.time(), 1), "rel": round(time.time() - self.t0, 1), "phase": self.phase, **data}
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()
        return rec

    def sample_for(self, seconds, snapshot, every=5):
        end = time.time() + seconds
        while time.time() < end:
            t = time.time()
            self.write(snapshot())
            time.sleep(max(0, every - (time.time() - t)))


def wait_http(url, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        code, _ = http(url, timeout=2)
        if code == 200:
            return True
        time.sleep(2)
    raise RuntimeError(f"timeout waiting for {url}")


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]
