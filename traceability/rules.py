"""放行规则册（版本化）。

治疗记账完成时执行全量评估：任何一条规则不通过都构成硬性阻断，治疗不得
归档，库存移动保持其当时状态（核销动作本身按支料全局幂等，不会重复扣减）。
规则编号随最终病历快照留存，做到“该次治疗采用的规则”可审计。
"""

from dataclasses import dataclass

from .errors import Blocked
from .models import (
    MAX_DISTINCT_INGREDIENTS,
    OPENED_TTL,
    Batch,
    Decision,
    Product,
    Site,
    Staff,
    Treatment,
    Unit,
    UnitStatus,
)

RULE_BOOK_VERSION = "RB-2026.1"

# 浮点余量平衡容差（毫升）
DOSE_EPSILON = 1e-6


@dataclass
class RuleContext:
    """规则评估快照：所有仓储数据在 finalize 时一次性冻结。"""

    now: object
    site: Site
    doctor: Staff
    nurse: Staff
    treatment: Treatment
    products: dict[str, Product]
    batches: dict[str, Batch]
    units: dict[str, Unit]


def _violations_site(ctx: RuleContext) -> list[Blocked]:
    site = ctx.site
    out = []
    if not site.is_medical:
        out.append(Blocked(
            "R101", "合法医疗机构",
            f"{site.name} 未取得《医疗机构执业许可证》，生活美容场所不得开展注射操作",
        ))
    if not site.medical_subject_registered:
        out.append(Blocked(
            "R102", "诊疗科目登记",
            f"{site.name} 未登记“{site.subject_detail or '医疗美容科'}”诊疗科目，不得开展对应注射项目",
        ))
    return out


def _staff_checks(person: Staff, id_code: str, scope_label: str, ctx: RuleContext) -> list[Blocked]:
    out = []
    if person.license_expiry < ctx.now:
        out.append(Blocked(
            id_code, f"{scope_label}执业资质有效期",
            f"{person.name}（{person.license_no}）执业资质已于 {person.license_expiry:%Y-%m-%d} 到期",
        ))
    if "医疗美容" not in person.practice_scopes:
        out.append(Blocked(
            id_code, f"{scope_label}执业范围",
            f"{person.name} 执业范围不含医疗美容：{', '.join(person.practice_scopes) or '未登记'}",
        ))
    if ctx.site.id not in person.filed_site_ids:
        out.append(Blocked(
            id_code, f"{scope_label}执业地点备案",
            f"{person.name} 未在 {ctx.site.name} 完成执业注册/多机构备案",
        ))
    return out


def _violations_staff(ctx: RuleContext) -> list[Blocked]:
    doctor_blocks = _staff_checks(ctx.doctor, "R201", "医师", ctx)
    nurse_blocks = _staff_checks(ctx.nurse, "R211", "护士", ctx)
    # 医师资质与护士资质各自独立上报，不互相吞并
    return doctor_blocks + nurse_blocks


def _violations_assessment(ctx: RuleContext) -> list[Blocked]:
    assessment = ctx.treatment.assessment
    if assessment is None:
        return [Blocked(
            "R301", "治疗前适应性判断",
            "缺少医师基于皮肤状态的适应性判断记录，不得开始治疗",
        )]
    if assessment.doctor_id != ctx.doctor.id:
        return [Blocked(
            "R301", "治疗前适应性判断",
            "皮肤评估必须由本次治疗的接诊医师本人完成",
        )]
    if assessment.decision == Decision.UNFIT:
        return [Blocked(
            "R302", "不适应结论",
            f"医师已判定不适应（{assessment.notes}），本次治疗不得执行",
        )]
    return []


def _product_block(product: Product | None, uid: str, batch_no: str) -> Blocked | None:
    if product is None or not product.approval_no or not product.registered:
        label = product.generic_name if product else "无法识别"
        approval = product.approval_no if product else ""
        return Blocked(
            "R401", "注册批准文号与来源核验",
            f"支料 {uid}（{label}，批号 {batch_no or '未知'}）无有效医疗器械注册证/药品批准文号，"
            f"来源不明，硬性阻断",
        )
    return None


