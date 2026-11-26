#!/usr/bin/env python3
"""冒烟脚本：起服务 -> 生成样盘 -> sim_reader 两轮读盘 -> 快照校验。

运行：python3 -I tests/smoke_test.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))

from tests.integration_test import (  # noqa: E402
    Client,
    make_image,
    start_server,
)

SECTORS = 300
PORT = 8920


def main() -> int:
    work = tempfile.mkdtemp(prefix="rescue-smoke-")
    sample_dir = tempfile.mkdtemp(prefix="rescue-sample-")
    sample = os.path.join(sample_dir, "sample.img")
    image = make_image(SECTORS)
    with open(sample, "wb") as fh:
        fh.write(image)
    print(f"样盘：{sample}（{SECTORS} 扇区 / {len(image)} 字节）")

    proc = start_server(work, PORT)
    try:
        client = Client(f"http://127.0.0.1:{PORT}")
        with open(sample, "rb") as fh:
            client.upload(fh.read())

        sim = [sys.executable, "-I", os.path.join(ROOT, "scripts", "sim_reader.py"),
               "--base", f"http://127.0.0.1:{PORT}", "--image", sample,
               "--fail", "5,60,123,250", "--rounds", "2", "--workers", "8"]
        r1 = subprocess.run(sim, capture_output=True, text=True, check=True)
        print(r1.stdout)
        state = client.get_json("/api/job")["progress"]
        assert state["missing"] == [5, 60, 123, 250], state["missing"]

        # 第二轮：故障扇区恢复一部分
        sim2 = sim.copy()
        sim2[sim2.index("--fail") + 1] = "250"
        r2 = subprocess.run(sim2, capture_output=True, text=True, check=True)
        print(r2.stdout)
        state = client.get_json("/api/job")["progress"]
        assert state["missing"] == [250], state["missing"]
        assert state["saved_count"] == SECTORS - 1

        # 最后一轮补完
        sim3 = sim2.copy()
        sim3[sim3.index("--fail") + 1] = ""
        r3 = subprocess.run(sim3, capture_output=True, text=True, check=True)
        state = client.get_json("/api/job")["progress"]
        assert state["complete"], state

        snap = client.post_json("/api/snapshots", {})["snapshot"]
        _, img = client.get_bytes(f"/api/snapshots/{snap['snapshot_id']}/image.bin")
        assert img == image, "最终镜像与样盘不一致"
        print("冒烟测试通过 ✅  snapshot =", snap["snapshot_id"])
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
