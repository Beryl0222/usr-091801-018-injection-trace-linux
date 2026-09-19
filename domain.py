"""医美注射全程追溯的领域模型与硬性规则。

水光注射只是一种操作方式，不代表固定产品：每一项成分都必须单独登记
批准文号、储运温度、启封时间与实际用量。治疗执行采用"先校验、后扣减"
两阶段：任一规则不满足都会硬性阻断，且不会留下半扣减的库存状态。
随访模块只生成分级复诊提醒，不给出任何诊断结论。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum


# ---------------------------------------------------------------- 阻断错误


class TraceabilityError(Exception):
    """追溯链路被硬性阻断的基类。"""


class LicenseError(TraceabilityError):
    """机构诊疗科目或人员资质不满足注射要求。"""


class UnknownMaterialError(TraceabilityError):
    """材料来源不明：缺少批准文号。"""


class CombinationRuleError(TraceabilityError):
    """成分组合或单次用量超出机构规则。"""


class ConsentError(TraceabilityError):
    """顾客未在治疗前逐项确认成分。"""


class AssessmentError(TraceabilityError):
    """缺少皮肤状态记录或适应性判断。"""


class BatchError(TraceabilityError):
    """批次不可用（如已过期）。"""


class RecalledBatchError(BatchError):
    """批次已召回，禁止继续用于新治疗。"""


class InventoryError(TraceabilityError):
    """库存扣减冲突：重复扣减、余量不足或跨治疗复用。"""


# ---------------------------------------------------------------- 规则标识
#
# 每次治疗强制执行的规则都会写入治疗记录，供上线前追查与事后审计。

RULE_INSTITUTION = "R1-机构诊疗科目核验"
RULE_PRACTITIONER = "R2-人员资质有效期核验"
RULE_MATERIAL_SOURCE = "R3-材料批准文号核验"
RULE_COMBINATION = "R4-机构组合规则核验"
RULE_BATCH = "R5-批次状态核验"
RULE_CONSENT = "R6-逐项成分确认核验"
RULE_ASSESSMENT = "R7-皮肤状态与适应性判断记录"

ALL_RULES = (
    RULE_INSTITUTION,
    RULE_PRACTITIONER,
    RULE_MATERIAL_SOURCE,
    RULE_COMBINATION,
    RULE_BATCH,
    RULE_CONSENT,
    RULE_ASSESSMENT,
)


# ---------------------------------------------------------------- 机构与人员


class Role(str, Enum):
    DOCTOR = "doctor"
    NURSE = "nurse"


@dataclass(frozen=True)
class InstitutionRules:
    """机构层面的组合与用量规则。"""

    required_subject: str = "美容皮肤科"
    max_materials_per_session: int = 3
    forbidden_combinations: tuple[tuple[str, ...], ...] = ()
    max_volume_ml_per_material: float = 5.0

    def check_combination(
        self,
        materials: Iterable[Material],
        volume_by_material: dict[str, float],
    ) -> None:
        """R4：材料种数、禁用成分组合与单材料用量都不得超机构规则。"""
        materials = list(materials)
        if len(materials) > self.max_materials_per_session:
            raise CombinationRuleError(
                f"单次治疗使用 {len(materials)} 种材料，"
                f"超过机构上限 {self.max_materials_per_session} 种"
            )
        used_ingredients = {ing for m in materials for ing in m.ingredients}
        for combo in self.forbidden_combinations:
            if set(combo) <= used_ingredients:
                raise CombinationRuleError(
                    f"成分组合 {'+'.join(combo)} 超出机构规则，禁止同次使用"
                )
        for material_id, volume in volume_by_material.items():
            if volume > self.max_volume_ml_per_material:
                raise CombinationRuleError(
                    f"材料 {material_id} 单次用量 {volume}ml "
                    f"超过机构上限 {self.max_volume_ml_per_material}ml"
                )


@dataclass(frozen=True)
class Institution:
    """机构：无医疗机构执业许可的即为生活美容机构，不得开展注射。"""

    institution_id: str
    name: str
    medical_license_no: str | None
    subjects: frozenset[str]
    rules: InstitutionRules = InstitutionRules()

    def check_injectable(self) -> None:
        """R1：机构须持执业许可并登记所需诊疗科目。"""
        if not self.medical_license_no:
            raise LicenseError(
                f"{self.name} 无医疗机构执业许可，生活美容机构不属于合法注射地点"
            )
        if self.rules.required_subject not in self.subjects:
            raise LicenseError(
                f"{self.name} 未登记诊疗科目 {self.rules.required_subject}"
            )


@dataclass(frozen=True)
class Practitioner:
    """医生或护士的执业资质，含有效期。"""

    practitioner_id: str
    name: str
    role: Role
    license_no: str
    valid_until: date

    def check_valid(self, on: date) -> None:
        """R2：治疗当日资质必须在有效期内。"""
        if on > self.valid_until:
            raise LicenseError(
                f"{self.name} 的执业资质已于 {self.valid_until} 到期"
            )


# ---------------------------------------------------------------- 材料与批次


@dataclass(frozen=True)
class Material:
    """材料品种：每项成分单独登记批准文号，缺失即为来源不明。"""

    material_id: str
    name: str
    approval_no: str | None
    ingredients: tuple[str, ...]

    def check_traceable(self) -> None:
        """R3：来源不明的材料硬性阻断。"""
        if not self.approval_no:
            raise UnknownMaterialError(
                f"材料 {self.name} 缺少批准文号，来源不明，禁止使用"
            )


@dataclass(frozen=True)
class TemperatureRecord:
    """入库温控记录。"""

    recorded_at: datetime
    celsius: float


@dataclass(frozen=True)
class Batch:
    """批次：储运温控落在批次上。"""

    batch_id: str
    material_id: str
    lot_no: str
    expiry: date
    temperature_log: tuple[TemperatureRecord, ...] = ()


@dataclass
class Vial:
    """单支材料：启封时间、用量与余量都落在批次上。

    deductions 记录 treatment_id → 本次用量，同一治疗重复扫描同一支
    是幂等操作，库存只扣减一次。
    """

    vial_id: str
    batch_id: str
    material_id: str
    volume_total_ml: float
    volume_remaining_ml: float
    opened_at: datetime | None = None
    deductions: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------- 确认与记录


@dataclass(frozen=True)
class ItemizedConsent:
    """顾客对每一项成分的单独确认；只确认"水光+"套餐不算确认。"""

    customer_id: str
    material_ids: tuple[str, ...]
    confirmed_at: datetime


@dataclass(frozen=True)
class VialUsage:
    """一次治疗中某支材料的实际用量，落到批次。"""

    vial_id: str
    batch_id: str
    lot_no: str
    material_id: str
    material_name: str
    volume_ml: float


@dataclass(frozen=True)
class TreatmentRecord:
    """治疗记录：列清本次治疗采用的规则与全部批次，按规定留存。"""

    treatment_id: str
    institution_id: str
    customer_id: str
    doctor_id: str
    nurse_id: str
    skin_state: str
    adaptability: str
    usages: tuple[VialUsage, ...]
    rules_applied: tuple[str, ...]
    batches: tuple[str, ...]
    performed_at: datetime


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    phone: str
    allergies: tuple[str, ...] = ()
    marketing_consent: bool = True


# ---------------------------------------------------------------- 随访


@dataclass(frozen=True)
class FollowUpWindow:
    """术后风险时间窗（按治疗后天数）。"""

    name: str
    start_day: int
    end_day: int


FOLLOW_UP_WINDOWS = (
    FollowUpWindow("短期反应窗", 0, 3),
    FollowUpWindow("感染风险窗", 4, 30),
    FollowUpWindow("迟发异常窗", 31, 720),
)

URGENT_SYMPTOMS = frozenset(
    {"高热", "化脓", "剧烈疼痛", "皮肤发白", "视力异常", "呼吸困难"}
)
PROMPT_SYMPTOMS = frozenset({"红肿", "疼痛", "硬结", "结节", "瘙痒", "肿胀"})

GRADE_URGENT = "紧急"
GRADE_PROMPT = "尽快"
GRADE_ROUTINE = "常规"


@dataclass(frozen=True)
class SymptomReport:
    treatment_id: str
    customer_id: str
    symptoms: tuple[str, ...]
    window_name: str
    reported_at: datetime


@dataclass(frozen=True)
class FollowUpReminder:
    """分级复诊提醒：只提示复诊，不构成、也不替代医生诊断。"""

    treatment_id: str
    customer_id: str
    window_name: str
    grade: str
    due_in_days: int
    message: str
    created_at: datetime


# ---------------------------------------------------------------- 服务


class TraceabilityService:
    """连锁医美注射追溯服务：登记、治疗、随访、召回与授权管理。"""

    def __init__(self) -> None:
        self.institutions: dict[str, Institution] = {}
        self.practitioners: dict[str, Practitioner] = {}
        self.materials: dict[str, Material] = {}
        self.batches: dict[str, Batch] = {}
        self.vials: dict[str, Vial] = {}
        self.customers: dict[str, Customer] = {}
        self.treatments: dict[str, TreatmentRecord] = {}
        self.recalled_batches: dict[str, str] = {}
        self.symptom_reports: list[SymptomReport] = []
        self.reminders: list[FollowUpReminder] = []

    # ---- 登记 ----

    def register_institution(self, institution: Institution) -> None:
        self.institutions[institution.institution_id] = institution

    def register_practitioner(self, practitioner: Practitioner) -> None:
        self.practitioners[practitioner.practitioner_id] = practitioner

    def register_material(self, material: Material) -> None:
        self.materials[material.material_id] = material

    def register_batch(self, batch: Batch) -> None:
        self.batches[batch.batch_id] = batch

    def register_vial(self, vial: Vial) -> None:
        self.vials[vial.vial_id] = vial

    def register_customer(self, customer: Customer) -> None:
        self.customers[customer.customer_id] = customer

    # ---- 治疗 ----

    def conduct_treatment(
        self,
        treatment_id: str,
        institution_id: str,
        customer_id: str,
        doctor_id: str,
        nurse_id: str,
        scans: Iterable[tuple[str, float]],
        consent: ItemizedConsent,
        skin_state: str,
        adaptability: str,
        at: datetime,
    ) -> TreatmentRecord:
        """完成一次注射治疗：先通过全部规则核验，再一次性扣减库存。

        scans 是扫码事件序列（vial_id, 用量ml），允许同一支材料被重复
        扫描——相同用量的重复扫描是幂等的，库存只扣减一次。任一规则
        不满足都会硬性阻断，且不会留下部分扣减的库存状态。
        """
        if treatment_id in self.treatments:
            raise TraceabilityError(
                f"治疗编号 {treatment_id} 已存在，医疗记录不可覆盖"
            )
        institution = self._institution(institution_id)
        doctor = self._practitioner(doctor_id)
        nurse = self._practitioner(nurse_id)
        self._customer(customer_id)

        # R1 机构诊疗科目
        institution.check_injectable()

        # R2 人员资质有效期
        if doctor.role is not Role.DOCTOR:
            raise LicenseError(f"{doctor.name} 不具备医生资质，不能作为主诊")
        if nurse.role is not Role.NURSE:
            raise LicenseError(f"{nurse.name} 不具备护士资质")
        doctor.check_valid(at.date())
        nurse.check_valid(at.date())

        # 归并扫码事件：同一支材料重复扫描只计一次
        unique_scans: dict[str, float] = {}
        for vial_id, volume_ml in scans:
            if vial_id in unique_scans:
                if unique_scans[vial_id] != volume_ml:
                    raise InventoryError(
                        f"支剂 {vial_id} 两次扫描用量不一致，请核对"
                    )
                continue
            unique_scans[vial_id] = volume_ml
        if not unique_scans:
            raise InventoryError("治疗必须至少使用一支材料")

        vials = {vid: self._vial(vid) for vid in unique_scans}
        materials = {
            vial.material_id: self._material(vial.material_id)
            for vial in vials.values()
        }

        # R3 材料批准文号
        for material in materials.values():
            material.check_traceable()

        # R4 机构组合规则
        volume_by_material: dict[str, float] = {}
        for vid, volume in unique_scans.items():
            mid = vials[vid].material_id
            volume_by_material[mid] = volume_by_material.get(mid, 0.0) + volume
        institution.rules.check_combination(materials.values(), volume_by_material)

        # R5 批次状态
        for vid, vial in vials.items():
            batch = self._batch(vial.batch_id)
            if batch.batch_id in self.recalled_batches:
                raise RecalledBatchError(
                    f"批次 {batch.lot_no} 已召回："
                    f"{self.recalled_batches[batch.batch_id]}"
                )
            if batch.expiry < at.date():
                raise BatchError(f"批次 {batch.lot_no} 已于 {batch.expiry} 过期")
            if vial.deductions and treatment_id not in vial.deductions:
                raise InventoryError(
                    f"支剂 {vid} 已在其他治疗中启封使用，不得跨治疗复用"
                )
            if unique_scans[vid] > vial.volume_remaining_ml:
                raise InventoryError(f"支剂 {vid} 余量不足")

        # R6 逐项成分确认
        if consent.customer_id != customer_id:
            raise ConsentError("成分确认人与治疗顾客不一致")
        if consent.confirmed_at > at:
            raise ConsentError("成分确认必须早于治疗开始")
        missing = [
            m.name for mid, m in materials.items() if mid not in consent.material_ids
        ]
        if missing:
            raise ConsentError(
                f"顾客未逐项确认成分：{'、'.join(missing)}"
                f"（只确认套餐不能代替逐项确认）"
            )

        # R7 皮肤状态与适应性判断
        if not skin_state.strip() or not adaptability.strip():
            raise AssessmentError("必须记录皮肤状态与适应性判断")

        # 全部规则通过后才扣减库存
        usages = []
        for vid in sorted(vials):
            vial = vials[vid]
            volume = unique_scans[vid]
            if vial.deductions.get(treatment_id) == volume:
                continue  # 幂等：同一治疗重复提交不重复扣减
            batch = self._batch(vial.batch_id)
            material = materials[vial.material_id]
            vial.opened_at = at
            vial.volume_remaining_ml -= volume
            vial.deductions[treatment_id] = volume
            usages.append(
                VialUsage(
                    vial_id=vid,
                    batch_id=batch.batch_id,
                    lot_no=batch.lot_no,
                    material_id=material.material_id,
                    material_name=material.name,
                    volume_ml=volume,
                )
            )

        record = TreatmentRecord(
            treatment_id=treatment_id,
            institution_id=institution_id,
            customer_id=customer_id,
            doctor_id=doctor_id,
            nurse_id=nurse_id,
            skin_state=skin_state,
            adaptability=adaptability,
            usages=tuple(usages),
            rules_applied=ALL_RULES,
            batches=tuple(sorted({u.lot_no for u in usages})),
            performed_at=at,
        )
        self.treatments[treatment_id] = record
        return record

    # ---- 随访 ----

    def report_symptoms(
        self,
        treatment_id: str,
        symptoms: Iterable[str],
        reported_at: datetime,
    ) -> FollowUpReminder:
        """按风险时间窗记录症状并生成分级复诊提醒，不下诊断。"""
        record = self._treatment(treatment_id)
        symptoms = tuple(symptoms)
        if not symptoms:
            raise TraceabilityError("症状报告至少包含一项症状")
        days = (reported_at.date() - record.performed_at.date()).days
        window = next(
            (w for w in FOLLOW_UP_WINDOWS if w.start_day <= days <= w.end_day),
            None,
        )
        window_name = window.name if window else "随访期外"
        if URGENT_SYMPTOMS & set(symptoms):
            grade, due_in_days = GRADE_URGENT, 1
        elif PROMPT_SYMPTOMS & set(symptoms):
            grade, due_in_days = GRADE_PROMPT, 3
        else:
            grade, due_in_days = GRADE_ROUTINE, 14
        message = (
            f"【{grade}】{window_name}内收到症状报告：{'、'.join(symptoms)}。"
            f"请安排顾客于 {due_in_days} 日内携治疗记录复诊，"
            f"由接诊医生面诊评估。"
        )
        report = SymptomReport(
            treatment_id=record.treatment_id,
            customer_id=record.customer_id,
            symptoms=symptoms,
            window_name=window_name,
            reported_at=reported_at,
        )
        reminder = FollowUpReminder(
            treatment_id=record.treatment_id,
            customer_id=record.customer_id,
            window_name=window_name,
            grade=grade,
            due_in_days=due_in_days,
            message=message,
            created_at=reported_at,
        )
        self.symptom_reports.append(report)
        self.reminders.append(reminder)
        return reminder

    # ---- 召回、授权与跨门店病史 ----

    def recall_batch(self, batch_id: str, reason: str) -> None:
        """召回批次：阻断后续使用，历史治疗记录保持不变。"""
        self._batch(batch_id)
        self.recalled_batches[batch_id] = reason

    def treatments_using_batch(self, batch_id: str) -> list[TreatmentRecord]:
        """召回追查：列出使用过该批次的历史治疗，用于通知，不做修改。"""
        batch = self._batch(batch_id)
        return [
            record
            for record in self.treatments.values()
            if any(u.batch_id == batch.batch_id for u in record.usages)
        ]

    def withdraw_marketing_consent(self, customer_id: str) -> None:
        """撤回营销授权：仅停止营销触达，医疗记录按规定继续留存。"""
        customer = self._customer(customer_id)
        self.customers[customer_id] = replace(customer, marketing_consent=False)

    def marketing_audience(self) -> list[str]:
        """当前可触达的营销名单。"""
        return sorted(
            c.customer_id for c in self.customers.values() if c.marketing_consent
        )

    def cross_store_history(self, customer_id: str) -> dict:
        """跨门店复诊时提供给接诊者的最小必要病史。

        只含诊疗所需信息（过敏史、既往材料与批次、适应性判断、症状
        报告），不包含联系方式、营销授权等与本次诊疗无关的字段。
        """
        customer = self._customer(customer_id)
        treatments = sorted(
            (t for t in self.treatments.values() if t.customer_id == customer_id),
            key=lambda t: t.performed_at,
        )
        return {
            "customer_id": customer.customer_id,
            "name": customer.name,
            "allergies": list(customer.allergies),
            "treatments": [
                {
                    "treatment_id": t.treatment_id,
                    "institution": self._institution(t.institution_id).name,
                    "performed_at": t.performed_at.isoformat(),
                    "skin_state": t.skin_state,
                    "adaptability": t.adaptability,
                    "materials": [u.material_name for u in t.usages],
                    "batches": list(t.batches),
                }
                for t in treatments
            ],
            "symptom_reports": [
                {
                    "reported_at": r.reported_at.isoformat(),
                    "window": r.window_name,
                    "symptoms": list(r.symptoms),
                }
                for r in self.symptom_reports
                if r.customer_id == customer_id
            ],
        }

    # ---- 登记查询 ----

    def _institution(self, institution_id: str) -> Institution:
        try:
            return self.institutions[institution_id]
        except KeyError:
            raise TraceabilityError(f"未登记的机构 {institution_id}") from None

    def _practitioner(self, practitioner_id: str) -> Practitioner:
        try:
            return self.practitioners[practitioner_id]
        except KeyError:
            raise TraceabilityError(f"未登记的人员 {practitioner_id}") from None

    def _material(self, material_id: str) -> Material:
        try:
            return self.materials[material_id]
        except KeyError:
            raise TraceabilityError(f"未登记的材料 {material_id}") from None

    def _batch(self, batch_id: str) -> Batch:
        try:
            return self.batches[batch_id]
        except KeyError:
            raise TraceabilityError(f"未登记的批次 {batch_id}") from None

    def _vial(self, vial_id: str) -> Vial:
        try:
            return self.vials[vial_id]
        except KeyError:
            raise TraceabilityError(f"未登记的支剂 {vial_id}") from None

    def _customer(self, customer_id: str) -> Customer:
        try:
            return self.customers[customer_id]
        except KeyError:
            raise TraceabilityError(f"未登记的顾客 {customer_id}") from None

    def _treatment(self, treatment_id: str) -> TreatmentRecord:
        try:
            return self.treatments[treatment_id]
        except KeyError:
            raise TraceabilityError(f"未登记的治疗 {treatment_id}") from None
