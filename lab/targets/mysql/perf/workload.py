"""小型工作負載。模式：
  run      THREADS 個 thread，每個每秒最多 RATE 筆交易（點查 + 單列 UPDATE + COMMIT）；每秒印一行 JSON。
  storm N S        N 條連線反覆「連線 → 全表掃描 → 斷線」S 秒（連線風暴）。
  digests N        送出 N 種不同形狀的查詢（每種一個 digest）。
  slow S           S 秒內反覆跑慢查詢（filesort 全表、SLEEP(1)）。
  tmp S            S 秒內用很小的 tmp_table_size 反覆 GROUP BY（暫存表落到磁碟）。
  fill             往 DATA DIRECTORY 在 24 MB tmpfs 上的表一直寫，直到出錯。
時間戳都是 time.time()（wall clock）。
"""
import json, os, random, sys, threading, time
import pymysql

HOST = os.environ.get("DB_HOST", "mdb")
ROWS = 200000


def conn(user="app", pw="apppw", autocommit=False, **kw):
    return pymysql.connect(host=HOST, user=user, password=pw, database="app",
                           autocommit=autocommit, connect_timeout=3, **kw)


def errno(e):
    return str(e.args[0]) if getattr(e, "args", None) else type(e).__name__


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.ok, self.err, self.lat = 0, {}, []

    def add(self, ok, lat=None, err=None):
        with self.lock:
            if ok:
                self.ok += 1
                self.lat.append(lat)
            else:
                self.err[err] = self.err.get(err, 0) + 1

    def flush(self, tag):
        with self.lock:
            lat = sorted(self.lat)
            p99 = lat[int(len(lat) * 0.99)] if lat else None
            line = {"t": round(time.time(), 1), "mode": tag, "ok": self.ok, "err": self.err,
                    "p99_ms": round(p99 * 1000, 1) if p99 is not None else None,
                    "max_ms": round(lat[-1] * 1000, 1) if lat else None}
            self.reset()
        print(json.dumps(line), flush=True)


def reporter(st, tag, stop):
    # 用 wall clock 排程：容器內的單調時鐘比 wall clock 慢，time.sleep(1) 實際會超過 1 秒
    nxt = time.time() + 1
    while not stop.is_set():
        while time.time() < nxt:
            time.sleep(0.05)
        nxt = max(nxt + 1, time.time() + 0.5)
        st.flush(tag)


def run():
    threads, rate = int(os.environ.get("THREADS", 8)), float(os.environ.get("RATE", 40))
    st, stop = Stats(), threading.Event()

    def worker():
        c = None
        nxt = time.time()
        while True:
            try:
                if c is None:
                    c = conn()
                t0 = time.time()
                with c.cursor() as cur:
                    cur.execute("SELECT c FROM sb WHERE id=%s", (random.randint(1, ROWS),))
                    cur.fetchall()
                    cur.execute("UPDATE sb SET k=k+1 WHERE id=%s", (random.randint(1, ROWS),))
                c.commit()
                st.add(True, time.time() - t0)
            except Exception as e:  # noqa: BLE001
                st.add(False, err=errno(e))
                try:
                    c.rollback()
                except Exception:  # noqa: BLE001
                    try:
                        c.close()
                    except Exception:  # noqa: BLE001
                        pass
                    c = None
                    time.sleep(0.5)
            nxt += 1 / rate
            now = time.time()
            if nxt > now:
                time.sleep(nxt - now)
            else:
                nxt = now  # 落後時不補送

    for _ in range(threads):
        threading.Thread(target=worker, daemon=True).start()
    reporter(st, "run", stop)


def timed(fn, n, secs, tag):
    st, stop = Stats(), threading.Event()
    rep = threading.Thread(target=reporter, args=(st, tag, stop), daemon=True)
    rep.start()
    end = time.time() + secs
    ths = [threading.Thread(target=fn, args=(st, end), daemon=True) for _ in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    stop.set()
    st.flush(tag)


def storm(n, secs):
    def fn(st, end):
        while time.time() < end:
            t0 = time.time()
            try:
                c = conn(autocommit=True)
                with c.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM sb WHERE c LIKE '%%abc%%'")
                    cur.fetchall()
                c.close()
                st.add(True, time.time() - t0)
            except Exception as e:  # noqa: BLE001
                st.add(False, err=errno(e))
                time.sleep(0.2)
    timed(fn, n, secs, "storm")


def digests(n):
    c = conn(autocommit=True)
    with c.cursor() as cur:
        for i in range(n):
            # 別名是 digest 的一部分，所以每個 i 是一種新的 digest
            cur.execute(f"SELECT id AS col_{i} FROM sb WHERE id = 1")
            cur.fetchall()
    print(json.dumps({"t": round(time.time(), 1), "mode": "digests", "sent": n}), flush=True)


def slow(secs):
    def fn(st, end):
        c = conn(autocommit=True)
        i = 0
        while time.time() < end:
            t0 = time.time()
            with c.cursor() as cur:
                if i % 2:
                    cur.execute("SELECT SLEEP(1)")
                else:
                    cur.execute("SELECT id, c FROM sb ORDER BY pad DESC LIMIT 5")
                cur.fetchall()
            st.add(True, time.time() - t0)
            i += 1
    timed(fn, 2, secs, "slow")


def tmp(secs):
    def fn(st, end):
        c = conn(autocommit=True)
        with c.cursor() as cur:
            cur.execute("SET SESSION tmp_table_size=65536, max_heap_table_size=65536")
            while time.time() < end:
                t0 = time.time()
                cur.execute("SELECT k, COUNT(*), MAX(c) FROM sb WHERE id <= 40000 GROUP BY k ORDER BY 2 DESC LIMIT 3")
                cur.fetchall()
                st.add(True, time.time() - t0)
    timed(fn, 2, secs, "tmp")


def fill():
    c = conn(user="root", pw="rootpw", autocommit=True)
    t0 = time.time()
    with c.cursor() as cur:
        cur.execute("CREATE TABLE fill (id INT AUTO_INCREMENT PRIMARY KEY, pad VARCHAR(4000)) DATA DIRECTORY='/fill'")
        n = 0
        while True:
            try:
                cur.execute("INSERT INTO fill (pad) SELECT REPEAT('x', 4000) FROM d")
                n += 10
            except Exception as e:  # noqa: BLE001
                print(json.dumps({"t": round(time.time(), 1), "mode": "fill", "rows": n,
                                  "secs": round(time.time() - t0, 1), "error": str(e)[:200]}), flush=True)
                break
        # 滿了之後再試：同一張表、別的表
        for sql in ("INSERT INTO fill (pad) VALUES ('y')", "UPDATE sb SET k=k+1 WHERE id=1"):
            t1 = time.time()
            try:
                cur.execute(sql)
                res = "ok"
            except Exception as e:  # noqa: BLE001
                res = str(e)[:200]
            print(json.dumps({"t": round(time.time(), 1), "mode": "fill-after", "sql": sql,
                              "secs": round(time.time() - t1, 2), "result": res}), flush=True)


if __name__ == "__main__":
    m, a = sys.argv[1], sys.argv[2:]
    {"run": lambda: run(), "storm": lambda: storm(int(a[0]), float(a[1])),
     "digests": lambda: digests(int(a[0])), "slow": lambda: slow(float(a[0])),
     "tmp": lambda: tmp(float(a[0])), "fill": lambda: fill()}[m]()
