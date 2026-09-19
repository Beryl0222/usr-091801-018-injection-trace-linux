"""治疗病历：归档时冻结的不可变快照。

快照必须列清：
1. 该次治疗采用的规则册版本与全部规则的评估结果（含通过项）；
2. 全部批次：产品、批准文号、批号、用量、余量去向；
3. 治疗机构、医师护士、解剖部位、皮肤评估结论与逐项确认。

病历自治疗完成之日起依法留存 15 年；营销授权撤回、批次召回、人员资质
失效都不删除、不改写病历。
"""

from dataclasses import dataclass
from datetime import timedelta

from .models import MEDICAL_RECORD_RETENTION_YEARS


@dataclass(frozen=True)
class RuleResult:
    code: str
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class BatchLine:
    product_id: str
    generic_name: str
    approval_no: str
    batch_no: str
    unit_uid: str
    dose_ml: float
    remainder_ml: float
    remainder_disposition: str
    anatomical_site: str


@dataclass(frozen=True)
class MedicalRecord:
    treatment_id: str
    customer_id: str
    site_id: str
    site_name: str
    doctor_id: str
    doctor_name: str
    nurse_id: str
    nurse_name: str
    finalized_at: object
    rule_book_version: str
    rule_results: tuple[RuleResult, ...]
    batch_lines: tuple[BatchLine, ...]
    anatomy_sites: tuple[str, ...]
    assessment_summary: str
    retention_until: object
    immutable: bool = True


RULE_TITLES = (
    ("R101", "合法医疗机构"),
    ("R102", "诊疗科目登记"),
    ("R201", "医师执业资质（有效期/执业范围/地点备案）"),
    ("R211", "护士执业资质（有效期/执业范围/地点备案）"),
    ("R301", "治疗前适应性判断"),
    ("R302", "不适应结论"),
    ("R401", "注册批准文号与来源核验"),
    ("R402", "批次归属"),
    ("R403", "批次效期"),
    ("R404", "全程温控"),
    ("R405", "批次检疫冻结"),
    ("R406", "批次召回"),
    ("R501", "实物支料与库存归属"),
    ("R502", "一启一患（启封绑定）"),
    ("R503", "启封后使用时限"),
    ("R504", "用量余量平衡与去向"),
    ("R505", "核销幂等"),
    ("R601", "成分组合上限"),
    ("R602", "单成分最大剂量"),
    ("R701", "逐项成分知情同意"),
    ("R702", "治疗记账完整（用量与余量不得为空）"),
)


class RecordStore:
    def __init__(self):
        self._records: dict[str, MedicalRecord] = {}

    def save(self, record: MedicalRecord) -> None:
        if record.treatment_id in self._records:
            # 不可变：重复 finalize 返回既有快照，绝不覆盖
            return
        self._records[record.treatment_id] = record

    def get(self, treatment_id: str) -> MedicalRecord:
        return self._records[treatment_id]

    def all_for_customer(self, customer_id: str) -> list[MedicalRecord]:
        return [r for r in self._records.values() if r.customer_id == customer_id]

    def __len__(self):
        return len(self._records)


def build_record(ctx, violations) -> MedicalRecord:
    """依据 finalize 时的规则上下文与命中明细构造快照。"""
    failed = {v.code: v for v in violations}
    # 规则册中未在本次触发的规则一律记为通过；触发到的规则记失败并带原因。
    # R301/R302、R201/R211 等存在“合并上报”的规则按命中编码落位。
    results = []
    for code, title in RULE_TITLES:
        if code in failed:
            results.append(RuleResult(code, title, False, failed[code].reason))
        else:
            results.append(RuleResult(code, title, True, ""))

    products = ctx.products
    lines = []
    for usage in sorted(ctx.treatment.usages, key=lambda u: u.uid):
        product = products[usage.product_id]
        lines.append(BatchLine(
            product_id=product.id,
            generic_name=product.generic_name,
            approval_no=product.approval_no,
            batch_no=usage.batch_no,
            unit_uid=usage.uid,
            dose_ml=usage.dose_ml,
            remainder_ml=usage.remainder_ml,
            remainder_disposition=usage.remainder_disposition,
            anatomical_site=usage.anatomical_site,
        ))

    assessment = ctx.treatment.assessment
    summary = (
        f"{assessment.decision.value}；所见：{'、'.join(assessment.findings) or '无'}；"
        f"注意事项：{assessment.notes or '无'}"
        if assessment else "无评估"
    )
    finalized_at = ctx.now
    return MedicalRecord(
        treatment_id=ctx.treatment.id,
        customer_id=ctx.treatment.customer_id,
        site_id=ctx.site.id,
        site_name=ctx.site.name,
        doctor_id=ctx.doctor.id,
        doctor_name=ctx.doctor.name,
        nurse_id=ctx.nurse.id,
        nurse_name=ctx.nurse.name,
        finalized_at=finalized_at,
        rule_book_version=__rule_version(),
        rule_results=tuple(results),
        batch_lines=tuple(lines),
        anatomy_sites=tuple(ctx.treatment.anatomy_sites),
        assessment_summary=summary,
        retention_until=finalized_at + timedelta(days=365 * MEDICAL_RECORD_RETENTION_YEARS),
    )


def __rule_version():
    from .rules import RULE_BOOK_VERSION
    return RULE_BOOK_VERSION
