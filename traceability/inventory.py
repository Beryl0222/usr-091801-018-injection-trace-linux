"""批次库存与支料移动。

核销（库存扣减）只在治疗 finalize 放行通过后发生，并以支料 uid 为幂等键：
任何原因的重复扫描、重复 finalize 都不会产生第二次扣减。
"""

from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from .errors import Blocked, NotFound
from .models import Batch, Unit, UnitStatus


@dataclass(frozen=True)
class StockMovement:
    uid: str
    batch_no: str
    treatment_id: str
    at: datetime
    kind: str = "CONSUME"          # 当前只有核销扣减一类移动


@dataclass(frozen=True)
class ScanEvent:
    uid: str
    treatment_id: str
    at: datetime
    repeated: bool                 # 是否重复扫描（不产生新状态）


class Inventory:
    def __init__(self):
        self._lock = RLock()       # 跨门店并发调用下保证核销原子
        self.batches: dict[str, Batch] = {}
        self.units: dict[str, Unit] = {}
        self.movements: list[StockMovement] = []
        self.scan_log: list[ScanEvent] = []
        self._consumed: set[str] = set()

    # ---- 入库 ----------------------------------------------------------
    def register_batch(self, batch: Batch):
        self.batches[batch.batch_no] = batch

    def inbound_unit(self, unit: Unit):
        if unit.batch_no not in self.batches:
            raise NotFound(f"支料 {unit.uid} 的批号 {unit.batch_no} 未先登记入库温控")
        self.units[unit.uid] = unit

    def get_unit(self, uid: str) -> Unit:
        unit = self.units.get(uid)
        if unit is None:
            raise NotFound(f"条码 {uid} 无法识别：无入库记录，不产生任何库存移动")
        return unit

    def recall(self, recall):
        batch = self.batches.get(recall.batch_no)
        if batch is None:
            raise NotFound(f"召回批号 {recall.batch_no} 无入库记录")
        batch.recalled = recall

    # ---- 扫描启封 ------------------------------------------------------
    def scan(self, uid: str, treatment_id: str, at: datetime) -> ScanEvent:
        """扫描支料。

        - 在库：启封并绑定本次治疗；
        - 已由本次治疗启封：重复扫描幂等返回（repeated=True），零状态变更；
        - 已被其他治疗启封/核销：R502 硬性阻断，且不产生任何移动。
        """
        with self._lock:
            unit = self.get_unit(uid)
            if unit.status == UnitStatus.IN_STOCK:
                unit.status = UnitStatus.OPENED
                unit.opened_at = at
                unit.opened_in_treatment = treatment_id
                event = ScanEvent(uid, treatment_id, at, repeated=False)
            elif unit.status == UnitStatus.OPENED and unit.opened_in_treatment == treatment_id:
                event = ScanEvent(uid, treatment_id, at, repeated=True)
            elif unit.status == UnitStatus.CONSUMED:
                raise Blocked(
                    "R505", "核销幂等",
                    f"支料 {uid} 已被治疗 {unit.consumed_in_treatment} 核销，"
                    "重复扫描零扣减并阻断（全局不得二次使用）",
                )
            elif unit.status == UnitStatus.DISCARDED:
                raise Blocked(
                    "R501", "实物支料",
                    f"支料 {uid} 已按医疗废物弃置，扫描不产生任何移动",
                )
            else:
                raise Blocked(
                    "R502", "一启一患",
                    f"支料 {uid} 已在治疗 {unit.opened_in_treatment} 启封，不得跨治疗复用",
                )
            self.scan_log.append(event)
            return event

    # ---- 核销（仅由 finalize 放行后调用）-------------------------------
    def consume_for(self, treatment_id: str, uids, at: datetime) -> list[StockMovement]:
        """原子核销一组支料。每个 uid 全局只扣减一次。

        已被其他治疗核销的 uid 触发断言式阻断；同一治疗重复提交返回空列表，
        不产生第二条移动。
        """
        with self._lock:
            created: list[StockMovement] = []
            for uid in uids:
                unit = self.units[uid]
                if uid in self._consumed or unit.status == UnitStatus.CONSUMED:
                    if unit.consumed_in_treatment == treatment_id:
                        continue  # 同一治疗重复 finalize：幂等，零扣减
                    raise Blocked(
                        "R505", "核销幂等",
                        f"支料 {uid} 已被治疗 {unit.consumed_in_treatment} 核销，全局不得二次扣减",
                    )
            for uid in uids:
                unit = self.units[uid]
                if unit.consumed_in_treatment == treatment_id and unit.status == UnitStatus.CONSUMED:
                    continue
                unit.status = UnitStatus.CONSUMED
                unit.consumed_in_treatment = treatment_id
                movement = StockMovement(uid, unit.batch_no, treatment_id, at)
                self.movements.append(movement)
                self._consumed.add(uid)
                created.append(movement)
            return created

    def discard_opened(self, uid: str):
        """治疗未归档时，已启封支料不得回库，按医疗废物弃置。"""
        with self._lock:
            unit = self.get_unit(uid)
            if unit.status == UnitStatus.OPENED:
                unit.status = UnitStatus.DISCARDED

    # ---- 查询 ----------------------------------------------------------
    def stock_units(self, batch_no: str) -> int:
        return sum(
            1 for u in self.units.values()
            if u.batch_no == batch_no and u.status == UnitStatus.IN_STOCK
        )

    def deduction_count(self, uid: str) -> int:
        """某支料的实际扣减次数——幂等性验收指标，必须恒为 0 或 1。"""
        return sum(1 for m in self.movements if m.uid == uid)
