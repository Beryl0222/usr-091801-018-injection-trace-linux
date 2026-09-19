"""上线前验收场景：用固定时钟追查一例跨门店复诊。

时间线（2026 年）：
  01-05 朝阳店 江晚吟 透明质酸注射（T-2026-001），同支重复扫描
  01-08/01-12 短期随访：红肿热痛 L2、无症状 L1
  01-15 朝阳店 闻笙 肉毒毒素注射（T-2026-003）
  01-20 三成分组合超限 → R601 阻断，库存零扣减
  01-25 闻笙 D10 视力红旗 → L3 紧急提醒（不自动诊断）
  02-10 江晚吟 额头胶原（T-2026-002，留余量处置记录）
  03-01 闻笙撤回营销授权（病历不动）
  04-01 厂家召回旧批次；04-05 试图使用召回批次 → R406 阻断
  07-05 周慕白资质已过期 → 新开治疗 R201 阻断，旧病历不变
  08-04 江晚吟 D211 面颊硬结 → L2 迟发结节提醒；跨店到海淀店，
        沈砚之凭有效资质调阅最小必要病史，只见面颊相关 T-2026-001

脚本对每个验收不变量做硬断言，全部通过时以 0 退出。
"""

from .clock import Clock, to_dt
from .errors import Blocked, TreatmentBlocked, Unauthorized, ValidationFailed
from .models import Decision, Recall
from .seed import build_service


class Auditor:
    def __init__(self):
        self.checks = 0

    def expect(self, condition: bool, message: str):
        if not condition:
            raise AssertionError(f"验收失败：{message}")
        self.checks += 1
        print(f"  ✓ {message}")

    def expect_block(self, callable_, code: str, message: str):
        try:
            callable_()
        except (Blocked, TreatmentBlocked) as exc:
            codes = [v.code for v in getattr(exc, "violations", [])] or [getattr(exc, "code", "")]
            self.expect(code in codes, f"{message}（命中 {code}，实际 {codes}）")
            return exc
        raise AssertionError(f"验收失败：{message}——预期阻断 {code} 但放行")


