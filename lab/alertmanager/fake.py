#!/usr/bin/env python3
"""假接收端：一個行程同時開 HTTP webhook 與三種 SMTP。只用標準函式庫。

HTTP :8080
  POST /status/<code>[?tag=x]   回應指定的 HTTP 狀態碼，例如 /status/400、/status/500、/status/200
  GET  /stats                   每個 path（含 tag）收到幾次，以及 SMTP 的紀錄
  POST /release                 關掉所有卡住的 SMTP 連線
SMTP
  :2525 ok      正常收信，信件內容存在 /stats 的 mails
  :2526 reject  接受 MAIL、RCPT、DATA，在 DATA 結尾回 554
  :2527 hang    回應到 DATA 的 354，收完信之後不再回覆
連線被拒：連到這個容器沒有開的 port（例如 :9）即可。
"""
import asyncio
import json
import time
from collections import Counter

hits: Counter = Counter()
smtp_log: list = []
mails: list = []
hung: set = set()
T0 = time.time()


def now() -> float:
    return round(time.time() - T0, 3)


async def http_handler(reader, writer):
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except Exception:
        writer.close()
        return
    lines = head.decode(errors="replace").split("\r\n")
    method, path, _ = lines[0].split(" ", 2)
    length = 0
    for ln in lines[1:]:
        if ln.lower().startswith("content-length:"):
            length = int(ln.split(":", 1)[1])
    if length:
        await reader.readexactly(length)
    code, body = 200, b"ok"
    if path.startswith("/status/"):
        code = int(path.split("/")[2].split("?")[0])
        hits[path] += 1
        hits["t:" + path] = now()  # 最後一次的時間
    elif path == "/stats":
        body = json.dumps({"t": now(), "hits": hits, "smtp": smtp_log, "mails": mails,
                           "hung": len(hung)}, ensure_ascii=False).encode()
    elif path == "/release":
        n = len(hung)
        for w in list(hung):
            w.close()
        hung.clear()
        body = f"released {n}".encode()
    writer.write(f"HTTP/1.1 {code} X\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    await writer.drain()
    writer.close()


def smtp_factory(mode: str):
    async def handler(reader, writer):
        async def send(s):
            writer.write((s + "\r\n").encode())
            await writer.drain()
        await send("220 fake ESMTP")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                cmd = line.decode(errors="replace").strip().upper()
                if cmd.startswith("EHLO") or cmd.startswith("HELO"):
                    await send("250-fake\r\n250 8BITMIME")
                elif cmd.startswith("MAIL") or cmd.startswith("RCPT") or cmd.startswith("RSET"):
                    await send("250 ok")
                elif cmd == "DATA":
                    await send("354 go")
                    data = []
                    while True:
                        ln = await reader.readline()
                        if not ln or ln in (b".\r\n", b".\n"):
                            break
                        data.append(ln.decode(errors="replace"))
                    smtp_log.append({"t": now(), "mode": mode, "event": "data_end", "bytes": sum(map(len, data))})
                    if mode == "reject":
                        await send("554 5.7.1 rejected by fake")
                    elif mode == "hang":
                        hung.add(writer)
                        await asyncio.sleep(10 ** 6)
                    else:
                        mails.append({"t": now(), "msg": "".join(data)})
                        await send("250 queued")
                elif cmd == "QUIT":
                    await send("221 bye")
                    return
                else:
                    await send("250 ok")
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            hung.discard(writer)
            writer.close()
    return handler


async def main():
    await asyncio.start_server(http_handler, "0.0.0.0", 8080)
    for port, mode in ((2525, "ok"), (2526, "reject"), (2527, "hang")):
        await asyncio.start_server(smtp_factory(mode), "0.0.0.0", port)
    print("fake receivers ready", flush=True)
    await asyncio.Event().wait()


asyncio.run(main())
