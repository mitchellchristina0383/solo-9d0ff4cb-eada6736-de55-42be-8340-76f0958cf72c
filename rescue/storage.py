"""持久化层：样盘保存、扇区原子写入、崩溃对账、同进度快照。

设计要点
--------
- 镜像按 512 字节分扇区，最多 8 MiB（MAX_IMAGE_SIZE）。
- 每个已救出的扇区写成独立文件 sectors/XX/<index>.bin。
  写入走 "临时文件 + fsync + rename + 目录 fsync"，保证单扇区要么完整
  落盘、要么完全不存在；写到一半的临时文件在启动对账时删除，仍算缺失。
- job.json 是状态缓存，磁盘上的扇区文件才是真相；重启时以文件系统
  为准重建状态，因此中断重启后已完整保存的扇区仍然可用。
- 已救扇区在单个任务内只增不减，因此维护一份全局的「首次救出顺序」
  序列 _saved_order。快照只记录生成瞬间该序列的前缀长度 seq 与少量
  元数据；快照的 saved/missing 在需要时由前缀重建。
  这样 M 份快照的长期占用是 O(M)，连同 O(N) 的已救序列总占用
  O(N+M)，不再为每份快照各留一份整盘 saved 与 missing 列表。
  快照不落额外磁盘；生成后读盘继续进行不影响旧快照。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from typing import Dict, Iterable, List, Optional, Set, Tuple

SECTOR_SIZE = 512
MAX_IMAGE_SIZE = 8 * 1024 * 1024  # 8 MiB

JOB_FILE = "job.json"
SAMPLE_FILE = "sample.img"
SECTORS_DIR = "sectors"
TMP_SUFFIX = ".tmp"
_TMP_RE = re.compile(r"^(\d+)\.tmp$")
_SECTOR_RE = re.compile(r"^(\d+)\.bin$")


class StorageError(Exception):
    """请求无效（镜像不合规、扇区号越界等）。"""


class JobExistsError(StorageError):
    """已有一轮救援任务，拒绝再次上传。"""


def _shard_dir(sectors_root: str, index: int) -> str:
    return os.path.join(sectors_root, f"{index % 1000:03d}")


def _sector_path(sectors_root: str, index: int) -> str:
    return os.path.join(_shard_dir(sectors_root, index), f"{index}.bin")


class JobStorage:
    """一个工作目录对应一份救援任务，所有方法线程安全。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.job_path = os.path.join(data_dir, JOB_FILE)
        self.sample_path = os.path.join(data_dir, SAMPLE_FILE)
        self.sectors_root = os.path.join(data_dir, SECTORS_DIR)
        self._lock = threading.RLock()
        self._snapshots: Dict[str, dict] = {}
        self._snapshot_counter = 0
        # 首次救出顺序（append-only）。快照只保存该序列的前缀长度。
        self._saved_order: List[int] = []
        self._state: dict = self._load_or_init()

    # ------------------------------------------------------------------ #
    # 启动与状态
    # ------------------------------------------------------------------ #
    def _load_or_init(self) -> dict:
        os.makedirs(self.sectors_root, exist_ok=True)
        # 崩溃对账：删除任何写到一半的临时扇区文件与状态临时文件。
        for name in os.listdir(self.data_dir):
            if name.startswith(JOB_FILE) and name.endswith(TMP_SUFFIX):
                _unlink_quiet(os.path.join(self.data_dir, name))
        for shard in os.listdir(self.sectors_root):
            shard_path = os.path.join(self.sectors_root, shard)
            if not os.path.isdir(shard_path):
                continue
            for name in os.listdir(shard_path):
                if _TMP_RE.match(name):
                    try:
                        os.unlink(os.path.join(shard_path, name))
                    except FileNotFoundError:
                        pass

        saved = self._scan_saved_sectors()

        if os.path.exists(self.job_path):
            with open(self.job_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            # 磁盘真相优先：状态里声称已救、但文件不在了的扇区剔除。
            # 内部用集合维护，避免每次写入都对不断增长的列表排序（O(n^2)）。
            state["saved"] = {i for i in saved if i < state["sector_count"]}
        else:
            state = None

        if state is None:
            self._saved_order = []
            return {
                "sector_count": 0,
                "saved": set(),
                "sample_sha256": None,
                "sample_size": 0,
                "created_at": None,
            }

        # 重建首次救出序列：沿用 job.json 里的历史顺序，磁盘上额外出现
        # 的扇区按编号追加。只用于定义快照前缀，集合语义不变。
        self._saved_order = self._load_saved_order(state)
        self._persist_state(state)
        return state

    def _load_saved_order(self, state: dict) -> List[int]:
        saved: Set[int] = state["saved"]
        order: List[int] = []
        seen: Set[int] = set()
        for index in state.get("saved_order", []):
            if index in saved and index not in seen:
                order.append(index)
                seen.add(index)
        for index in sorted(saved - seen):
            order.append(index)
        return order

    def _scan_saved_sectors(self) -> Set[int]:
        saved: Set[int] = set()
        if not os.path.isdir(self.sectors_root):
            return saved
        for shard in os.listdir(self.sectors_root):
            shard_path = os.path.join(self.sectors_root, shard)
            if not os.path.isdir(shard_path):
                continue
            for name in os.listdir(shard_path):
                match = _SECTOR_RE.match(name)
                if match:
                    saved.add(int(match.group(1)))
        return saved

    def _persist_state(self, state: Optional[dict] = None) -> None:
        state = state or self._state
        # 临时名带线程标识，避免并发持久化时共用同一 tmp 路径。
        tmp_path = f"{self.job_path}.{os.getpid()}.{threading.get_ident()}.{TMP_SUFFIX}"
        payload = dict(state)
        payload["saved"] = sorted(state["saved"])  # 落盘时才排序
        # 首次救出顺序一并落盘，重启后快照前缀语义保持一致。
        payload["saved_order"] = list(self._saved_order)
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self.job_path)
        # fsync 目录，保证 rename 本身落盘。
        dir_fd = os.open(self.data_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def has_job(self) -> bool:
        with self._lock:
            return self._state["sector_count"] > 0

    def get_state(self) -> dict:
        with self._lock:
            return self._public_state(self._state)

    def _public_state(self, state: dict) -> dict:
        count = state["sector_count"]
        saved = sorted(state["saved"])
        saved_set = set(saved)
        missing = [i for i in range(count) if i not in saved_set]
        return {
            "sector_count": count,
            "saved_count": len(saved),
            "missing_count": len(missing),
            "saved": saved,
            "missing": missing,
            "sample_size": state["sample_size"],
            "sample_sha256": state["sample_sha256"],
            "created_at": state["created_at"],
            "complete": count > 0 and not missing,
        }

    def missing_sectors(self) -> List[int]:
        with self._lock:
            saved = self._state["saved"]
            return [i for i in range(self._state["sector_count"]) if i not in saved]

    # ------------------------------------------------------------------ #
    # 建任务：流式保存样盘
    # ------------------------------------------------------------------ #
    def create_job(self, stream) -> dict:
        """从上传流读取样盘并建任务。

        - 必须是 512 字节的整数倍，且 1 扇区 <= 大小 <= 8 MiB。
        - 流式落盘并实时校验大小，超限直接拒绝，避免把大文件读进内存。
        """
        with self._lock:
            if self.has_job():
                raise JobExistsError("已存在救援任务，请先重置后再上传新样盘")

            os.makedirs(self.sectors_root, exist_ok=True)
            tmp_sample = self.sample_path + TMP_SUFFIX
            hasher = hashlib.sha256()
            total = 0
            try:
                with open(tmp_sample, "wb") as fh:
                    while True:
                        chunk = stream.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_IMAGE_SIZE:
                            raise StorageError(
                                f"样盘超过上限 {MAX_IMAGE_SIZE} 字节（8 MiB）"
                            )
                        hasher.update(chunk)
                        fh.write(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
            except StorageError:
                _unlink_quiet(tmp_sample)
                raise
            except Exception:
                _unlink_quiet(tmp_sample)
                raise

            if total == 0:
                _unlink_quiet(tmp_sample)
                raise StorageError("上传内容为空")
            if total % SECTOR_SIZE != 0:
                _unlink_quiet(tmp_sample)
                raise StorageError(
                    f"样盘大小 {total} 字节不是 {SECTOR_SIZE} 字节的整数倍"
                )

            os.replace(tmp_sample, self.sample_path)
            sector_count = total // SECTOR_SIZE
            state = {
                "sector_count": sector_count,
                "saved": set(),
                "sample_sha256": hasher.hexdigest(),
                "sample_size": total,
                "created_at": _utc_now(),
            }
            self._state = state
            self._snapshots.clear()
            self._snapshot_counter = 0
            self._saved_order = []
            self._persist_state(state)
            return self._public_state(state)

    # ------------------------------------------------------------------ #
    # 读盘：逐个扇区原子写入
    # ------------------------------------------------------------------ #
    def save_sector(self, index: int, data: bytes, persist: bool = True) -> bool:
        """保存一个已读出的扇区。

        返回 True 表示本轮新写入，False 表示该扇区此前已完整保存。
        全零扇区只要真的读到了就正常保存，不算缺失。

        persist=False 时只更新内存状态、不写 job.json，供批量接口在
        整批结束时统一落一次状态；崩溃也不影响正确性——重启时以磁盘上
        的扇区文件为准重建状态。
        """
        with self._lock:
            count = self._state["sector_count"]
            if not self._valid_index(index, count):
                raise StorageError(f"扇区号越界：{index}，合法范围 0..{count - 1}")
            if len(data) != SECTOR_SIZE:
                raise StorageError(
                    f"扇区数据长度必须为 {SECTOR_SIZE} 字节，实际 {len(data)}"
                )

            target = _sector_path(self.sectors_root, index)
            if os.path.exists(target):
                return False

            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + TMP_SUFFIX
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
            # fsync 分片目录，保证 rename 落盘。
            dir_fd = os.open(os.path.dirname(target), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

            self._state["saved"].add(index)
            self._saved_order.append(index)
            if persist:
                self._persist_state()
            return True

    def save_batch(self, items: Iterable[Tuple[int, bytes]]) -> dict:
        """批量保存，扇区逐个原子落盘，整批只持久化一次状态。

        整批在同一把锁内完成，避免并发批量请求交错持久化状态。
        """
        saved_now: List[int] = []
        already: List[int] = []
        rejected: List[dict] = []
        with self._lock:
            for index, data in items:
                try:
                    is_new = self.save_sector(index, data, persist=False)
                except StorageError as exc:
                    rejected.append({"sector": index, "error": str(exc)})
                    continue
                (saved_now if is_new else already).append(index)
            if saved_now:
                self._persist_state()
        return {
            "saved_now": sorted(saved_now),
            "already_saved": sorted(already),
            "rejected": rejected,
            "progress": self.get_state(),
        }

    @staticmethod
    def _valid_index(index: int, count: int) -> bool:
        return isinstance(index, int) and 0 <= index < count

    def read_saved_sector(self, index: int) -> Optional[bytes]:
        with self._lock:
            count = self._state["sector_count"]
            if not self._valid_index(index, count):
                raise StorageError(f"扇区号越界：{index}")
            path = _sector_path(self.sectors_root, index)
            if not os.path.exists(path):
                return None
            with open(path, "rb") as fh:
                return fh.read()

    # ------------------------------------------------------------------ #
    # 同进度快照：镜像 + 缺失清单
    # ------------------------------------------------------------------ #
    def create_snapshot(self) -> dict:
        """生成一份不可变进度快照，镜像与缺失清单共用同一 snapshot_id。

        已救扇区只增不减，因此快照只需固定「首次救出顺序」的前缀长度 seq；
        之后读盘继续往 _saved_order 尾部追加，旧快照的前缀保持不变。
        每份快照只占 O(1) 内存、不写额外磁盘，M 份快照合计 O(M)。
        """
        with self._lock:
            count = self._state["sector_count"]
            if count == 0:
                raise StorageError("尚无救援任务")
            self._snapshot_counter += 1
            snapshot_id = f"s{self._snapshot_counter:06d}"
            snapshot = {
                "snapshot_id": snapshot_id,
                "sector_count": count,
                "sector_size": SECTOR_SIZE,
                # 快照时刻已确认落盘的扇区 = 该前缀内的扇区。
                "seq": len(self._saved_order),
                "sample_sha256": self._state["sample_sha256"],
                "created_at": _utc_now(),
            }
            self._snapshots[snapshot_id] = snapshot
            # 只复制前缀（已救扇区编号），组装视图放到锁外，避免长时间
            # 持锁阻塞其他请求保存扇区。
            prefix = self._saved_order[: snapshot["seq"]]
        return self._snapshot_view(snapshot, prefix)

    @staticmethod
    def _snapshot_view(snapshot: dict, prefix: List[int]) -> dict:
        """创建响应用：saved/missing 均按快照时刻计算，不会随后续读盘变化。"""
        saved = sorted(prefix)
        saved_set = set(saved)
        missing = [i for i in range(snapshot["sector_count"]) if i not in saved_set]
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "sector_count": snapshot["sector_count"],
            "sector_size": SECTOR_SIZE,
            "saved": saved,
            "missing": missing,
            "missing_count": len(missing),
            "sample_sha256": snapshot["sample_sha256"],
            "created_at": snapshot["created_at"],
        }

    def open_snapshot_image(self, snapshot_id: str):
        """按快照顺序流式产出救援镜像；缺失位置补零。

        生成器只持有快照前缀内的扇区编号副本，读盘继续进行不影响本流；
        组装与逐扇区读取均在锁外进行，不阻塞其他请求保存扇区。
        """
        with self._lock:
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None:
                return None
            count = snapshot["sector_count"]
            prefix = self._saved_order[: snapshot["seq"]]

        def generate():
            saved_set = set(prefix)
            zero = b"\x00" * SECTOR_SIZE
            for index in range(count):
                if index in saved_set:
                    path = _sector_path(self.sectors_root, index)
                    with open(path, "rb") as fh:
                        data = fh.read()
                    # 正常情况下长度恒为 SECTOR_SIZE，防御性补齐。
                    if len(data) < SECTOR_SIZE:
                        data = data + zero[len(data):]
                    yield data[:SECTOR_SIZE]
                else:
                    yield zero

        return generate()

    def list_snapshots(self) -> List[dict]:
        """已生成快照的概要，按生成先后排序（供救援页核对新旧快照）。

        只返回计数与元数据；整盘 saved/missing 列表仍在各自下载接口按
        快照前缀即时计算，不在快照记录中保留。
        """
        with self._lock:
            summaries = []
            for snapshot in self._snapshots.values():
                # _saved_order 内编号唯一（仅在文件尚不存在时追加，重启时
                # 去重），故前缀长度即已救数量；无需再构造集合，列表组装
                # 保持 O(M)。
                saved_count = snapshot["seq"]
                total = snapshot["sector_count"]
                summaries.append(
                    {
                        "snapshot_id": snapshot["snapshot_id"],
                        "sector_count": total,
                        "sector_size": SECTOR_SIZE,
                        "saved_count": saved_count,
                        "missing_count": total - saved_count,
                        "sample_sha256": snapshot["sample_sha256"],
                        "created_at": snapshot["created_at"],
                    }
                )
            return summaries

    def snapshot_missing_list(self, snapshot_id: str) -> Optional[dict]:
        with self._lock:
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None:
                return None
            prefix = self._saved_order[: snapshot["seq"]]
        # 锁外组装缺失清单，期间其他请求仍可保存扇区。
        saved_set = set(prefix)
        missing = [
            i for i in range(snapshot["sector_count"]) if i not in saved_set
        ]
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "created_at": snapshot["created_at"],
            "sector_count": snapshot["sector_count"],
            "sector_size": SECTOR_SIZE,
            "sample_sha256": snapshot["sample_sha256"],
            "missing_count": len(missing),
            "missing": missing,
        }

    def snapshot_sector_count(self, snapshot_id: str) -> Optional[int]:
        """快照的整盘扇区数（下载镜像时用于设置 Content-Length）。"""
        with self._lock:
            snapshot = self._snapshots.get(snapshot_id)
            return None if snapshot is None else snapshot["sector_count"]

    # ------------------------------------------------------------------ #
    def reset(self) -> None:
        """删除整个任务目录下的任务数据（供测试与重新建任务使用）。"""
        with self._lock:
            import shutil

            self._snapshots.clear()
            self._snapshot_counter = 0
            if os.path.isdir(self.sectors_root):
                shutil.rmtree(self.sectors_root)
            _unlink_quiet(self.job_path)
            _unlink_quiet(self.sample_path)
            os.makedirs(self.sectors_root, exist_ok=True)
            self._state = self._load_or_init()


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
