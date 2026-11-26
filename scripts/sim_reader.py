#!/usr/bin/env python3
"""模拟恢复工具：对样盘逐扇区“读盘”，把读出的扇区上报给救援页。

用法：
  python3 scripts/sim_reader.py --base http://127.0.0.1:8000 \
      --image ./sample.img --fail 3,7,1024 --rounds 3

规则：
- --fail 给出本轮读不出的扇区；失败就跳过、继续往后读。
- 每轮开始先拉取缺失清单，只补读仍缺失的扇区，已救出的不重复上报。
- 可加 --delay 放慢节奏，配合人工测试“边读边下载”。
- 并发用线程池，上报内容为 base64 JSON。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def http_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise SystemExit(f"HTTP {exc.code} {url}: {body}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--image", required=True, help="本地样盘路径（512 整数倍）")
    parser.add_argument("--fail", default="", help="本轮读不出的扇区号，逗号分隔")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--delay", type=float, default=0.0, help="每个扇区上报前延迟秒数")
    args = parser.parse_args()

    fail_set = {int(x) for x in args.fail.split(",") if x.strip()}

    with open(args.image, "rb") as fh:
        image = fh.read()
    if len(image) % 512:
        raise SystemExit("样盘不是 512 字节整数倍")
    total = len(image) // 512

    for round_no in range(1, args.rounds + 1):
        status = http_json("GET", f"{args.base}/api/job")["progress"]
        missing = set(status["missing"])
        targets = [i for i in range(total) if i in missing and i not in fail_set]
        print(
            f"[round {round_no}] 缺失 {len(missing)} 个，本轮可读 {len(targets)} 个，"
            f"故障扇区 {sorted(fail_set)}",
            flush=True,
        )

        def report(index: int) -> int:
            if args.delay:
                time.sleep(args.delay)
            data = image[index * 512 : (index + 1) * 512]
            http_json(
                "POST",
                f"{args.base}/api/sectors",
                {"sectors": [{"sector": index, "data": base64.b64encode(data).decode()}]},
            )
            return index

        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for index in pool.map(report, targets):
                done += 1
                if done % 200 == 0 or done == len(targets):
                    print(f"  已上报 {done}/{len(targets)}（最后扇区 {index}）", flush=True)

        status = http_json("GET", f"{args.base}/api/job")["progress"]
        print(
            f"[round {round_no}] 结束：已救 {status['saved_count']}/{status['sector_count']}，"
            f"仍缺 {status['missing_count']}",
            flush=True,
        )

    final = http_json("GET", f"{args.base}/api/job")["progress"]
    print(json.dumps(final, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
