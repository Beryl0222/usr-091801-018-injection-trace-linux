"""上线前验收：用现有资质、材料和随访样例追查一例跨门店复诊。

场景：顾客在城东门店完成注射，术后不同风险时间窗报告症状；半年后
发现结节，到城西门店复诊。期间同一支材料被重复扫描，库存只能扣减
一次；城西接诊者只看到必要病史；每次治疗的最终记录都列清所采用的
规则和全部批次。
"""

import json
import unittest
from datetime import datetime, timedelta

from domain import ALL_RULES, ItemizedConsent, LicenseError, RecalledBatchError
from sample_data import build_sample_service

DAY0 = datetime(2026, 9, 19, 10, 0)  # 首次治疗时间


def consent_at(day_offset, *material_ids, hour=9):
    at = (DAY0 + timedelta(days=day_offset)).replace(hour=hour)
    return ItemizedConsent("CUST-1", tuple(material_ids), at)


class CrossStoreFollowUpAcceptanceTest(unittest.TestCase):
    """一例跨门店复诊的完整追查链路。"""

    def setUp(self):
        self.svc = build_sample_service()

    def _first_treatment(self):
        return self.svc.conduct_treatment(
            treatment_id="T-A-001",
            institution_id="STORE-A",
            customer_id="CUST-1",
            doctor_id="DOC-1",
            nurse_id="NUR-1",
            # 同一支透明质酸被重复扫描两次（扫码枪回读）
            scans=[("VIAL-HA-1", 1.5), ("VIAL-HA-1", 1.5), ("VIAL-BTX-1", 0.5)],
            consent=consent_at(0, "MAT-HA", "MAT-BTX"),
            skin_state="面颊轻度干燥，无活动性炎症",
            adaptability="适合注射：透明质酸双侧面颊各0.75ml，肉毒素鱼尾纹0.5ml",
            at=DAY0,
        )

    def test_cross_store_follow_up_trace(self):
        # —— 城东门店：首次治疗，重复扫描只扣减一次库存 ——
        record_a = self._first_treatment()
        vial = self.svc.vials["VIAL-HA-1"]
        self.assertAlmostEqual(vial.volume_remaining_ml, 0.5)  # 2.0 - 1.5，只扣一次
        self.assertEqual(
            [u for u in record_a.usages if u.vial_id == "VIAL-HA-1"]
            and len([u for u in record_a.usages if u.vial_id == "VIAL-HA-1"]),
            1,
        )
        self.assertEqual(vial.opened_at, DAY0)  # 启封时间落在批次链路上
        self.assertTrue(self.svc.batches[vial.batch_id].temperature_log)  # 入库温控

        # 最终记录列清本次采用的规则与全部批次
        self.assertEqual(record_a.rules_applied, ALL_RULES)
        self.assertEqual(record_a.batches, ("BTX260101", "HA260101"))

        # —— 术后随访：不同风险时间窗生成分级复诊提醒，不下诊断 ——
        day2 = self.svc.report_symptoms(
            "T-A-001", ("红肿", "疼痛"), DAY0 + timedelta(days=2)
        )
        self.assertEqual((day2.window_name, day2.grade), ("短期反应窗", "尽快"))
        day200 = self.svc.report_symptoms(
            "T-A-001", ("结节",), DAY0 + timedelta(days=200)
        )
        self.assertEqual((day200.window_name, day200.grade), ("迟发异常窗", "尽快"))
        for reminder in (day2, day200):
            self.assertIn("复诊", reminder.message)
            self.assertNotIn("诊断", reminder.message)

        # —— 城西门店复诊：接诊者只看到必要病史 ——
        view = self.svc.cross_store_history("CUST-1")
        self.assertNotIn("phone", view)
        self.assertNotIn("marketing_consent", view)
        self.assertNotIn("138", json.dumps(view, ensure_ascii=False))  # 联系方式不外泄
        self.assertEqual(view["allergies"], ["青霉素"])
        self.assertEqual(view["treatments"][0]["institution"], "连锁医美·城东门诊部")
        self.assertEqual(view["treatments"][0]["batches"], ["BTX260101", "HA260101"])
        self.assertEqual(len(view["symptom_reports"]), 2)  # 既往症状随病史可见

        # —— 城西门店：复诊治疗，最终记录只含本次批次 ——
        record_b = self.svc.conduct_treatment(
            treatment_id="T-B-001",
            institution_id="STORE-B",
            customer_id="CUST-1",
            doctor_id="DOC-1",
            nurse_id="NUR-1",
            scans=[("VIAL-HA-2", 1.0)],
            consent=consent_at(210, "MAT-HA"),
            skin_state="左侧面颊可及硬结，边界清，无红肿热痛",
            adaptability="硬结区暂不注射，仅右侧面颊补充透明质酸1.0ml",
            at=DAY0 + timedelta(days=210),
        )
        self.assertEqual(record_b.batches, ("HA260202",))
        self.assertEqual(record_b.rules_applied, ALL_RULES)
        self.assertNotIn("HA260101", record_b.batches)  # 不混入上次批次

        # 跨门店病史随之更新，两次治疗都可追查
        view = self.svc.cross_store_history("CUST-1")
        self.assertEqual(len(view["treatments"]), 2)

    def test_recall_expiry_and_withdrawal_stay_in_their_scope(self):
        record_a = self._first_treatment()

        # 召回批次：阻断后续使用，历史记录原样留存，且能追查受影响治疗
        self.svc.recall_batch("BATCH-BTX-01", "厂家主动召回")
        self.assertEqual(
            self.svc.treatments["T-A-001"].batches, record_a.batches
        )
        affected = self.svc.treatments_using_batch("BATCH-BTX-01")
        self.assertEqual([t.treatment_id for t in affected], ["T-A-001"])
        with self.assertRaises(RecalledBatchError):
            self.svc.conduct_treatment(
                treatment_id="T-A-002",
                institution_id="STORE-A",
                customer_id="CUST-1",
                doctor_id="DOC-1",
                nurse_id="NUR-1",
                scans=[("VIAL-BTX-2", 0.5)],
                consent=consent_at(1, "MAT-BTX"),
                skin_state="皮肤状态良好",
                adaptability="适合注射",
                at=DAY0 + timedelta(days=1),
            )

        # 资质失效：只阻断失效之后的新治疗，既有医疗记录不受影响
        with self.assertRaises(LicenseError):
            self.svc.conduct_treatment(
                treatment_id="T-A-003",
                institution_id="STORE-A",
                customer_id="CUST-1",
                doctor_id="DOC-1",
                nurse_id="NUR-1",  # 陈护士资质 2027-06-30 到期
                scans=[("VIAL-HA-2", 1.0)],
                consent=ItemizedConsent(
                    "CUST-1", ("MAT-HA",), datetime(2027, 8, 1, 9, 0)
                ),
                skin_state="皮肤状态良好",
                adaptability="适合注射",
                at=datetime(2027, 8, 1, 10, 0),
            )
        self.assertIn("T-A-001", self.svc.treatments)

        # 撤回营销授权：只退出营销名单，医疗记录按规定留存
        self.assertIn("CUST-1", self.svc.marketing_audience())
        self.svc.withdraw_marketing_consent("CUST-1")
        self.assertNotIn("CUST-1", self.svc.marketing_audience())
        self.assertIn("T-A-001", self.svc.treatments)
        self.assertEqual(
            len(self.svc.cross_store_history("CUST-1")["treatments"]), 1
        )


if __name__ == "__main__":
    unittest.main()
