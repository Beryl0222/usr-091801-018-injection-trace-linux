"""领域错误类型。硬性阻断一律带规则编号，便于审计追溯。"""


class TraceError(Exception):
    """领域错误基类。"""


class NotFound(TraceError):
    """对象不存在或条码无法识别（同样不产生任何库存移动）。"""


class Unauthorized(TraceError):
    """接诊者不具备查看该病史的资格。"""


class Blocked(TraceError):
    """命中放行规则的硬性阻断。

    code   规则编号（如 R401）
    name   规则名称
    reason 阻断原因
    """

    def __init__(self, code: str, name: str, reason: str):
        super().__init__(f"{code} {name}: {reason}")
        self.code = code
        self.name = name
        self.reason = reason


class ValidationFailed(TraceError):
    """输入不满足领域约束（如把套餐名当作成分确认）。"""


class TreatmentBlocked(TraceError):
    """治疗归档时命中一条以上放行规则。携带全部命中项以便审计。"""

    def __init__(self, treatment_id: str, violations: list):
        codes = ", ".join(v.code for v in violations)
        super().__init__(f"治疗 {treatment_id} 被硬性阻断：{codes}")
        self.treatment_id = treatment_id
        self.violations = violations
