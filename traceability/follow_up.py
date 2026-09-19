"""术后随访：按风险时间窗收集症状，按信号分级生成复诊提醒。

本模块只做信号分级与提醒，绝不输出诊断结论。分级依据：
- 3 级（紧急）：视功能异常、剧烈疼痛、发热伴红肿扩散等红旗信号，
  任何窗口出现都要求立即急诊/联系手术医师；
- 2 级（尽快复诊）：180 天后新发结节/硬结、感染窗内红肿热痛等；
- 1 级（常规回访）：短期轻度红肿等预期反应或无症状。
"""

from datetime import datetime

from .clock import Clock
from .models import FOLLOW_UP_WINDOWS, FollowUpReport

# 症状信号分级表（机构随访规则 V1，随提醒记录留存）
FOLLOW_UP_RULE_VERSION = "FU-2026.1"

LEVEL3_SIGNALS = {
    "视力模糊或变化": "出现视功能异常，立即急诊并联系接诊医师（警惕血管相关急症）",
    "眼周剧烈疼痛伴皮肤发白": "立即急诊（组织缺血红旗信号）",
    "发热超过38.5度伴红肿扩散": "立即就诊发热门诊/急诊排查感染",
}

LEVEL2_SIGNALS = {
    # 迟发窗信号：半年后结节
    "新发硬结或结节": "请于3日内复诊，由医师触诊评估，必要时安排影像检查",
    "结节增大或反复": "请于3日内复诊评估结节性质",
    # 感染窗信号
    "红肿热痛持续加重": "请于24小时内复诊，排查感染与排异反应",
    "针眼渗液或化脓": "请于24小时内复诊，勿自行挤压或用药",
    # 迟发异常窗
    "注射区肤色持续异常": "请尽快复诊评估",
}

LEVEL1_SIGNALS = {
    "轻度红肿": "多为注射后短期反应，继续观察，48小时内随访",
    "轻微胀痛": "多为短期反应，避免按压，48小时内随访",
    "无症状": "保持常规随访，出现新症状随时上报",
}


def elapsed_days(treatment_at: datetime, now: datetime) -> int:
    return (now.date() - treatment_at.date()).days


def window_for(days: int) -> str:
    for window in FOLLOW_UP_WINDOWS:
        if window.contains(days):
            return window.code
    return "EARLY"


def classify(symptoms: tuple[str, ...], days: int) -> tuple[int, str]:
    """返回 (级别, 提醒文案)。命中多个信号时取最高级别。"""

    for symptom in symptoms:
        if symptom in LEVEL3_SIGNALS:
            return 3, LEVEL3_SIGNALS[symptom]
    for symptom in symptoms:
        if symptom in LEVEL2_SIGNALS:
            advice = LEVEL2_SIGNALS[symptom]
            if days > 180 and "结节" in symptom:
                advice = "迟发结节信号（注射已超过180天），" + advice
            return 2, advice
    for symptom in symptoms:
        if symptom in LEVEL1_SIGNALS:
            return 1, LEVEL1_SIGNALS[symptom]
    return 1, "保持常规随访，出现新症状随时上报"


class FollowUpService:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.reports: list[FollowUpReport] = []

    def collect(
        self,
        report_id: str,
        treatment_id: str,
        customer_id: str,
        treatment_at: datetime,
        symptoms: tuple[str, ...],
        chief_site: str,
    ) -> FollowUpReport:
        now = self.clock.now()
        days = elapsed_days(treatment_at, now)
        level, reminder = classify(tuple(symptoms), days)
        report = FollowUpReport(
            id=report_id,
            treatment_id=treatment_id,
            customer_id=customer_id,
            at=now,
            elapsed_days=days,
            window_code=window_for(days),
            symptoms=tuple(symptoms),
            chief_site=chief_site,
            level=level,
            reminder=reminder,
        )
        self.reports.append(report)
        return report

    def due_reminders(self) -> list[FollowUpReport]:
        """当前所有需复诊的信号（2 级尽快、3 级紧急）。"""
        return sorted(
            (r for r in self.reports if r.level >= 2),
            key=lambda r: (-r.level, r.at),
        )