def run_scenario(clock: Clock | None = None) -> dict:
    clock = clock or Clock(to_dt("2026-01-01T08:00:00+08:00"))
    svc, refs = build_service(clock)
    audit = Auditor()
    r = refs
    print("=" * 72)
    print("注射全程追溯 · 跨门店复诊追查验收（时钟驱动，无系统时间依赖）")
    print("=" * 72)

    # ---------------------------------------------------------------
    print("\n[01-05] 朝阳店 T-2026-001：江晚吟·透明质酸·左侧面颊")
    clock.set(to_dt("2026-01-05T09:00:00+08:00"))
    eligibility = svc.eligibility_report("S01", "D01", "N01")
    audit.expect(all(i["passed"] for i in eligibility), "治疗前核验：机构科目+医师+护士全部有效")

    t1 = svc.open_treatment(
        "T-2026-001", "C001", "S01", "D01", "N01",
        anatomy_sites=["左侧面颊"], planned_product_ids=["P-HA"],
    )
    svc.note_marketing_package("T-2026-001", "水光+焕活套餐")
    svc.record_assessment(
        "T-2026-001", Decision.FIT_WITH_CAUTION,
        findings=["皮肤完整无破溃", "左侧面颊轻度干燥"],
        contraindications_checked=["妊娠", "活动性感染", "免疫治疗史", "异体材料史"],
        notes="皮肤屏障偏弱，术后48小时避免高温环境",
    )
    # 顾客逐项确认：确认的是成分+文号+批次+支料，而不是“水光+”套餐
    consent = svc.add_consent_line("T-2026-001", "U-HA-1")
    audit.expect(consent.approval_no == "国械注准202431400001"
                 and consent.batch_no == "B-HA-2025A" and consent.unit_uid == "U-HA-1",
                 "逐项确认精确到批准文号/批号/支料条码（非套餐名）")

    scan1 = svc.scan_unit("T-2026-001", "U-HA-1")
    scan2 = svc.scan_unit("T-2026-001", "U-HA-1")   # 期间重复扫描同一支材料
    audit.expect(not scan1.repeated and scan2.repeated, "同一支材料重复扫描：幂等返回，零状态变更")

    svc.record_usage("T-2026-001", "U-HA-1", "左侧面颊", dose_ml=1.0)
    rec1 = svc.finalize("T-2026-001")

    audit.expect(svc.inventory.deduction_count("U-HA-1") == 1, "库存只扣减一次（U-HA-1 移动数=1）")
    audit.expect(r["units"]["U-HA-1"].status.value == "CONSUMED", "支料状态已核销")
    audit.expect(rec1.rule_book_version == "RB-2026.1", "病历留存该次治疗采用的规则册版本")
    audit.expect(
        {x.code for x in rec1.rule_results} == {
            "R101", "R102", "R201", "R211", "R301", "R302", "R401", "R402",
            "R403", "R404", "R405", "R406", "R501", "R502", "R503", "R504",
            "R505", "R601", "R602", "R701", "R702",
        },
        "最终记录列清全部 21 条规则的评估结果",
    )
    audit.expect(all(x.passed for x in rec1.rule_results), "T-2026-001 全部规则通过")
    line1 = rec1.batch_lines[0]
    audit.expect(
        (line1.unit_uid, line1.batch_no, line1.approval_no, line1.dose_ml, line1.remainder_ml)
        == ("U-HA-1", "B-HA-2025A", "国械注准202431400001", 1.0, 0.0),
        "最终记录列清全部批次：支料/批号/批准文号/用量/余量",
    )
    audit.expect(line1.remainder_disposition == "无余量（整支用完）", "余量去向落账")
    audit.expect(rec1.retention_until.year == 2041, "病历留存至 2041 年（治疗日起 15 年）")

    # 归档后重复 finalize：病历不可变；跨治疗再扫描已核销支料：阻断零扣减
    try:
        svc.finalize("T-2026-001")
        raise AssertionError("重复归档应被拒绝")
    except ValidationFailed:
        audit.expect(True, "已归档病历不可变：重复 finalize 被拒绝")
    reuse = svc.open_treatment("T-REUSE-HA1", "C001", "S01", "D01", "N01", ["左侧面颊"])
    svc.record_assessment("T-REUSE-HA1", Decision.FIT, ["无异常"], ["无禁忌"], "")
    audit.expect_block(lambda: svc.scan_unit("T-REUSE-HA1", "U-HA-1"), "R505",
                       "另一治疗再次扫描已核销支料：阻断且零扣减")
    audit.expect(svc.inventory.deduction_count("U-HA-1") == 1, "反复操作后扣减次数仍为 1")

    # ---------------------------------------------------------------
    print("\n[阻断演示 A] 生活美容馆接诊 → R101")
    audit.expect_block(
        lambda: svc.open_treatment("T-BAD-SITE", "C001", "S99", "D01", "N01", ["面部"]),
        "R101", "生活美容场所不得开展注射",
    )

    # ---------------------------------------------------------------
    print("\n[阻断演示 B] 来源不明粉剂 → R401（在逐项确认环节即阻断）")
    clock.set(to_dt("2026-01-06T10:00:00+08:00"))
    tbad = svc.open_treatment("T-BAD-X", "C001", "S01", "D01", "N01", ["面部"])
    svc.record_assessment("T-BAD-X", Decision.FIT, ["无异常"], ["无禁忌"], "")
    audit.expect_block(lambda: svc.add_consent_line("T-BAD-X", "U-X-1"), "R401",
                       "无中文标识/无批准文号粉剂不得进入确认与使用")
    audit.expect(svc.inventory.deduction_count("U-X-1") == 0, "来源不明材料库存零扣减")
    audit.expect(r["units"]["U-X-1"].status.value == "IN_STOCK", "来源不明材料未被启封")

    # ---------------------------------------------------------------
    print("\n[01-08/01-12] 术后随访：D3 红肿热痛 → L2；D7 无症状 → L1")
    clock.set(to_dt("2026-01-08T10:00:00+08:00"))
    fu_d3 = svc.collect_follow_up("F-001", "T-2026-001",
                                  ["红肿热痛持续加重"], "左侧面颊")
    audit.expect(fu_d3.window_code == "EARLY" and fu_d3.level == 2
                 and fu_d3.elapsed_days == 3,
                 "D3 落入短期反应窗，红肿热痛信号分级 L2（尽快复诊）")
    audit.expect("诊断" not in fu_d3.reminder, "提醒文案只给复诊建议，不下诊断")
    clock.set(to_dt("2026-01-12T10:00:00+08:00"))
    fu_d7 = svc.collect_follow_up("F-002", "T-2026-001", ["无症状"], "左侧面颊")
    audit.expect(fu_d7.level == 1, "D7 无症状：L1 常规随访")

    # ---------------------------------------------------------------
    print("\n[01-15] 朝阳店 T-2026-003：闻笙·A型肉毒毒素·眉间")
    clock.set(to_dt("2026-01-15T14:00:00+08:00"))
    t3 = svc.open_treatment("T-2026-003", "C002", "S01", "D01", "N01", ["眉间"])
    svc.record_assessment("T-2026-003", Decision.FIT, ["眉间动态纹明显"],
                          ["重症肌无力", "妊娠", "氨基糖苷类用药史"], "")
    svc.add_consent_line("T-2026-003", "U-BTX-2")
    svc.scan_unit("T-2026-003", "U-BTX-2")
    svc.record_usage("T-2026-003", "U-BTX-2", "眉间", dose_ml=1.0)
    rec3 = svc.finalize("T-2026-003")
    audit.expect(rec3.batch_lines[0].approval_no == "国药准字H202400002",
                 "药品按国药准字号留痕")

    # ---------------------------------------------------------------
    print("\n[01-20] 阻断演示 C：HA+胶原+肉毒 三成分组合 → R601")
    clock.set(to_dt("2026-01-20T11:00:00+08:00"))
    tcombo = svc.open_treatment("T-BAD-COMBO", "C001", "S01", "D01", "N01",
                                ["全面部"], ["P-HA", "P-COL", "P-BTX"])
    svc.record_assessment("T-BAD-COMBO", Decision.FIT, ["无异常"], ["无禁忌"], "")
    for uid in ("U-HA-2", "U-COL-2", "U-BTX-1"):
        svc.add_consent_line("T-BAD-COMBO", uid)
        svc.scan_unit("T-BAD-COMBO", uid)
    svc.record_usage("T-BAD-COMBO", "U-HA-2", "全面部", 0.5)
    svc.record_usage("T-BAD-COMBO", "U-COL-2", "全面部", 0.3)
    svc.record_usage("T-BAD-COMBO", "U-BTX-1", "全面部", 0.2)
    blocked = audit.expect_block(lambda: svc.finalize("T-BAD-COMBO"), "R601",
                                 "组合超过机构规则上限 2 种：硬性阻断")
    audit.expect(any(v.code == "R601" for v in blocked.violations)
                 and all(uid not in [m.uid for m in svc.inventory.movements]
                         for uid in ("U-HA-2", "U-COL-2", "U-BTX-1")),
                 "阻断时三支材料库存零扣减")
    audit.expect(all(r["units"][u].status.value == "DISCARDED"
                     for u in ("U-HA-2", "U-COL-2", "U-BTX-1")),
                 "已启封材料阻断后不得回库，按医疗废物弃置")

    # ---------------------------------------------------------------
    print("\n[01-25] 闻笙 D10 视力模糊红旗 → L3 紧急提醒")
    clock.set(to_dt("2026-01-25T09:00:00+08:00"))
    fu_l3 = svc.collect_follow_up("F-003", "T-2026-003",
                                  ["视力模糊或变化"], "眉间")
    audit.expect(fu_l3.window_code == "INFECTION" and fu_l3.level == 3,
                 "D10 感染风险窗内的视功能红旗：L3 紧急复诊")
    audit.expect(fu_l3.reminder.startswith("出现视功能异常"), "只做风险提醒，不自动下诊断")

    # ---------------------------------------------------------------
    print("\n[02-10] 朝阳店 T-2026-002：江晚吟·胶原·额头（余量 0.2ml 弃置）")
    clock.set(to_dt("2026-02-10T10:00:00+08:00"))
    t2 = svc.open_treatment("T-2026-002", "C001", "S01", "D02", "N01", ["额头"])
    svc.record_assessment("T-2026-002", Decision.FIT, ["额头容量缺失"], ["无禁忌"], "")
    svc.add_consent_line("T-2026-002", "U-COL-1")
    svc.scan_unit("T-2026-002", "U-COL-1")
    svc.record_usage("T-2026-002", "U-COL-1", "额头", dose_ml=0.8)
    rec2 = svc.finalize("T-2026-002")
    line2 = rec2.batch_lines[0]
    audit.expect((line2.dose_ml, line2.remainder_ml) == (0.8, 0.2)
                 and "医疗废物" in line2.remainder_disposition,
                 "用量+余量平账，余量去向记录在案")

    # ---------------------------------------------------------------
    print("\n[03-01] 闻笙撤回营销授权：只影响营销范围")
    clock.set(to_dt("2026-03-01T09:00:00+08:00"))
    audit.expect("C002" in svc.marketing_targets(), "撤回前：闻笙在营销名单内")
    svc.withdraw_marketing("C002")
    audit.expect(svc.marketing_targets() == [], "撤回后：营销名单清空（含未授权顾客不入名单）")
    audit.expect(len(svc.records.all_for_customer("C002")) == 1
                 and svc.records.get("T-2026-003").batch_lines[0].batch_no == "B-BTX-2025A",
                 "营销撤回不删除、不改写任何医疗记录")

    # ---------------------------------------------------------------
    print("\n[04-01 召回 / 04-05] 召回批次使用 → R406，召回效力仅限本批次")
    clock.set(to_dt("2026-04-01T09:00:00+08:00"))
    svc.recall_batch(Recall(
        id="RC-2026-001", batch_no="B-HA-2024Q",
        reason="厂家主动召回：灭菌过程偏差", at=clock.now(),
    ))
    clock.set(to_dt("2026-04-05T10:00:00+08:00"))
    trecall = svc.open_treatment("T-BAD-RECALL", "C002", "S01", "D01", "N01", ["面颊"])
    svc.record_assessment("T-BAD-RECALL", Decision.FIT, ["无异常"], ["无禁忌"], "")
    svc.add_consent_line("T-BAD-RECALL", "U-HA-OLD")
    svc.scan_unit("T-BAD-RECALL", "U-HA-OLD")
    svc.record_usage("T-BAD-RECALL", "U-HA-OLD", "面颊", 1.0)
    audit.expect_block(lambda: svc.finalize("T-BAD-RECALL"), "R406",
                       "召回批次不得使用，硬性阻断")
    audit.expect(svc.inventory.deduction_count("U-HA-OLD") == 0, "召回材料零扣减")
    audit.expect(svc.records.get("T-2026-001").batch_lines[0].batch_no == "B-HA-2025A",
                 "召回不波及其他批次，旧病历保持完整")

    # ---------------------------------------------------------------
    print("\n[07-05] 周慕白资质 06-30 到期：新治疗 R201 阻断，旧病历不受影响")
    clock.set(to_dt("2026-07-05T10:00:00+08:00"))
    audit.expect_block(
        lambda: svc.open_treatment("T-BAD-EXPIRED", "C001", "S01", "D02", "N01", ["额头"]),
        "R201", "医师资质过期不得接诊",
    )
    audit.expect(svc.records.get("T-2026-002").doctor_id == "D02"
                 and svc.records.get("T-2026-002").doctor_name == "周慕白",
                 "资质失效不溯及既往：T-2026-002 病历原样留存")

    # ---------------------------------------------------------------
    print("\n[08-04] 江晚吟 D211 面颊硬结 → L2 迟发结节；跨门店到海淀店复诊")
    clock.set(to_dt("2026-08-04T09:30:00+08:00"))
    fu_late = svc.collect_follow_up("F-004", "T-2026-001",
                                    ["新发硬结或结节"], "左侧面颊")
    audit.expect(fu_late.window_code == "DELAYED" and fu_late.level == 2
                 and fu_late.elapsed_days == 211,
                 "D211 落入 180 天以上迟发窗，半年后结节信号分级 L2")
    audit.expect("迟发结节信号" in fu_late.reminder and "诊断" not in fu_late.reminder,
                 "生成迟发结节复诊提醒，不自动下诊断")

    # 海淀店接诊：沈砚之（有效资质、备案在 S02）调阅最小必要病史
    entries = svc.history.view(
        doctor=r["doctors"]["shen"], site_id="S02", customer_id="C001",
        chief_sites=("左侧面颊",), record_store=svc.records,
        reason="顾客跨店主诉面颊硬结",
    )
    audit.expect([e.treatment_id for e in entries] == ["T-2026-001"],
                 "接诊者只看到与主诉部位相关的病史（额头 T-2026-002 不可见）")
    entry = entries[0]
    mat = entry.materials[0]
    audit.expect((mat.generic_name, mat.batch_no, mat.dose_ml)
                 == ("注射用透明质酸钠凝胶", "B-HA-2025A", 1.0),
                 "跨店追查命中：2026-01-05 朝阳店透明质酸批次 B-HA-2025A / 1.0ml")
    blob = repr(entry)
    audit.expect("110120240001" not in blob and "许棠" not in blob
                 and "marketing" not in blob.lower(),
                 "最小必要：证件号、护士信息、营销授权等无关字段不下发")
    log = svc.history.access_log[-1]
    audit.expect(log.doctor_id == "D03" and log.returned_treatment_ids == ("T-2026-001",),
                 "跨店调阅全程留痕（接诊人/主诉/返回病历范围）")

    # 越权调阅必须被拒
    try:
        svc.history.view(doctor=r["nurses"]["gu"], site_id="S02", customer_id="C001",
                         chief_sites=("左侧面颊",), record_store=svc.records)
        raise AssertionError("护士调阅病史应被拒绝")
    except Unauthorized:
        audit.expect(True, "护士角色不得调阅病史")
    try:
        svc.history.view(doctor=r["doctors"]["zhou"], site_id="S02", customer_id="C001",
                         chief_sites=("左侧面颊",), record_store=svc.records)
        raise AssertionError("非本店备案医师调阅应被拒绝")
    except Unauthorized:
        audit.expect(True, "未在海淀店备案且资质过期的周慕白跨店调阅被拒绝")
    audit.expect(len(svc.history.access_log) == 1, "被拒绝的调阅不返回数据（仅授权调阅留痕）")

    # ---------------------------------------------------------------
    print("\n全局库存核对")
    consumed = sorted(m.uid for m in svc.inventory.movements)
    audit.expect(consumed == ["U-BTX-2", "U-COL-1", "U-HA-1"],
                 f"全程仅三支材料发生扣减：{consumed}")
    audit.expect(all(svc.inventory.deduction_count(u) == 1 for u in consumed),
                 "每支被扣减材料全局恰好一次")
    audit.expect(
        all(svc.inventory.deduction_count(u) == 0
            for u in ("U-HA-2", "U-HA-3", "U-COL-2", "U-BTX-1", "U-HA-OLD", "U-X-1")),
        "阻断/召回/未使用材料零扣减",
    )

    due = svc.follow_ups.due_reminders()
    audit.expect([(x.id, x.level) for x in due] == [("F-003", 3), ("F-001", 2), ("F-004", 2)],
                 "分级复诊提醒：L3 紧急在前，L2 随后，L1 不打扰")

    print("\n" + "=" * 72)
    print(f"全部 {audit.checks} 项验收通过：跨门店复诊链路可追查，库存幂等，最小必要，")
    print("阻断可审计，病历列清规则与全部批次，随访只提醒不诊断。")
    print("=" * 72)
    return {
        "checks": audit.checks,
        "records": len(svc.records),
        "movements": len(svc.inventory.movements),
        "reminders_due": len(due),
    }


if __name__ == "__main__":
    run_scenario()
