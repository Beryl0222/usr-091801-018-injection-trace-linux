"""跨门店复诊的最小必要病史视图。

接诊医师凭本人有效执业资质调阅顾客病史时，只能看到与本次主诉部位相关、
处于回看窗口内的治疗信息；营销授权状态、人员证件号码、温控流水、无关
部位的治疗均不下发。每次调阅留痕。
"""

from dataclasses import dataclass
from datetime import datetime

from .errors import Unauthorized
from .models import HISTORY_LOOKBACK_DAYS, Staff
from .records import MedicalRecord


@dataclass(frozen=True)
class HistoryEntryMaterial:
    generic_name: str
    approval_no: str
    batch_no: str
    dose_ml: float
    anatomical_site: str


@dataclass(frozen=True)
class HistoryEntry:
    treatment_id: str
    finalized_at: datetime
    site_name: str
    anatomy_sites: tuple[str, ...]
    materials: tuple[HistoryEntryMaterial, ...]
    assessment_summary: str


@dataclass(frozen=True)
class AccessLogEntry:
    doctor_id: str
    customer_id: str
    site_id: str
    at: datetime
    chief_sites: tuple[str, ...]
    returned_treatment_ids: tuple[str, ...]
    reason: str


def _site_overlap(record: MedicalRecord, chief_sites: tuple[str, ...]) -> tuple[str, ...]:
    """主诉部位与病历治疗部位按身体区域匹配。

    采用“区域前缀”匹配：主诉“面颊”可命中“左侧面颊”；主诉“全面部”命中
    所有面部记录。匹配不到则该病历不下发。
    """
    hit = []
    for treated in record.anatomy_sites:
        for chief in chief_sites:
            if chief == "全面部" and treated.endswith(("面颊", "额头", "鼻部", "下巴", "颞部", "面部")):
                hit.append(treated)
            elif chief in treated or treated in chief:
                hit.append(treated)
    return tuple(dict.fromkeys(hit))


class HistoryService:
    def __init__(self, clock):
        self.clock = clock
        self.access_log: list[AccessLogEntry] = []

    def _assert_active_doctor(self, doctor: Staff, site_id: str) -> None:
        if doctor.role != "doctor":
            raise Unauthorized("仅接诊医师可调阅病史")
        if doctor.license_expiry < self.clock.now():
            raise Unauthorized(
                f"{doctor.name} 执业资质已于 {doctor.license_expiry:%Y-%m-%d} 到期，不得调阅病史"
            )
        if "医疗美容" not in doctor.practice_scopes:
            raise Unauthorized(f"{doctor.name} 执业范围不含医疗美容")
        if site_id not in doctor.filed_site_ids:
            raise Unauthorized(f"{doctor.name} 未在该门店备案，跨店调阅被拒绝")

    def view(
        self,
        *,
        doctor: Staff,
        site_id: str,
        customer_id: str,
        chief_sites: tuple[str, ...],
        record_store,
        reason: str = "跨门店复诊评估",
    ) -> list[HistoryEntry]:
        self._assert_active_doctor(doctor, site_id)
        now = self.clock.now()
        entries: list[HistoryEntry] = []
        returned_ids: list[str] = []
        for record in sorted(record_store.all_for_customer(customer_id),
                             key=lambda r: r.finalized_at):
            age_days = (now.date() - record.finalized_at.date()).days
            if age_days > HISTORY_LOOKBACK_DAYS or age_days < 0:
                continue
            matched = _site_overlap(record, tuple(chief_sites))
            if not matched:
                continue
            materials = tuple(
                HistoryEntryMaterial(
                    generic_name=line.generic_name,
                    approval_no=line.approval_no,
                    batch_no=line.batch_no,
                    dose_ml=line.dose_ml,
                    anatomical_site=line.anatomical_site,
                )
                for line in record.batch_lines
                if line.anatomical_site in matched or any(s in line.anatomical_site for s in matched)
            )
            entries.append(HistoryEntry(
                treatment_id=record.treatment_id,
                finalized_at=record.finalized_at,
                site_name=record.site_name,
                anatomy_sites=matched,
                materials=materials,
                assessment_summary=record.assessment_summary,
            ))
            returned_ids.append(record.treatment_id)
        self.access_log.append(AccessLogEntry(
            doctor_id=doctor.id,
            customer_id=customer_id,
            site_id=site_id,
            at=now,
            chief_sites=tuple(chief_sites),
            returned_treatment_ids=tuple(returned_ids),
            reason=reason,
        ))
        return entries
