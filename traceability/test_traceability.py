"""领域规则单元测试：覆盖验收场景之外的边界与阻断分支。

运行：python3 -m unittest -v traceability.test_traceability
"""

import threading
import unittest
from datetime import timedelta

from traceability.clock import Clock, to_dt
from traceability.errors import Blocked, NotFound, TreatmentBlocked, Unauthorized, ValidationFailed
from traceability.follow_up import classify, window_for
from traceability.inventory import Inventory
from traceability.models import (
    ConsentEvent,
    Customer,
    Decision,
    Product,
    Recall,
    Staff,
    TempReading,
    Unit,
    UnitStatus,
)
from traceability.seed import build_service

BASE = "2026-01-05T09:00:00+08:00"


def happy_flow(svc, treatment_id="T-T", customer_id="C001", site_id="S01",
               doctor_id="D01", nurse_id="N01", sites=("左侧面颊",), uid="U-HA-1",
               dose=1.0, anatomy="左侧面颊"):
    """构造一笔可正常归档的治疗，返回病历。"""
    svc.open_treatment(treatment_id, customer_id, site_id, doctor_id, nurse_id,
                       anatomy_sites=list(sites))
    svc.record_assessment(treatment_id, Decision.FIT, ["无异常"], ["无禁忌"], "")
    svc.add_consent_line(treatment_id, uid)
    svc.scan_unit(treatment_id, uid)
    svc.record_usage(treatment_id, uid, anatomy, dose)
    return svc.finalize(treatment_id)


class EligibilityTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(to_dt(BASE))
        self.svc, self.refs = build_service(self.clock)

    def test_non_medical_site_blocks_at_open(self):
        with self.assertRaises(Blocked) as ctx:
            self.svc.open_treatment("T1", "C001", "S99", "D01", "N01", ["面部"])
        self.assertEqual(ctx.exception.code, "R101")

    def test_nurse_expired_blocks_independently_of_doctor(self):
        # 构造一名护士资质过期：直接替换注册对象
        expired_nurse = Staff(
            id="N99", name="过期护士", role="nurse", license_no="X",
            license_expiry=to_dt("2025-01-01T00:00:00+08:00"),
            practice_scopes=("医疗美容",), filed_site_ids=frozenset({"S01"}),
        )
        self.svc.register_staff(expired_nurse)
        with self.assertRaises(Blocked) as ctx:
            self.svc.open_treatment("T1", "C001", "S01", "D01", "N99", ["面部"])
        self.assertEqual(ctx.exception.code, "R211")

    def test_doctor_not_filed_at_site_blocks(self):
        with self.assertRaises(Blocked) as ctx:
            # 沈砚之只备案在海淀店 S02
            self.svc.open_treatment("T1", "C001", "S01", "D03", "N01", ["面部"])
        self.assertEqual(ctx.exception.code, "R201")

    def test_license_expiry_is_clock_driven(self):
        # 2026-06-29 周慕白仍有效，治疗可开立
        self.clock.set(to_dt("2026-06-29T10:00:00+08:00"))
        t = self.svc.open_treatment("T1", "C001", "S01", "D02", "N01", ["额头"])
        self.assertEqual(t.doctor_id, "D02")
        # 越过 06-30 后同一名医师立即失效
        self.clock.set(to_dt("2026-07-01T00:00:01+08:00"))
        with self.assertRaises(Blocked) as ctx:
            self.svc.open_treatment("T2", "C001", "S01", "D02", "N01", ["额头"])
        self.assertEqual(ctx.exception.code, "R201")


class TreatmentRuleTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(to_dt(BASE))
        self.svc, self.refs = build_service(self.clock)

    def test_assessment_required_before_consent(self):
        self.svc.open_treatment("T1", "C001", "S01", "D01", "N01", ["面部"])
        with self.assertRaises(ValidationFailed):
            self.svc.add_consent_line("T1", "U-HA-1")

    def test_unfit_decision_blocks_consent_and_finalize(self):
        self.svc.open_treatment("T1", "C001", "S01", "D01", "N01", ["面部"])
        self.svc.record_assessment("T1", Decision.UNFIT, ["活动期痤疮伴破溃"],
                                   ["活动性感染"], "建议先治疗皮肤炎症")
        with self.assertRaises(Blocked) as ctx:
            self.svc.add_consent_line("T1", "U-HA-1")
        self.assertEqual(ctx.exception.code, "R302")

    def test_package_name_cannot_replace_line_consent(self):
        self.svc.open_treatment("T1", "C001", "S01", "D01", "N01", ["左侧面颊"])
        self.svc.note_marketing_package("T1", "水光+臻选套餐")
        self.svc.record_assessment("T1", Decision.FIT, ["无异常"], ["无禁忌"], "")
        # 只登记套餐、不逐项确认、零用量：finalize 以 R702 阻断（空病历）
        with self.assertRaises(TreatmentBlocked) as ctx:
            self.svc.finalize("T1")
        self.assertIn("R702", [v.code for v in ctx.exception.violations])
        # 套餐名仅作营销登记，库存无任何动作
        self.assertEqual(len(self.svc.inventory.movements), 0)

    def test_cold_chain_breach_blocks_R404(self):
        # 在 HA 批次追加一次超温读数
        batch = self.refs["batches"]["ha"]
        batch.readings.append(TempReading(to_dt("2026-01-04T03:00:00+08:00"), 31.5))
        with self.assertRaises(TreatmentBlocked) as ctx:
            happy_flow(self.svc, "T1")
        self.assertIn("R404", [v.code for v in ctx.exception.violations])

    def test_expired_batch_blocks_R403(self):
        self.clock.set(to_dt("2028-01-01T00:00:00+08:00"))
        with self.assertRaises(TreatmentBlocked) as ctx:
            happy_flow(self.svc, "T1")
        self.assertIn("R403", [v.code for v in ctx.exception.violations])

    def test_cross_store_unit_blocked_R501(self):
        # 把支料改挂到海淀店库存（模拟跨店领用）
        self.refs["units"]["U-HA-1"].site_id = "S02"
        with self.assertRaises(TreatmentBlocked) as ctx:
            happy_flow(self.svc, "T1")
        self.assertIn("R501", [v.code for v in ctx.exception.violations])

    def test_opened_ttl_blocks_R503(self):
        self.svc.open_treatment("T1", "C001", "S01", "D01", "N01", ["左侧面颊"])
        self.svc.record_assessment("T1", Decision.FIT, ["无异常"], ["无禁忌"], "")
        self.svc.add_consent_line("T1", "U-HA-1")
        self.svc.scan_unit("T1", "U-HA-1")
        # 启封 4 小时后再记账
        self.clock.advance(hours=4, minutes=1)
        self.svc.record_usage("T1", "U-HA-1", "左侧面颊", 1.0)
        with self.assertRaises(TreatmentBlocked) as ctx:
            self.svc.finalize("T1")
        self.assertIn("R503", [v.code for v in ctx.exception.violations])

    def test_dose_balance_blocks_R504(self):
        # 正常记账后篡改充填量，制造用量+余量不平账
        self.svc.open_treatment("T1", "C001", "S01", "D01", "N01", ["左侧面颊"])
        self.svc.record_assessment("T1", Decision.FIT, ["无异常"], ["无禁忌"], "")
        self.svc.add_consent_line("T1", "U-HA-1")
        self.svc.scan_unit("T1", "U-HA-1")
        self.svc.record_usage("T1", "U-HA-1", "左侧面颊", 1.0)
        self.refs["units"]["U-HA-1"].fill_ml = 0.9
        with self.assertRaises(TreatmentBlocked) as ctx:
            self.svc.finalize("T1")
        self.assertIn("R504", [v.code for v in ctx.exception.violations])

    def test_max_dose_blocks_R602(self):
        # 把该成分注册的单次上限调低到 0.5ml，实际用 1.0ml → R602
        product = self.svc.products["P-HA"]
        self.svc.products["P-HA"] = Product(
            product.id, product.generic_name, product.approval_no,
            product.kind, product.registered, max_dose_ml=0.5,
        )
        with self.assertRaises(TreatmentBlocked) as ctx:
            happy_flow(self.svc, "T1", uid="U-HA-1", dose=1.0)
        self.assertIn("R602", [v.code for v in ctx.exception.violations])

    def test_quarantine_blocks_R405(self):
        self.refs["batches"]["ha"].quarantine = True
        with self.assertRaises(TreatmentBlocked) as ctx:
            happy_flow(self.svc, "T1")
        self.assertIn("R405", [v.code for v in ctx.exception.violations])

    def test_unknown_barcode_creates_no_movement(self):
        inv = self.svc.inventory
        before = len(inv.movements)
        with self.assertRaises(NotFound):
            inv.scan("U-DOES-NOT-EXIST", "T1", self.clock.now())
        self.assertEqual(len(inv.movements), before)

    def test_blocked_treatment_does_not_deduct_and_discards_opened(self):
        # HA+胶原 两成分合法；加入来源不明粉剂 → R401 同时阻断
        self.svc.open_treatment("T1", "C001", "S01", "D01", "N01",
                                ["左侧面颊"], ["P-HA", "P-X"])
        self.svc.record_assessment("T1", Decision.FIT, ["无异常"], ["无禁忌"], "")
        self.svc.add_consent_line("T1", "U-HA-1")
        self.svc.scan_unit("T1", "U-HA-1")
        self.svc.record_usage("T1", "U-HA-1", "左侧面颊", 0.5)
        # 来源不明粉剂无法逐项确认，直接扫描记账绕过应用层会在规则层暴露；
        # 这里验证：合法支料因组合中其他问题导致整体阻断时不扣减
        # 人为给治疗加一条无文号 usage
        from traceability.models import UsageRecord
        self.svc.treatments["T1"].usages.append(UsageRecord(
            uid="U-X-1", batch_no="B-X-NO-ID", product_id="P-X",
            anatomical_site="左侧面颊", dose_ml=2.0, remainder_ml=0.0,
            remainder_disposition="无余量（整支用完）", at=self.clock.now(),
        ))
        with self.assertRaises(TreatmentBlocked) as ctx:
            self.svc.finalize("T1")
        codes = [v.code for v in ctx.exception.violations]
        self.assertIn("R401", codes)
        self.assertEqual(self.svc.inventory.deduction_count("U-HA-1"), 0)
        self.assertEqual(self.refs["units"]["U-HA-1"].status, UnitStatus.DISCARDED)

    def test_record_is_immutable_after_finalize(self):
        rec = happy_flow(self.svc, "T1")
        self.assertEqual(len(self.svc.records), 1)
        # 再次尝试通过任何修改接口都应被拒
        with self.assertRaises(ValidationFailed):
            self.svc.record_assessment("T1", Decision.UNFIT, [], [], "")


