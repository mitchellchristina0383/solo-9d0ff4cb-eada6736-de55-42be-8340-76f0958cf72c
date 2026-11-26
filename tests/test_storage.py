"""存储层单元测试：原子写入、崩溃对账、快照一致性。"""

import io
import os
import shutil
import tempfile
import unittest

from rescue.storage import (
    MAX_IMAGE_SIZE,
    SECTOR_SIZE,
    JobExistsError,
    JobStorage,
    StorageError,
)


class StorageTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rescue-test-")
        self.storage = JobStorage(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_image(self, sectors: int) -> bytes:
        return bytes((i * 7 + 1) % 256 for i in range(sectors * SECTOR_SIZE))


class UploadTests(StorageTestBase):
    def test_valid_upload_creates_job(self):
        image = self.make_image(4)
        state = self.storage.create_job(io.BytesIO(image))
        self.assertEqual(state["sector_count"], 4)
        self.assertEqual(state["saved_count"], 0)
        self.assertEqual(state["missing_count"], 4)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "sample.img")))

    def test_empty_rejected(self):
        with self.assertRaises(StorageError):
            self.storage.create_job(io.BytesIO(b""))

    def test_non_multiple_of_512_rejected(self):
        with self.assertRaises(StorageError):
            self.storage.create_job(io.BytesIO(b"x" * (512 + 1)))

    def test_too_large_rejected(self):
        oversized = b"x" * (MAX_IMAGE_SIZE + SECTOR_SIZE)
        with self.assertRaises(StorageError):
            self.storage.create_job(io.BytesIO(oversized))
        # 超限后不留临时样盘
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "sample.img.tmp")))

    def test_8mib_boundary_accepted(self):
        image = self.make_image(MAX_IMAGE_SIZE // SECTOR_SIZE)
        state = self.storage.create_job(io.BytesIO(image))
        self.assertEqual(state["sector_count"], MAX_IMAGE_SIZE // SECTOR_SIZE)

    def test_duplicate_upload_rejected(self):
        self.storage.create_job(io.BytesIO(self.make_image(2)))
        with self.assertRaises(JobExistsError):
            self.storage.create_job(io.BytesIO(self.make_image(2)))


class SectorWriteTests(StorageTestBase):
    def setUp(self):
        super().setUp()
        self.image = self.make_image(6)
        self.storage.create_job(io.BytesIO(self.image))

    def test_save_and_readback(self):
        data = self.image[:512]
        self.assertTrue(self.storage.save_sector(0, data))
        self.assertEqual(self.storage.read_saved_sector(0), data)

    def test_duplicate_save_is_idempotent(self):
        self.storage.save_sector(1, self.image[512:1024])
        self.assertFalse(self.storage.save_sector(1, self.image[512:1024]))
        self.assertEqual(self.storage.get_state()["saved_count"], 1)

    def test_out_of_range_rejected(self):
        with self.assertRaises(StorageError):
            self.storage.save_sector(6, b"x" * 512)
        with self.assertRaises(StorageError):
            self.storage.save_sector(-1, b"x" * 512)

    def test_wrong_length_rejected(self):
        with self.assertRaises(StorageError):
            self.storage.save_sector(0, b"x" * 100)

    def test_all_zero_sector_is_saved_not_missing(self):
        # 实际读到的全零扇区必须正常保存，不算缺失
        self.storage.save_sector(2, b"\x00" * 512)
        state = self.storage.get_state()
        self.assertIn(2, state["saved"])
        self.assertNotIn(2, state["missing"])
        self.assertEqual(self.storage.read_saved_sector(2), b"\x00" * 512)

    def test_batch_mixed(self):
        result = self.storage.save_batch(
            [
                (0, self.image[:512]),
                (0, self.image[:512]),          # 重复
                (99, self.image[:512]),         # 越界
                (1, b"short"),                  # 长度错
                (2, self.image[1024:1536]),
            ]
        )
        self.assertEqual(result["saved_now"], [0, 2])
        self.assertEqual(result["already_saved"], [0])
        self.assertEqual(len(result["rejected"]), 2)

    def test_missing_list(self):
        for i in (0, 2, 4):
            self.storage.save_sector(i, self.image[i * 512 : (i + 1) * 512])
        self.assertEqual(self.storage.missing_sectors(), [1, 3, 5])


class RestartTests(StorageTestBase):
    def test_state_rebuilt_from_disk(self):
        image = self.make_image(5)
        self.storage.create_job(io.BytesIO(image))
        for i in (0, 1, 3):
            self.storage.save_sector(i, image[i * 512 : (i + 1) * 512])

        reopened = JobStorage(self.tmp)
        state = reopened.get_state()
        self.assertEqual(state["saved"], [0, 1, 3])
        self.assertEqual(state["missing"], [2, 4])
        self.assertEqual(reopened.read_saved_sector(1), image[512:1024])

    def test_half_written_tmp_treated_as_missing(self):
        image = self.make_image(4)
        self.storage.create_job(io.BytesIO(image))
        self.storage.save_sector(0, image[:512])

        # 伪造一个写到一半的临时文件（进程崩溃场景）
        shard = os.path.join(self.tmp, "sectors", "002")
        os.makedirs(shard, exist_ok=True)
        with open(os.path.join(shard, "2.tmp"), "wb") as fh:
            fh.write(b"partial")

        reopened = JobStorage(self.tmp)
        # 临时文件被清理
        self.assertFalse(os.path.exists(os.path.join(shard, "2.tmp")))
        state = reopened.get_state()
        self.assertEqual(state["saved"], [0])
        self.assertIn(2, state["missing"])

    def test_saved_file_without_state_entry_still_recognized(self):
        # job.json 落后于磁盘：磁盘上多出来的扇区也应被认回
        image = self.make_image(3)
        self.storage.create_job(io.BytesIO(image))
        self.storage.save_sector(0, image[:512])
        target = os.path.join(self.tmp, "sectors", "001", "1.bin")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(image[512:1024])

        reopened = JobStorage(self.tmp)
        self.assertEqual(reopened.get_state()["saved"], [0, 1])


class SnapshotTests(StorageTestBase):
    def setUp(self):
        super().setUp()
        self.image = self.make_image(8)
        self.storage.create_job(io.BytesIO(self.image))
        for i in (0, 1, 3, 4, 7):
            self.storage.save_sector(i, self.image[i * 512 : (i + 1) * 512])

    def test_snapshot_image_zero_fills_missing(self):
        snap = self.storage.create_snapshot()
        stream = self.storage.open_snapshot_image(snap["snapshot_id"])
        content = b"".join(stream)
        self.assertEqual(len(content), 8 * 512)
        zero = b"\x00" * 512
        for i in range(8):
            block = content[i * 512 : (i + 1) * 512]
            if i in (0, 1, 3, 4, 7):
                self.assertEqual(block, self.image[i * 512 : (i + 1) * 512])
            else:
                self.assertEqual(block, zero)

    def test_snapshot_missing_list_matches(self):
        snap = self.storage.create_snapshot()
        listing = self.storage.snapshot_missing_list(snap["snapshot_id"])
        self.assertEqual(listing["missing"], [2, 5, 6])
        self.assertEqual(listing["missing_count"], 3)
        self.assertEqual(listing["snapshot_id"], snap["snapshot_id"])

    def test_snapshot_immutable_while_reading_continues(self):
        snap = self.storage.create_snapshot()
        sid = snap["snapshot_id"]
        # 快照后继续救出扇区
        self.storage.save_sector(2, self.image[2 * 512 : 3 * 512])
        self.storage.save_sector(5, self.image[5 * 512 : 6 * 512])

        old_listing = self.storage.snapshot_missing_list(sid)
        self.assertEqual(old_listing["missing"], [2, 5, 6])  # 仍是旧进度
        content = b"".join(self.storage.open_snapshot_image(sid))
        self.assertEqual(content[2 * 512 : 3 * 512], b"\x00" * 512)  # 旧快照补零

        new_snap = self.storage.create_snapshot()
        new_listing = self.storage.snapshot_missing_list(new_snap["snapshot_id"])
        self.assertEqual(new_listing["missing"], [6])

    def test_unknown_snapshot_404(self):
        self.assertIsNone(self.storage.open_snapshot_image("nope"))
        self.assertIsNone(self.storage.snapshot_missing_list("nope"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
