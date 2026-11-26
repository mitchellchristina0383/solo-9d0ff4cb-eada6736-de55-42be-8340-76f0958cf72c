"""快照存储规模与并发语义测试。

重点验证任务要求：
- 单个任务 N 个扇区、M 份快照，长期保留的占用为 O(N+M)，且与 N 无关；
- 进度持续变化（每两份快照之间都在补读）时同样成立；
- 旧快照在后续读盘、下载交错进行时内容不变、链接始终可用；
- 组装缺失清单 / 传输镜像期间，其他线程仍能保存扇区。
"""

import io
import os
import shutil
import tempfile
import threading
import time
import tracemalloc
import unittest

from rescue.storage import SECTOR_SIZE, JobStorage

# 内部快照记录允许出现的字段：全部是标量/短字符串，不含整盘列表。
SNAPSHOT_FIELDS = {
    "snapshot_id",
    "sector_count",
    "sector_size",
    "seq",
    "sample_sha256",
    "created_at",
}
# 单份快照记录（含 dict 自身）必须远小于一份整盘编号列表。
PER_SNAPSHOT_BUDGET = 2048  # 字节


def _deep_size(obj) -> int:
    """粗略估算对象的常驻字节数（dict/list/str/int 递归）。"""
    seen = set()
    stack = [obj]
    total = 0
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        total += _object_size(cur)
        if isinstance(cur, dict):
            stack.extend(cur.keys())
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple, set, frozenset)):
            stack.extend(cur)
    return total


def _object_size(obj) -> int:
    import sys

    size = sys.getsizeof(obj)
    if isinstance(obj, str):
        # 对象本身之外，内容通常已是独立分配；getsizeof 已计入。
        return size
    return size


def make_image(sectors: int) -> bytes:
    return bytes((i * 11 + 3) % 256 for i in range(sectors * SECTOR_SIZE))


class SnapshotRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rescue-scale-")
        self.storage = JobStorage(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, sectors: int, snapshots: int) -> "JobStorage":
        """建任务；每轮救出一批扇区后生成一份快照。"""
        tmp = tempfile.mkdtemp(prefix="rescue-scale-run-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        storage = JobStorage(tmp)
        image = make_image(sectors)
        storage.create_job(io.BytesIO(image))
        batch = max(1, sectors // snapshots)
        saved = 0
        for _ in range(snapshots):
            end = min(sectors, saved + batch)
            if end > saved:
                storage.save_batch(
                    (i, image[i * SECTOR_SIZE : (i + 1) * SECTOR_SIZE])
                    for i in range(saved, end)
                )
                saved = end
            storage.create_snapshot()
        return storage

    def test_each_snapshot_record_has_no_full_lists(self):
        image = make_image(64)
        self.storage.create_job(io.BytesIO(image))
        self.storage.save_sector(0, image[:SECTOR_SIZE])
        snap = self.storage.create_snapshot()
        stored = self.storage._snapshots[snap["snapshot_id"]]
        self.assertEqual(set(stored), SNAPSHOT_FIELDS)
        self.assertLessEqual(_deep_size(stored), PER_SNAPSHOT_BUDGET)
        # 对外响应仍保留原有字段，且内容按快照时刻计算。
        for key in ("snapshot_id", "sector_count", "saved", "missing"):
            self.assertIn(key, snap)
        self.assertEqual(snap["saved"], [0])

    def test_retention_independent_of_sector_count(self):
        # 同样的 120 份快照：小盘与大盘的保留占用应基本一致。
        small = self._run(8, 120)
        big = self._run(2048, 120)
        small_bytes = _deep_size(small._snapshots)
        big_bytes = _deep_size(big._snapshots)

        self.assertEqual(len(small._snapshots), 120)
        self.assertEqual(len(big._snapshots), 120)
        # 大盘多 256 倍扇区，快照保留占用不应随 N 放大。
        self.assertLess(big_bytes, small_bytes * 3 + 16 * 1024)
        self.assertLess(big_bytes, 120 * PER_SNAPSHOT_BUDGET + 32 * 1024)

    def test_memory_growth_tracks_snapshots_not_progress(self):
        # tracemalloc 观测：随进度增加只保留一份已救序列 + 每快照 O(1)。
        image = make_image(1024)
        self.storage.create_job(io.BytesIO(image))
        tracemalloc.start()
        try:
            base = tracemalloc.take_snapshot()
            for i in range(1024):
                self.storage.save_sector(i, image[i * SECTOR_SIZE : (i + 1) * SECTOR_SIZE])
                if i % 8 == 0:
                    self.storage.create_snapshot()
            after = tracemalloc.take_snapshot()
            grown = sum(
                stat.size_diff
                for stat in after.compare_to(base, "filename")
            )
        finally:
            tracemalloc.stop()
        # 128 份快照 + 1024 个已救扇区编号，远小于「每快照两份整盘列表」
        # 会产生的量级（旧实现约 128 * 2 * 512 个整数 ≈ 数十 MB）。
        self.assertLess(grown, 2 * 1024 * 1024)


class InterleavedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rescue-interleave-")
        self.storage = JobStorage(self.tmp)
        self.sectors = 96
        self.image = make_image(self.sectors)
        self.storage.create_job(io.BytesIO(self.image))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _save(self, indexes):
        self.storage.save_batch(
            (i, self.image[i * SECTOR_SIZE : (i + 1) * SECTOR_SIZE])
            for i in indexes
        )

    def test_many_snapshots_keep_their_own_progress(self):
        created = []
        # 交错：读一批 -> 快照 -> 再读一批 -> 快照 ...
        for round_no in range(8):
            self._save(range(round_no * 10, round_no * 10 + 10))
            snap = self.storage.create_snapshot()
            created.append((snap["snapshot_id"], round(round_no + 1) * 10))

        # 继续补读剩余扇区，旧快照不受影响。
        self._save(range(80, self.sectors))

        for sid, expected_saved in created:
            listing = self.storage.snapshot_missing_list(sid)
            self.assertEqual(
                listing["missing"],
                [i for i in range(self.sectors) if i >= expected_saved],
            )
            content = b"".join(self.storage.open_snapshot_image(sid))
            self.assertEqual(len(content), self.sectors * SECTOR_SIZE)
            for i in range(self.sectors):
                block = content[i * SECTOR_SIZE : (i + 1) * SECTOR_SIZE]
                if i < expected_saved:
                    self.assertEqual(block, self.image[i * SECTOR_SIZE : (i + 1) * SECTOR_SIZE])
                else:
                    self.assertEqual(block, b"\x00" * SECTOR_SIZE)

        # 最新快照反映最终进度。
        latest = self.storage.create_snapshot()
        self.assertEqual(latest["missing"], [])

    def test_old_snapshot_readable_after_many_later_writes(self):
        self._save(range(0, 10))
        sid = self.storage.create_snapshot()["snapshot_id"]
        for start in range(10, self.sectors, 10):
            self._save(range(start, min(self.sectors, start + 10)))
            self.storage.create_snapshot()
        # 旧链接仍然可用，且内容与创建时一致。
        self.assertIsNotNone(self.storage.open_snapshot_image(sid))
        listing = self.storage.snapshot_missing_list(sid)
        self.assertEqual(listing["missing"], list(range(10, self.sectors)))
        content = b"".join(self.storage.open_snapshot_image(sid))
        self.assertEqual(content[10 * SECTOR_SIZE : 11 * SECTOR_SIZE], b"\x00" * SECTOR_SIZE)
        self.assertEqual(content[: SECTOR_SIZE], self.image[: SECTOR_SIZE])

    def test_zero_sectors_count_as_saved_in_snapshot(self):
        # 索引 5 是真实读到的全零扇区，应算已救、镜像里原样为零但不在缺失清单。
        zero_image = bytearray(self.image)
        zero_image[5 * SECTOR_SIZE : 6 * SECTOR_SIZE] = b"\x00" * SECTOR_SIZE
        tmp = tempfile.mkdtemp(prefix="rescue-zero-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        storage = JobStorage(tmp)
        storage.create_job(io.BytesIO(bytes(zero_image)))
        storage.save_sector(5, b"\x00" * SECTOR_SIZE)
        snap = storage.create_snapshot()
        self.assertIn(5, snap["saved"])
        self.assertNotIn(5, snap["missing"])
        listing = storage.snapshot_missing_list(snap["snapshot_id"])
        self.assertNotIn(5, listing["missing"])

    def test_saves_proceed_while_image_is_streamed(self):
        self._save(range(0, 8))
        sid = self.storage.create_snapshot()["snapshot_id"]
        stream = self.storage.open_snapshot_image(sid)

        written = []
        errors = []

        def writer():
            try:
                for i in range(8, self.sectors):
                    self.storage.save_sector(
                        i, self.image[i * SECTOR_SIZE : (i + 1) * SECTOR_SIZE]
                    )
                    written.append(i)
                    time.sleep(0.001)
            except Exception as exc:  # pragma: no cover - 失败时提供诊断
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        content = bytearray()
        try:
            for chunk in stream:
                content.extend(chunk)
                # 模拟慢速下载：主动让出，给写线程推进的机会。
                time.sleep(0.003)
        finally:
            thread.join(timeout=20)

        self.assertFalse(errors)
        self.assertEqual(written, list(range(8, self.sectors)))
        # 快照内容固定：后 8.. 扇区仍是补零，不因下载期间补读而变化。
        for i in range(8, self.sectors):
            self.assertEqual(
                bytes(content[i * SECTOR_SIZE : (i + 1) * SECTOR_SIZE]),
                b"\x00" * SECTOR_SIZE,
            )
        self.assertEqual(len(content), self.sectors * SECTOR_SIZE)

    def test_missing_list_assembly_does_not_block_saves(self):
        self._save(range(0, 4))
        sid = self.storage.create_snapshot()["snapshot_id"]
        done = threading.Event()
        start = threading.Event()

        def builder():
            start.set()
            self.storage.snapshot_missing_list(sid)
            done.set()

        thread = threading.Thread(target=builder)
        thread.start()
        start.wait(timeout=5)
        # 清单组装期间保存扇区应立即成功，不被存储锁长期阻塞。
        self.assertTrue(
            self.storage.save_sector(
                50, self.image[50 * SECTOR_SIZE : 51 * SECTOR_SIZE]
            )
        )
        thread.join(timeout=5)
        self.assertTrue(done.is_set())
        # 旧快照清单不变。
        self.assertIn(50, self.storage.snapshot_missing_list(sid)["missing"])


class RestartOrderTests(unittest.TestCase):
    def test_saved_order_survives_restart_and_defines_prefixes(self):
        tmp = tempfile.mkdtemp(prefix="rescue-order-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        image = make_image(12)
        storage = JobStorage(tmp)
        storage.create_job(io.BytesIO(image))
        # 以非递增顺序救出，验证顺序被保留（影响前缀语义）。
        for i in (4, 0, 9, 2, 7):
            storage.save_sector(i, image[i * SECTOR_SIZE : (i + 1) * SECTOR_SIZE])

        reopened = JobStorage(tmp)
        self.assertEqual(reopened._saved_order, [4, 0, 9, 2, 7])
        snap = reopened.create_snapshot()
        self.assertEqual(sorted(snap["saved"]), [0, 2, 4, 7, 9])
        self.assertEqual(snap["missing"], [1, 3, 5, 6, 8, 10, 11])


if __name__ == "__main__":
    unittest.main(verbosity=2)