class InventoryConcurrencyTest(unittest.TestCase):
    def test_concurrent_consume_deducts_exactly_once(self):
        clock = Clock(to_dt(BASE))
        svc, refs = build_service(clock)
        inv = svc.inventory
        uid = "U-HA-3"
        # 模拟“已启封待核销”状态：并发 finalize 竞争同一支
        inv.scan(uid, "T-WINNER", clock.now())
        errors = []

        def consume(tid):
            try:
                inv.consume_for(tid, [uid], clock.now())
            except Blocked as exc:
                errors.append((tid, exc.code))

        inv.consume_for("T-WINNER", [uid], clock.now())  # 成功者
        threads = [threading.Thread(target=consume, args=(f"T-LOSE-{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(inv.deduction_count(uid), 1)
        self.assertEqual(len(errors), 8)
        self.assertTrue(all(code == "R505" for _, code in errors))

    def test_same_treatment_repeated_consume_is_idempotent(self):
        clock = Clock(to_dt(BASE))
        svc, _ = build_service(clock)
        inv = svc.inventory
        inv.scan("U-HA-3", "T1", clock.now())
        first = inv.consume_for("T1", ["U-HA-3"], clock.now())
        second = inv.consume_for("T1", ["U-HA-3"], clock.now())
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(inv.deduction_count("U-HA-3"), 1)


class FollowUpTest(unittest.TestCase):
    def test_window_boundaries(self):
        self.assertEqual(window_for(0), "EARLY")
        self.assertEqual(window_for(7), "EARLY")
        self.assertEqual(window_for(8), "INFECTION")
        self.assertEqual(window_for(30), "INFECTION")
        self.assertEqual(window_for(31), "LATE")
        self.assertEqual(window_for(180), "LATE")
        self.assertEqual(window_for(181), "DELAYED")
        self.assertEqual(window_for(400), "DELAYED")

    def test_level3_beats_level1(self):
        level, _ = classify(("轻度红肿", "视力模糊或变化"), days=3)
        self.assertEqual(level, 3)

    def test_late_nodule_is_level2_without_diagnosis(self):
        level, advice = classify(("新发硬结或结节",), days=211)
        self.assertEqual(level, 2)
        self.assertIn("迟发结节", advice)
        self.assertNotIn("诊断为", advice)

    def test_unknown_symptom_defaults_to_level1(self):
        level, advice = classify(("轻微瘙痒",), days=5)
        self.assertEqual(level, 1)


class MarketingAndRecordRetentionTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(to_dt(BASE))
        self.svc, self.refs = build_service(self.clock)

    def test_consent_event_stream_default_deny(self):
        c = Customer("C9", "测试顾客")
        self.assertFalse(c.marketing_allowed_at(self.clock.now()))
        c.consent_events.append(ConsentEvent(self.clock.now(), True))
        self.assertTrue(c.marketing_allowed_at(self.clock.now()))
        self.clock.advance(days=2)
        c.consent_events.append(ConsentEvent(self.clock.now(), False))
        self.assertFalse(c.marketing_allowed_at(self.clock.now()))
        # 撤回不影响撤回前时点的判定
        self.assertTrue(c.marketing_allowed_at(self.clock.now() - timedelta(days=1)))

    def test_recall_scoped_to_single_batch(self):
        self.svc.recall_batch(Recall("RC1", "B-HA-2025A", "抽检异常", self.clock.now()))
        # 召回批次阻断
        with self.assertRaises(TreatmentBlocked):
            happy_flow(self.svc, "T1")
        # 其他批次（肉毒）不受影响，可正常治疗
        rec = happy_flow(self.svc, "T2", uid="U-BTX-2", sites=["眉间"], anatomy="眉间")
        self.assertEqual(rec.batch_lines[0].batch_no, "B-BTX-2025A")

    def test_retention_15_years_and_records_survive_revocation(self):
        rec = happy_flow(self.svc, "T1")
        self.assertEqual(rec.retention_until.year, rec.finalized_at.year + 15)
        self.svc.withdraw_marketing("C001")
        self.assertIs(self.svc.records.get("T1"), rec)


class HistoryViewTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(to_dt(BASE))
        self.svc, self.refs = build_service(self.clock)
        # 两笔病史：面颊（T-2026-001）与额头（T-2026-002）
        happy_flow(self.svc, "T-2026-001", uid="U-HA-1",
                   sites=["左侧面颊"], anatomy="左侧面颊")
        self.clock.set(to_dt("2026-02-10T10:00:00+08:00"))
        happy_flow(self.svc, "T-2026-002", doctor_id="D02", uid="U-COL-1",
                   sites=["额头"], anatomy="额头", dose=0.8)
        self.clock.set(to_dt("2026-08-04T09:00:00+08:00"))

    def test_site_filter_returns_only_relevant_records(self):
        entries = self.svc.history.view(
            doctor=self.refs["doctors"]["shen"], site_id="S02", customer_id="C001",
            chief_sites=("左侧面颊",), record_store=self.svc.records)
        self.assertEqual([e.treatment_id for e in entries], ["T-2026-001"])

    def test_lookback_window_365_days(self):
        self.clock.set(to_dt("2027-08-04T09:00:00+08:00"))
        entries = self.svc.history.view(
            doctor=self.refs["doctors"]["shen"], site_id="S02", customer_id="C001",
            chief_sites=("全面部",), record_store=self.svc.records)
        # 2026-01-05 距今超过 365 天；02-10 距 2027-08-04 也超过 365 天
        self.assertEqual(entries, [])

    def test_non_doctor_rejected(self):
        with self.assertRaises(Unauthorized):
            self.svc.history.view(
                doctor=self.refs["nurses"]["gu"], site_id="S02", customer_id="C001",
                chief_sites=("左侧面颊",), record_store=self.svc.records)

    def test_expired_doctor_rejected(self):
        with self.assertRaises(Unauthorized):
            self.svc.history.view(
                doctor=self.refs["doctors"]["zhou"], site_id="S01", customer_id="C001",
                chief_sites=("左侧面颊",), record_store=self.svc.records)

    def test_minimum_necessary_excludes_sensitive_fields(self):
        entries = self.svc.history.view(
            doctor=self.refs["doctors"]["shen"], site_id="S02", customer_id="C001",
            chief_sites=("左侧面颊",), record_store=self.svc.records)
        blob = repr(entries)
        for secret in ("110120240001", "20211020260001", "许棠", "顾清晨", "N01"):
            self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main()
