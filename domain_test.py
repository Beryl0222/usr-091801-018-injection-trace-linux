"""领域规则单元测试：硬性阻断、幂等扣减与分级复诊提醒。"""

import unittest
from datetime import datetime, timedelta

from domain import (
    ALL_RULES,
    AssessmentError,
    BatchError,
    CombinationRuleError,
    ConsentError,
    InventoryError,
    ItemizedConsent,
    LicenseError,
    RecalledBatchError,
    UnknownMaterialError,
)
from sample_data import build_sample_service

NOW = datetime(2026, 9, 19, 10, 0)


def consent(*material_ids, confirmed_at=None):
    return ItemizedConsent(
        customer_id="CUST-1",
        material_ids=tuple(material_ids),
        confirmed_at=confirmed_at or datetime(2026, 9, 19, 9, 30),
    )


class DomainRuleTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_sample_service()

    def conduct(self, **overrides):
        params = dict(
            treatment_id="T-1",
            institution_id="STORE-A",
            customer_id="CUST-1",
            doctor_id="DOC-1",
            nurse_id="NUR-1",
            scans=[("VIAL-HA-1", 1.5)],
            consent=consent("MAT-HA"),
            skin_state="面部皮肤完整，轻度干燥",
            adaptability="适合注射，避开炎症区",
            at=NOW,
        )
        params.update(overrides)
        return self.svc.conduct_treatment(**params)

    # ---- R1 机构诊疗科目 ----

    def test_beauty_salon_is_not_a_legal_injection_site(self):
        with self.assertRaises(LicenseError) as ctx:
            self.conduct(institution_id="SALON-01")
        self.assertIn("生活美容", str(ctx.exception))

    def test_institution_without_required_subject_is_blocked(self):
        from domain import Institution
        from sample_data import CHAIN_RULES

        self.svc.register_institution(
            Institution(
                "STORE-C", "综合门诊部", "PDY2026-0003",
                frozenset({"美容外科"}), CHAIN_RULES,
            )
        )
        with self.assertRaises(LicenseError):
            self.conduct(institution_id="STORE-C")

    # ---- R2 人员资质有效期 ----

    def test_expired_nurse_license_is_blocked(self):
        with self.assertRaises(LicenseError) as ctx:
            self.conduct(nurse_id="NUR-2")
        self.assertIn("到期", str(ctx.exception))

    def test_nurse_cannot_act_as_doctor(self):
        with self.assertRaises(LicenseError):
            self.conduct(doctor_id="NUR-1")

    # ---- R3 材料来源 ----

    def test_unknown_source_powder_is_hard_blocked(self):
        with self.assertRaises(UnknownMaterialError) as ctx:
            self.conduct(
                scans=[("VIAL-XXX-1", 0.5)], consent=consent("MAT-XXX")
            )
        self.assertIn("来源不明", str(ctx.exception))

    # ---- R4 机构组合规则 ----

    def test_too_many_materials_in_one_session_is_blocked(self):
        with self.assertRaises(CombinationRuleError):
            self.conduct(
                scans=[
                    ("VIAL-HA-1", 1.0),
                    ("VIAL-BTX-1", 0.5),
                    ("VIAL-COL-1", 0.5),
                    ("VIAL-PLLA-1", 0.5),
                ],
                consent=consent("MAT-HA", "MAT-BTX", "MAT-COL", "MAT-PLLA"),
            )

    def test_forbidden_ingredient_combination_is_blocked(self):
        with self.assertRaises(CombinationRuleError) as ctx:
            self.conduct(
                scans=[("VIAL-HA-1", 1.0), ("VIAL-COL-1", 0.5)],
                consent=consent("MAT-HA", "MAT-COL"),
            )
        self.assertIn("透明质酸+胶原蛋白", str(ctx.exception))

    def test_volume_over_institution_limit_is_blocked(self):
        with self.assertRaises(CombinationRuleError):
            self.conduct(scans=[("VIAL-HA-1", 6.0)])

    # ---- R5 批次状态 ----

    def test_expired_batch_is_blocked(self):
        with self.assertRaises(BatchError):
            self.conduct(scans=[("VIAL-EXP-1", 0.5)])

    def test_recalled_batch_is_blocked(self):
        self.svc.recall_batch("BATCH-BTX-01", "厂家主动召回")
        with self.assertRaises(RecalledBatchError):
            self.conduct(scans=[("VIAL-BTX-2", 0.5)], consent=consent("MAT-BTX"))

    # ---- R6 逐项成分确认 ----

    def test_package_only_consent_is_rejected(self):
        # 只确认"水光+"套餐，没有逐项确认任何成分
        with self.assertRaises(ConsentError) as ctx:
            self.conduct(consent=consent())
        self.assertIn("逐项确认", str(ctx.exception))

    def test_partial_itemized_consent_is_rejected(self):
        with self.assertRaises(ConsentError) as ctx:
            self.conduct(
                scans=[("VIAL-HA-1", 1.0), ("VIAL-BTX-1", 0.5)],
                consent=consent("MAT-HA"),
            )
        self.assertIn("A型肉毒毒素", str(ctx.exception))

    def test_consent_after_treatment_start_is_rejected(self):
        with self.assertRaises(ConsentError):
            self.conduct(
                consent=consent("MAT-HA", confirmed_at=NOW + timedelta(hours=1))
            )

    # ---- R7 皮肤状态与适应性判断 ----

    def test_missing_skin_assessment_is_blocked(self):
        with self.assertRaises(AssessmentError):
            self.conduct(skin_state="  ")

    # ---- 库存幂等与复用 ----

    def test_conflicting_duplicate_scan_is_rejected(self):
        with self.assertRaises(InventoryError):
            self.conduct(scans=[("VIAL-HA-1", 1.0), ("VIAL-HA-1", 2.0)])

    def test_opened_vial_cannot_be_reused_in_another_treatment(self):
        self.conduct(treatment_id="T-1")
        with self.assertRaises(InventoryError) as ctx:
            self.conduct(treatment_id="T-2", scans=[("VIAL-HA-1", 0.5)])
        self.assertIn("复用", str(ctx.exception))

    def test_insufficient_remaining_volume_is_blocked(self):
        # 1.5ml 在机构单材料上限内，但超过该支剂 1.0ml 的余量
        with self.assertRaises(InventoryError):
            self.conduct(scans=[("VIAL-BTX-1", 1.5)], consent=consent("MAT-BTX"))

    def test_blocked_treatment_leaves_inventory_untouched(self):
        with self.assertRaises(CombinationRuleError):
            self.conduct(
                scans=[("VIAL-HA-1", 1.0), ("VIAL-COL-1", 0.5)],
                consent=consent("MAT-HA", "MAT-COL"),
            )
        self.assertEqual(self.svc.vials["VIAL-HA-1"].volume_remaining_ml, 2.0)
        self.assertEqual(self.svc.vials["VIAL-COL-1"].volume_remaining_ml, 1.0)
        self.assertNotIn("T-1", self.svc.treatments)

    def test_successful_treatment_records_rules_and_batches(self):
        record = self.conduct()
        self.assertEqual(record.rules_applied, ALL_RULES)
        self.assertEqual(record.batches, ("HA260101",))
        vial = self.svc.vials["VIAL-HA-1"]
        self.assertEqual(vial.opened_at, NOW)
        self.assertAlmostEqual(vial.volume_remaining_ml, 0.5)
        # 入库温控落在批次上
        batch = self.svc.batches[vial.batch_id]
        self.assertTrue(batch.temperature_log)

    # ---- 随访分级提醒（不下诊断） ----

    def test_reminder_is_graded_by_symptom_and_window(self):
        self.conduct()
        urgent = self.svc.report_symptoms("T-1", ("高热",), NOW + timedelta(days=1))
        self.assertEqual((urgent.grade, urgent.due_in_days), ("紧急", 1))
        prompt = self.svc.report_symptoms(
            "T-1", ("红肿", "疼痛"), NOW + timedelta(days=2)
        )
        self.assertEqual((prompt.window_name, prompt.grade), ("短期反应窗", "尽快"))
        delayed = self.svc.report_symptoms(
            "T-1", ("结节",), NOW + timedelta(days=200)
        )
        self.assertEqual((delayed.window_name, delayed.grade), ("迟发异常窗", "尽快"))
        routine = self.svc.report_symptoms(
            "T-1", ("轻微紧绷",), NOW + timedelta(days=10)
        )
        self.assertEqual((routine.window_name, routine.grade), ("感染风险窗", "常规"))

    def test_reminder_never_contains_a_diagnosis(self):
        self.conduct()
        reminder = self.svc.report_symptoms(
            "T-1", ("结节",), NOW + timedelta(days=200)
        )
        self.assertFalse(hasattr(reminder, "diagnosis"))
        self.assertIn("复诊", reminder.message)
        for term in ("诊断", "确诊", "肉芽肿", "感染症"):
            self.assertNotIn(term, reminder.message)

    # ---- 数据边界 ----

    def test_marketing_withdrawal_keeps_medical_records(self):
        self.conduct()
        self.assertIn("CUST-1", self.svc.marketing_audience())
        self.svc.withdraw_marketing_consent("CUST-1")
        self.assertNotIn("CUST-1", self.svc.marketing_audience())
        # 医疗记录按规定留存
        self.assertIn("T-1", self.svc.treatments)
        self.assertEqual(len(self.svc.cross_store_history("CUST-1")["treatments"]), 1)

    def test_recall_keeps_history_and_traces_affected_treatments(self):
        self.conduct(scans=[("VIAL-BTX-1", 0.5)], consent=consent("MAT-BTX"))
        self.svc.recall_batch("BATCH-BTX-01", "厂家主动召回")
        record = self.svc.treatments["T-1"]
        self.assertEqual(record.batches, ("BTX260101",))
        affected = self.svc.treatments_using_batch("BATCH-BTX-01")
        self.assertEqual([t.treatment_id for t in affected], ["T-1"])


if __name__ == "__main__":
    unittest.main()
