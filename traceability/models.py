"""核心领域模型：机构、人员、产品、批次、支料、治疗会话与随访。

所有时间字段统一使用带时区的 datetime；判断“此刻是否有效”必须经过
注入的 Clock，避免在代码里散落 datetime.now()。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

# 医嘱组合上限：机构规则（见规则册 R601），单次治疗最多两种不同成分
MAX_DISTINCT_INGREDIENTS = 2
# 启封后最长使用时限（R503）
OPENED_TTL = timedelta(hours=4)
# 门诊病历法定留存年限：自治疗完成之日起 15 年
MEDICAL_RECORD_RETENTION_YEARS = 15
# 跨门店复诊病史回看窗口
HISTORY_LOOKBACK_DAYS = 365


class UnitStatus(str, Enum):
    IN_STOCK = "IN_STOCK"      # 在库可领用
    OPENED = "OPENED"          # 已启封，限当次当人
    CONSUMED = "CONSUMED"      # 已核销（任何重复扫描零扣减）
    DISCARDED = "DISCARDED"    # 余量按医疗废物弃置


class Decision(str, Enum):
    FIT = "FIT"                         # 适应
    FIT_WITH_CAUTION = "FIT_WITH_CAUTION"  # 谨慎适应（需记录注意事项）
    UNFIT = "UNFIT"                     # 不适应，硬性阻断治疗


@dataclass(frozen=True)
class Site:
    id: str
    name: str
    is_medical: bool                       # 是否取得《医疗机构执业许可证》
    medical_subject_registered: bool       # 是否登记医疗美容科诊疗科目
    subject_detail: str
    address: str


@dataclass(frozen=True)
class Staff:
    id: str
    name: str
    role: str                              # "doctor" | "nurse"
    license_no: str
    license_expiry: datetime
    practice_scopes: tuple[str, ...]       # 执业范围，如 ("医疗美容",)
    filed_site_ids: frozenset[str]         # 多机构执业备案/执业注册所在门店


@dataclass(frozen=True)
class Product:
    id: str
    generic_name: str                      # 成分通用名，禁止用套餐名代替
    approval_no: str                       # 医疗器械注册证/药品批准文号；"" 表示无
    kind: str                              # "医疗器械" | "药品"
    registered: bool                       # 批准文号是否真实在册
    max_dose_ml: float | None              # 单次最大用量（毫升/支当量）


@dataclass(frozen=True)
class TempReading:
    at: datetime
    temp_c: float


@dataclass(frozen=True)
class Recall:
    id: str
    batch_no: str
    reason: str
    at: datetime
    scope: str = "BATCH"                   # 召回仅作用于该批次，不波及其余批次


@dataclass
class Batch:
    batch_no: str
    product_id: str
    expiry: datetime
    storage_min_c: float
    storage_max_c: float
    inbound_at: datetime
    readings: list[TempReading] = field(default_factory=list)
    recalled: Recall | None = None
    quarantine: bool = False               # 检疫/冻结标记

    def cold_chain_ok(self) -> bool:
        """入库以来每一次温控读数都必须落在注册储运条件内。"""
        return all(
            self.storage_min_c <= r.temp_c <= self.storage_max_c for r in self.readings
        )


@dataclass
class Unit:
    """最小可追溯实物：一支预灌封/瓶装制剂。"""

    uid: str
    batch_no: str
    site_id: str
    fill_ml: float
    status: UnitStatus = UnitStatus.IN_STOCK
    opened_at: datetime | None = None
    opened_in_treatment: str | None = None
    consumed_in_treatment: str | None = None


@dataclass(frozen=True)
class ConsentLine:
    """逐项成分知情确认：一行只对应一种成分 + 一个批次 + 一支实物。"""

    product_id: str
    generic_name: str
    approval_no: str
    batch_no: str
    unit_uid: str
    confirmed_at: datetime


@dataclass(frozen=True)
class SkinAssessment:
    doctor_id: str
    at: datetime
    decision: Decision
    findings: tuple[str, ...]              # 皮肤状态所见
    contraindications_checked: tuple[str, ...]
    notes: str


@dataclass(frozen=True)
class UsageRecord:
    uid: str
    batch_no: str
    product_id: str
    anatomical_site: str
    dose_ml: float
    remainder_ml: float
    remainder_disposition: str             # 余量去向，如“余量当场按医疗废物弃置”
    at: datetime


@dataclass
class Treatment:
    id: str
    customer_id: str
    site_id: str
    doctor_id: str
    nurse_id: str
    anatomy_sites: list[str]
    planned_product_ids: list[str] = field(default_factory=list)
    assessment: SkinAssessment | None = None
    consents: list[ConsentLine] = field(default_factory=list)
    usages: list[UsageRecord] = field(default_factory=list)
    finalized: bool = False
    finalized_at: datetime | None = None
    rule_book_version: str | None = None


@dataclass(frozen=True)
class ConsentEvent:
    at: datetime
    allowed: bool


@dataclass
class Customer:
    id: str
    name: str
    consent_events: list[ConsentEvent] = field(default_factory=list)

    def marketing_allowed_at(self, at: datetime) -> bool:
        """营销授权按事件流判定；从未授权即默认拒绝。"""
        allowed = False
        for event in self.consent_events:
            if event.at <= at:
                allowed = event.allowed
        return allowed


@dataclass(frozen=True)
class FollowUpWindow:
    code: str
    label: str
    day_from: int
    day_to: int | None                    # None 表示开放窗口

    def contains(self, elapsed_days: int) -> bool:
        if self.day_to is None:
            return elapsed_days >= self.day_from
        return self.day_from <= elapsed_days <= self.day_to


FOLLOW_UP_WINDOWS: tuple[FollowUpWindow, ...] = (
    FollowUpWindow("EARLY", "短期反应窗（0-7天）", 0, 7),
    FollowUpWindow("INFECTION", "感染风险窗（8-30天）", 8, 30),
    FollowUpWindow("LATE", "迟发异常观察窗（31-180天）", 31, 180),
    FollowUpWindow("DELAYED", "迟发结节随访窗（180天以上）", 181, None),
)


@dataclass(frozen=True)
class FollowUpReport:
    id: str
    treatment_id: str
    customer_id: str
    at: datetime
    elapsed_days: int
    window_code: str
    symptoms: tuple[str, ...]
    chief_site: str
    level: int                             # 1 常规回访 / 2 尽快复诊 / 3 紧急复诊
    reminder: str                          # 复诊提醒文案，绝不构成诊断
