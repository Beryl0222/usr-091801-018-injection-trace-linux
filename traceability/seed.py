"""验收用样例数据：三家门店、三类人员、五种产品、六个批次、三位顾客。

数据刻意覆盖：合法/生活美容门店、有效/过期医师、正常/召回/来源不明批次，
供场景脚本与单元测试共用。
"""

from .clock import Clock, to_dt
from .models import (
    Batch,
    Customer,
    Product,
    Site,
    Staff,
    TempReading,
    Unit,
    ConsentEvent,
)
from .app import TraceService

# 固定基准时间，整个世界用同一个时钟推进
EPOCH = "2026-01-01T08:00:00+08:00"


def build_service(clock: Clock | None = None) -> tuple[TraceService, dict]:
    clock = clock or Clock(to_dt(EPOCH))
    svc = TraceService(clock)

    # ---------- 门店 ----------
    site_cy = Site(
        id="S01", name="星璨医疗美容·朝阳店",
        is_medical=True, medical_subject_registered=True,
        subject_detail="医疗美容科（美容外科/美容皮肤科）",
        address="北京市朝阳区XX路1号",
    )
    site_hd = Site(
        id="S02", name="星璨医疗美容·海淀店",
        is_medical=True, medical_subject_registered=True,
        subject_detail="医疗美容科（美容皮肤科）",
        address="北京市海淀区YY路2号",
    )
    site_beauty = Site(
        id="S99", name="焕颜生活美容馆",
        is_medical=False, medical_subject_registered=False,
        subject_detail="生活美容（无医疗科目）",
        address="北京市朝阳区ZZ街3号",
    )
    for site in (site_cy, site_hd, site_beauty):
        svc.register_site(site)

    # ---------- 人员 ----------
    doctor_lin = Staff(
        id="D01", name="林知微", role="doctor", license_no="110120240001",
        license_expiry=to_dt("2028-12-31T23:59:59+08:00"),
        practice_scopes=("医疗美容",),
        filed_site_ids=frozenset({"S01", "S02"}),
    )
    # 周慕白：样例开始时资质有效，2026-06-30 到期，演示“资质失效不溯及旧病历”
    doctor_zhou = Staff(
        id="D02", name="周慕白", role="doctor", license_no="110120240002",
        license_expiry=to_dt("2026-06-30T23:59:59+08:00"),
        practice_scopes=("医疗美容",),
        filed_site_ids=frozenset({"S01"}),
    )
    doctor_shen = Staff(
        id="D03", name="沈砚之", role="doctor", license_no="110120250003",
        license_expiry=to_dt("2029-06-30T23:59:59+08:00"),
        practice_scopes=("医疗美容",),
        filed_site_ids=frozenset({"S02"}),
    )
    nurse_xu = Staff(
        id="N01", name="许棠", role="nurse", license_no="20211020260001",
        license_expiry=to_dt("2028-06-30T23:59:59+08:00"),
        practice_scopes=("医疗美容",),
        filed_site_ids=frozenset({"S01"}),
    )
    nurse_gu = Staff(
        id="N02", name="顾清晨", role="nurse", license_no="20211020260002",
        license_expiry=to_dt("2028-06-30T23:59:59+08:00"),
        practice_scopes=("医疗美容",),
        filed_site_ids=frozenset({"S02"}),
    )
    for person in (doctor_lin, doctor_zhou, doctor_shen, nurse_xu, nurse_gu):
        svc.register_staff(person)

    # ---------- 产品（成分必须有通用名与批准文号）----------
    ha = Product(
        id="P-HA", generic_name="注射用透明质酸钠凝胶",
        approval_no="国械注准202431400001", kind="医疗器械",
        registered=True, max_dose_ml=2.0,
    )
    collagen = Product(
        id="P-COL", generic_name="含利多卡因胶原蛋白植入剂",
        approval_no="国械注准202431400003", kind="医疗器械",
        registered=True, max_dose_ml=1.0,
    )
    botox = Product(
        id="P-BTX", generic_name="注射用A型肉毒毒素（复溶后当量）",
        approval_no="国药准字H202400002", kind="药品",
        registered=True, max_dose_ml=4.0,
    )
    unknown_powder = Product(
        id="P-X", generic_name="来源不明韩文粉剂（无中文标识）",
        approval_no="", kind="不明",
        registered=False, max_dose_ml=None,
    )
    for product in (ha, collagen, botox, unknown_powder):
        svc.register_product(product)

    # ---------- 批次与温控 ----------
    def readings(specs, base):
        return [TempReading(at=to_dt(f"{base}T{hh}:00:00+08:00"), temp_c=t)
                for hh, t in specs]

    batch_ha = Batch(
        batch_no="B-HA-2025A", product_id="P-HA",
        expiry=to_dt("2027-06-30T23:59:59+08:00"),
        storage_min_c=2.0, storage_max_c=25.0,
        inbound_at=to_dt("2025-12-20T10:00:00+08:00"),
        readings=readings([("10", 6.2), ("14", 8.1), ("18", 7.4)], "2025-12-20"),
    )
    batch_col = Batch(
        batch_no="B-COL-2025A", product_id="P-COL",
        expiry=to_dt("2027-03-31T23:59:59+08:00"),
        storage_min_c=2.0, storage_max_c=8.0,
        inbound_at=to_dt("2025-12-22T10:00:00+08:00"),
        readings=readings([("10", 4.5), ("14", 5.2), ("18", 4.9)], "2025-12-22"),
    )
    batch_btx = Batch(
        batch_no="B-BTX-2025A", product_id="P-BTX",
        expiry=to_dt("2027-02-28T23:59:59+08:00"),
        storage_min_c=2.0, storage_max_c=8.0,
        inbound_at=to_dt("2025-12-22T10:00:00+08:00"),
        readings=readings([("10", 5.0), ("14", 6.3)], "2025-12-22"),
    )
    # 召回批次：入库时合格，2026-04-01 被厂家召回
    batch_old = Batch(
        batch_no="B-HA-2024Q", product_id="P-HA",
        expiry=to_dt("2026-12-31T23:59:59+08:00"),
        storage_min_c=2.0, storage_max_c=25.0,
        inbound_at=to_dt("2025-10-08T10:00:00+08:00"),
        readings=readings([("10", 7.0)], "2025-10-08"),
    )
    # 来源不明粉剂的“批次”：无注册产品、温控自始缺失
    batch_x = Batch(
        batch_no="B-X-NO-ID", product_id="P-X",
        expiry=to_dt("2027-01-01T00:00:00+08:00"),
        storage_min_c=2.0, storage_max_c=8.0,
        inbound_at=to_dt("2026-01-10T10:00:00+08:00"),
        readings=[],
    )
    for batch in (batch_ha, batch_col, batch_btx, batch_old, batch_x):
        svc.inventory.register_batch(batch)

    # ---------- 支料（一物一码）----------
    units = [
        Unit("U-HA-1", "B-HA-2025A", "S01", 1.0),
        Unit("U-HA-2", "B-HA-2025A", "S01", 1.0),
        Unit("U-HA-3", "B-HA-2025A", "S01", 1.0),
        Unit("U-COL-1", "B-COL-2025A", "S01", 1.0),
        Unit("U-COL-2", "B-COL-2025A", "S01", 1.0),
        Unit("U-BTX-1", "B-BTX-2025A", "S01", 1.0),
        Unit("U-BTX-2", "B-BTX-2025A", "S01", 1.0),
        Unit("U-HA-OLD", "B-HA-2024Q", "S01", 1.0),
        Unit("U-X-1", "B-X-NO-ID", "S01", 2.0),
    ]
    for unit in units:
        svc.inventory.inbound_unit(unit)

    # ---------- 顾客 ----------
    jiang = Customer(id="C001", name="江晚吟")
    # 闻笙：有治疗史，营销授权随后撤回（演示撤回不波及病历）
    wen = Customer(
        id="C002", name="闻笙",
        consent_events=[ConsentEvent(to_dt("2026-01-01T09:00:00+08:00"), True)],
    )
    for customer in (jiang, wen):
        svc.register_customer(customer)

    refs = {
        "sites": {"cy": site_cy, "hd": site_hd, "beauty": site_beauty},
        "doctors": {"lin": doctor_lin, "zhou": doctor_zhou, "shen": doctor_shen},
        "nurses": {"xu": nurse_xu, "gu": nurse_gu},
        "products": {"ha": ha, "col": collagen, "btx": botox, "x": unknown_powder},
        "batches": {"ha": batch_ha, "col": batch_col, "btx": batch_btx,
                    "old": batch_old, "x": batch_x},
        "units": {u.uid: u for u in units},
        "customers": {"jiang": jiang, "wen": wen},
    }
    return svc, refs