def _violations_materials(ctx: RuleContext) -> list[Blocked]:
    out = []
    treatment = ctx.treatment
    referenced = {(u.product_id, u.batch_no, u.uid): u for u in treatment.usages}
    for (product_id, batch_no, uid), usage in referenced.items():
        product = ctx.products.get(product_id)
        block = _product_block(product, uid, batch_no)
        if block:
            out.append(block)
            continue  # 来源不明时后续温控/召回判断无意义，跳过该支
        batch = ctx.batches.get(batch_no)
        if batch is None or batch.product_id != product_id:
            out.append(Blocked(
                "R402", "批次归属", f"支料 {uid} 的批号 {batch_no} 无入库记录",
            ))
            continue
        if batch.expiry < ctx.now:
            out.append(Blocked(
                "R403", "批次效期",
                f"{product.generic_name} 批号 {batch_no} 已于 {batch.expiry:%Y-%m-%d} 过期",
            ))
        if not batch.cold_chain_ok():
            bad = [f"{r.at:%Y-%m-%d %H:%M}={r.temp_c}℃" for r in batch.readings
                   if not (batch.storage_min_c <= r.temp_c <= batch.storage_max_c)]
            out.append(Blocked(
                "R404", "全程温控",
                f"{product.generic_name} 批号 {batch_no} 储运温度超出 {batch.storage_min_c}-{batch.storage_max_c}℃："
                f"{'; '.join(bad) or '无温控读数'}",
            ))
        if batch.quarantine:
            out.append(Blocked(
                "R405", "批次检疫冻结",
                f"{product.generic_name} 批号 {batch_no} 处于检疫冻结状态，不得领用",
            ))
        if batch.recalled is not None:
            out.append(Blocked(
                "R406", "批次召回",
                f"{product.generic_name} 批号 {batch_no} 已被召回（{batch.recalled.reason}，"
                f"{batch.recalled.at:%Y-%m-%d}）",
            ))
        unit = ctx.units.get(uid)
        if unit is None:
            out.append(Blocked("R501", "实物支料", f"条码 {uid} 无任何出入库记录"))
            continue
        if unit.site_id != treatment.site_id:
            out.append(Blocked(
                "R501", "支料库存归属",
                f"支料 {uid} 归属其他门店库存，不得跨店领用",
            ))
        if unit.status == UnitStatus.CONSUMED:
            out.append(Blocked(
                "R505", "核销幂等",
                f"支料 {uid} 已核销，重复扫描零扣减并阻断",
            ))
        elif unit.status == UnitStatus.DISCARDED:
            out.append(Blocked(
                "R501", "实物支料",
                f"支料 {uid} 已按医疗废物弃置，不得再次用于治疗",
            ))
        elif unit.status != UnitStatus.OPENED or unit.opened_in_treatment != treatment.id:
            owner = unit.opened_in_treatment or "无"
            out.append(Blocked(
                "R502" if owner != "无" else "R501", "启封与核销登记",
                f"支料 {uid} 未在本次治疗完成启封登记（当前状态 {unit.status.value}，"
                f"绑定治疗 {owner}），跨治疗复用与跨店领用一律阻断",
            ))
        if unit.opened_at is not None and ctx.now - unit.opened_at > OPENED_TTL:
            out.append(Blocked(
                "R503", "启封后使用时限",
                f"支料 {uid} 启封于 {unit.opened_at:%H:%M}，超过 {int(OPENED_TTL.total_seconds() // 3600)} 小时时限",
            ))
        if abs(usage.dose_ml + usage.remainder_ml - unit.fill_ml) > DOSE_EPSILON:
            out.append(Blocked(
                "R504", "用量余量平衡",
                f"支料 {uid} 充填 {unit.fill_ml}ml，用量 {usage.dose_ml}ml + 余量 "
                f"{usage.remainder_ml}ml 不平账",
            ))
        if usage.remainder_ml > 0 and not usage.remainder_disposition:
            out.append(Blocked(
                "R504", "余量去向",
                f"支料 {uid} 余 {usage.remainder_ml}ml 未记录去向（须当场按医疗废物处置）",
            ))
    return out


def _violations_combination(ctx: RuleContext) -> list[Blocked]:
    out = []
    product_doses: dict[str, float] = {}
    for usage in ctx.treatment.usages:
        product_doses[usage.product_id] = product_doses.get(usage.product_id, 0.0) + usage.dose_ml
    if len(product_doses) > MAX_DISTINCT_INGREDIENTS:
        names = [ctx.products.get(pid).generic_name if ctx.products.get(pid) else pid
                 for pid in product_doses]
        out.append(Blocked(
            "R601", "成分组合上限",
            f"本次计划 {len(product_doses)} 种成分（{', '.join(names)}），"
            f"超过机构规则上限 {MAX_DISTINCT_INGREDIENTS} 种",
        ))
    for product_id, total in product_doses.items():
        product = ctx.products.get(product_id)
        if product is not None and product.max_dose_ml is not None and total > product.max_dose_ml + DOSE_EPSILON:
            out.append(Blocked(
                "R602", "单成分最大剂量",
                f"{product.generic_name} 本次合计 {total}ml，超过单次最大剂量 {product.max_dose_ml}ml",
            ))
    return out


def _violations_consent(ctx: RuleContext) -> list[Blocked]:
    out = []
    if not ctx.treatment.usages:
        out.append(Blocked(
            "R702", "治疗记账完整",
            "本次治疗没有任何支料用量与余量记录，不得归档为空病历；"
            "套餐名登记不构成治疗记账",
        ))
    consent_keys = {(c.product_id, c.batch_no, c.unit_uid) for c in ctx.treatment.consents}
    for usage in ctx.treatment.usages:
        key = (usage.product_id, usage.batch_no, usage.uid)
        if key not in consent_keys:
            product = ctx.products.get(usage.product_id)
            name = product.generic_name if product else usage.product_id
            out.append(Blocked(
                "R701", "逐项成分知情同意",
                f"{name} / 批号 {usage.batch_no} / 支料 {usage.uid} 未经顾客逐项确认，"
                "禁止以“水光+”等套餐名替代成分确认",
            ))
    # 反向：确认过但最终未使用的支料不阻断归档，但属于待解释异常，由审计字段提示
    return out


# 评估顺序即病历中的规则排列顺序
RULE_CHECKS = (
    _violations_site,
    _violations_staff,
    _violations_assessment,
    _violations_materials,
    _violations_combination,
    _violations_consent,
)


def evaluate(ctx: RuleContext) -> list[Blocked]:
    """返回全部命中的阻断；空列表表示放行。"""
    violations: list[Blocked] = []
    for check in RULE_CHECKS:
        violations.extend(check(ctx))
    return violations
