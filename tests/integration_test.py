#!/usr/bin/env python3
"""端到端集成测试：真实启动 Flask 服务，验证两个关键场景。

场景 A 中断后补读：
  第 1 轮带着坏扇区读盘 -> 杀掉服务 -> 重启 -> 第 2 轮只补缺失扇区 -> 完成。

场景 B 边读边下载：
  生成快照后，一边继续上报扇区一边下载镜像与缺失清单，校验两者属于
  同一 snapshot_id、内容互相吻合（缺失位补零、已读位与样盘一致）。

运行：python3 -I tests/integration_test.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SECTOR_SIZE = 512


# ---------------------------------------------------------------------- #
# 小型 HTTP 客户端（标准库实现 multipart 上传）
# ---------------------------------------------------------------------- #
class Client:
    def __init__(self, base: str):
        self.base = base

    def _open(self, method: str, path: str, data=None, headers=None):
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers or {}, method=method
        )
        return urllib.request.urlopen(req, timeout=30)

    def upload(self, image: bytes):
        boundary = "----rescue-boundary-" + uuid.uuid4().hex
        body = io.BytesIO()
        body.write(f"--{boundary}\r\n".encode())
        body.write(
            b'Content-Disposition: form-data; name="image"; filename="sample.img"\r\n'
        )
        body.write(b"Content-Type: application/octet-stream\r\n\r\n")
        body.write(image)
        body.write(f"\r\n--{boundary}--\r\n".encode())
        return json.loads(
            self._open(
                "POST",
                "/api/job",
                body.getvalue(),
                {"Content-Type": f"multipart/form-data; boundary={boundary}"},
            ).read()
        )

    def get_json(self, path: str):
        return json.loads(self._open("GET", path).read())

    def post_json(self, path: str, payload: dict):
        return json.loads(
            self._open(
                "POST",
                path,
                json.dumps(payload).encode(),
                {"Content-Type": "application/json"},
            ).read()
        )

    def get_bytes(self, path: str):
        resp = self._open("GET", path)
        return resp.headers, resp.read()

    def delete(self, path: str):
        return json.loads(self._open("DELETE", path).read())


def wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise TimeoutError(f"端口 {port} 未在 {timeout}s 内就绪")


def start_server(data_dir: str, port: int) -> subprocess.Popen:
    env = dict(os.environ, RESCUE_DATA_DIR=data_dir, PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, "-I", os.path.join(ROOT, "wsgi.py")],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_port(port)
    except Exception:
        proc.terminate()
        print(proc.stdout.read().decode(errors="replace"))
        raise
    return proc


def make_image(sectors: int) -> bytes:
    # 确定性内容，并故意放入几个全零扇区
    buf = bytearray(sectors * SECTOR_SIZE)
    for i in range(sectors * SECTOR_SIZE):
        buf[i] = (i * 31 + 7) % 256
    for zero_sector in (2, 9):
        buf[zero_sector * SECTOR_SIZE : (zero_sector + 1) * SECTOR_SIZE] = b"\x00" * SECTOR_SIZE
    return bytes(buf)


def report_sectors(client: Client, image: bytes, indexes) -> None:
    entries = [
        {
            "sector": i,
            "data": base64.b64encode(image[i * 512 : (i + 1) * 512]).decode(),
        }
        for i in indexes
    ]
    # 分批，每批 25 个
    for start in range(0, len(entries), 25):
        client.post_json("/api/sectors", {"sectors": entries[start : start + 25]})


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError("校验失败：" + message)
    print("  ✓", message)


def main() -> int:
    sectors = 40
    image = make_image(sectors)
    base_port = 8910

    # ================= 场景 A：中断后补读 =================
    print("== 场景 A：中断重启 + 下一轮补读 ==")
    work = tempfile.mkdtemp(prefix="rescue-e2e-a-")
    port = base_port
    client = Client(f"http://127.0.0.1:{port}")

    proc = start_server(work, port)
    try:
        state = client.upload(image)["progress"]
        check(state["sector_count"] == sectors, f"建任务 {sectors} 扇区")
        check(state["sample_sha256"] == hashlib.sha256(image).hexdigest(),
              "样盘 SHA-256 与本地一致")

        round1_fail = {3, 11, 17, 22, 35}
        round1_read = [i for i in range(sectors) if i not in round1_fail]
        report_sectors(client, image, round1_read)
        state = client.get_json("/api/job")["progress"]
        check(state["saved_count"] == sectors - len(round1_fail),
              f"第 1 轮救出 {state['saved_count']} 个")
        check(sorted(state["missing"]) == sorted(round1_fail),
              "缺失清单恰好是本轮坏扇区")

        # 伪造写到一半的临时文件，验证重启对账
        shard = os.path.join(work, "sectors", f"{11 % 1000:03d}")
        os.makedirs(shard, exist_ok=True)
        with open(os.path.join(shard, "11.tmp"), "wb") as fh:
            fh.write(b"half-written-data")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        print("  → 服务已停止（模拟值班中断）")

    # 重启：同一数据目录
    proc = start_server(work, port)
    try:
        client = Client(f"http://127.0.0.1:{port}")
        state = client.get_json("/api/job")["progress"]
        check(state["saved_count"] == sectors - len(round1_fail),
              "重启后已完整保存的扇区全部仍可用")
        check(11 in state["missing"], "写到一半的 11.tmp 被清理，扇区 11 仍算缺失")
        check(not os.path.exists(os.path.join(shard, "11.tmp")), "临时文件已对账删除")

        # 第 2 轮：只拉缺失清单补读（坏扇区集合换成另一组，35 仍坏）
        round2_fail = {35}
        missing = client.get_json("/api/missing")["missing"]
        round2_read = [i for i in missing if i not in round2_fail]
        report_sectors(client, image, round2_read)
        state = client.get_json("/api/job")["progress"]
        check(state["saved_count"] == sectors - 1, "第 2 轮后只剩 1 个坏扇区")
        check(state["missing"] == [35], "仍缺失的正是还没修好的 35")

        # 第 3 轮：全部恢复
        report_sectors(client, image, client.get_json("/api/missing")["missing"])
        state = client.get_json("/api/job")["progress"]
        check(state["complete"] is True and state["missing_count"] == 0,
              "全部扇区救出，任务完成")

        snap = client.post_json("/api/snapshots", {})["snapshot"]
        _, img = client.get_bytes(f"/api/snapshots/{snap['snapshot_id']}/image.bin")
        check(img == image, "完整救援镜像与样盘逐字节一致")
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    # ================= 场景 B：边读边下载 =================
    print("\n== 场景 B：下载期间读盘继续，快照内容固定 ==")
    work_b = tempfile.mkdtemp(prefix="rescue-e2e-b-")
    port = base_port + 1
    proc = start_server(work_b, port)
    try:
        client = Client(f"http://127.0.0.1:{port}")
        client.upload(image)

        first_batch = list(range(0, 20))
        report_sectors(client, image, first_batch)

        snap = client.post_json("/api/snapshots", {})["snapshot"]
        sid = snap["snapshot_id"]
        check(snap["saved"] == first_batch, "快照记录生成瞬间的 20 个已救扇区")

        # 下载过程中继续读盘（先发起慢一点的镜像下载：分块读取）
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/snapshots/{sid}/image.bin", timeout=30
        )
        check(resp.headers["X-Snapshot-Id"] == sid, "镜像响应头带快照号")
        downloaded = bytearray()
        chunk = resp.read(512 * 4)  # 先读一部分，期间让读盘继续
        downloaded.extend(chunk)

        report_sectors(client, image, range(20, sectors))  # 全部剩余扇区
        progress_now = client.get_json("/api/job")["progress"]
        check(progress_now["complete"] is True, "下载期间读盘已把全部扇区救完")

        downloaded.extend(resp.read())
        resp.close()

        listing_headers, listing_raw = client.get_bytes(
            f"/api/snapshots/{sid}/missing.json"
        )
        listing = json.loads(listing_raw)
        check(listing_headers["X-Snapshot-Id"] == sid,
              "缺失清单与镜像对应同一 snapshot_id")
        check(listing["missing"] == list(range(20, sectors)),
              "清单仍是快照时刻的缺失（20..39），未被后续读盘改写")
        check(len(downloaded) == sectors * 512, "镜像长度等于整盘大小")

        # 镜像与清单必须互相吻合：清单缺失位补零，其余位等于样盘
        zero = b"\x00" * 512
        missing_set = set(listing["missing"])
        ok = True
        for i in range(sectors):
            block = bytes(downloaded[i * 512 : (i + 1) * 512])
            expect = zero if i in missing_set else image[i * 512 : (i + 1) * 512]
            if block != expect:
                ok = False
                break
        check(ok, "镜像逐扇区与清单吻合：缺失补零、已读原样（含真实全零扇区）")

        # 再生成一份新快照，应显示已全部救出
        snap2 = client.post_json("/api/snapshots", {})["snapshot"]
        check(snap2["missing"] == [], "新快照反映最新进度：无缺失")
        _, img2 = client.get_bytes(f"/api/snapshots/{snap2['snapshot_id']}/image.bin")
        check(img2 == image, "新快照镜像与样盘逐字节一致")
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print("\n全部集成测试通过 ✅")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        print("HTTP 错误", exc.code, exc.read().decode(errors="replace"))
        raise
