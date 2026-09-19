"""上线前验收用的样例数据：连锁门店、人员资质、材料批次与顾客。

包含合法样例与应被阻断的样例：生活美容机构、过期资质、过期批次、
来源不明粉剂。所有日期相对 2026-09-19 设定。
"""

from datetime import date, datetime

from domain import (
    Batch,
    Customer,
    Institution,
    InstitutionRules,
    Material,
    Practitioner,
    Role,
    TemperatureRecord,
    TraceabilityService,
    Vial,
)

CHAIN_RULES = InstitutionRules(
    required_subject="美容皮肤科",
    max_materials_per_session=3,
    forbidden_combinations=(("透明质酸", "胶原蛋白"),),  # 同类填充剂不得同次叠加
    max_volume_ml_per_material=5.0,
)


def _temp_log(*entries: tuple[int, float]) -> tuple[TemperatureRecord, ...]:
    return tuple(
        TemperatureRecord(datetime(2026, 8, day, 8, 30), celsius)
        for day, celsius in entries
    )


def build_sample_service() -> TraceabilityService:
    svc = TraceabilityService()

    # 机构：连锁两家医疗美容门诊部 + 一家生活美容馆（非法注射点样例）
    svc.register_institution(
        Institution(
            "STORE-A",
            "连锁医美·城东门诊部",
            "PDY2026-0001",
            frozenset({"美容皮肤科", "美容外科"}),
            CHAIN_RULES,
        )
    )
    svc.register_institution(
        Institution(
            "STORE-B",
            "连锁医美·城西门诊部",
            "PDY2026-0002",
            frozenset({"美容皮肤科"}),
            CHAIN_RULES,
        )
    )
    svc.register_institution(
        Institution("SALON-01", "美丽人生生活美容馆", None, frozenset(), CHAIN_RULES)
    )

    # 人员：有效医生、有效护士、已过期护士（资质失效样例）
    svc.register_practitioner(
        Practitioner("DOC-1", "林医生", Role.DOCTOR, "ZY2026-110", date(2027, 12, 31))
    )
    svc.register_practitioner(
        Practitioner("NUR-1", "陈护士", Role.NURSE, "HS2026-220", date(2027, 6, 30))
    )
    svc.register_practitioner(
        Practitioner("NUR-2", "王护士", Role.NURSE, "HS2026-999", date(2024, 1, 1))
    )

    # 材料：四项有批准文号，一项来源不明
    svc.register_material(
        Material("MAT-HA", "透明质酸凝胶", "国械注准20233130123", ("透明质酸",))
    )
    svc.register_material(
        Material("MAT-BTX", "A型肉毒毒素", "国药准字S10970037", ("A型肉毒毒素",))
    )
    svc.register_material(
        Material("MAT-COL", "胶原蛋白填充剂", "国械注准20223130456", ("胶原蛋白",))
    )
    svc.register_material(
        Material("MAT-PLLA", "聚左旋乳酸微球", "国械注准20243130789", ("聚左旋乳酸",))
    )
    svc.register_material(Material("MAT-XXX", "来源不明粉剂", None, ("未知粉末",)))

    # 批次：含入库温控记录；BATCH-EXP-01 为过期批次样例
    batches = [
        Batch("BATCH-HA-01", "MAT-HA", "HA260101", date(2027, 1, 31),
              _temp_log((1, 4.6), (2, 5.1))),
        Batch("BATCH-HA-02", "MAT-HA", "HA260202", date(2028, 6, 30),
              _temp_log((3, 4.9))),
        Batch("BATCH-BTX-01", "MAT-BTX", "BTX260101", date(2027, 6, 30),
              _temp_log((1, 5.4), (2, 5.0))),
        Batch("BATCH-COL-01", "MAT-COL", "COL260101", date(2027, 3, 31),
              _temp_log((4, 4.2))),
        Batch("BATCH-PLLA-01", "MAT-PLLA", "PLLA260101", date(2027, 5, 31),
              _temp_log((4, 6.1))),
        Batch("BATCH-XXX-01", "MAT-XXX", "UNKNOWN-01", date(2027, 1, 31)),
        Batch("BATCH-EXP-01", "MAT-HA", "HA250101", date(2025, 1, 1),
              _temp_log((1, 4.8))),
    ]
    for batch in batches:
        svc.register_batch(batch)

    # 支剂
    vials = [
        Vial("VIAL-HA-1", "BATCH-HA-01", "MAT-HA", 2.0, 2.0),
        Vial("VIAL-HA-2", "BATCH-HA-02", "MAT-HA", 2.0, 2.0),
        Vial("VIAL-BTX-1", "BATCH-BTX-01", "MAT-BTX", 1.0, 1.0),
        Vial("VIAL-BTX-2", "BATCH-BTX-01", "MAT-BTX", 1.0, 1.0),
        Vial("VIAL-COL-1", "BATCH-COL-01", "MAT-COL", 1.0, 1.0),
        Vial("VIAL-PLLA-1", "BATCH-PLLA-01", "MAT-PLLA", 1.0, 1.0),
        Vial("VIAL-XXX-1", "BATCH-XXX-01", "MAT-XXX", 1.0, 1.0),
        Vial("VIAL-EXP-1", "BATCH-EXP-01", "MAT-HA", 1.0, 1.0),
    ]
    for vial in vials:
        svc.register_vial(vial)

    # 顾客
    svc.register_customer(
        Customer(
            "CUST-1",
            "周某",
            "13800001111",
            allergies=("青霉素",),
            marketing_consent=True,
        )
    )
    return svc
