"""追溯应用服务：把资质核验、治疗全流程、库存核销、病历、随访编排起来。"""

from .clock import Clock
from .errors import Blocked, NotFound, TreatmentBlocked, ValidationFailed
from .follow_up import FollowUpService
from .history import HistoryService
from .inventory import Inventory
from .models import (
    ConsentEvent,
    Customer,
    Decision,
    Site,
    SkinAssessment,
    Staff,
    Treatment,
    UsageRecord,
    ConsentLine,
)
from .records import RecordStore, build_record
from .rules import RULE_BOOK_VERSION, RuleContext, evaluate
from .models import Product


class TraceService:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.sites: dict[str, Site] = {}
        self.staff: dict[str, Staff] = {}
        self.products: dict[str, Product] = {}
        self.customers: dict[str, Customer] = {}
        self.treatments: dict[str, Treatment] = {}
        self.inventory = Inventory()
        self.records = RecordStore()
        self.follow_ups = FollowUpService(clock)
        self.history = HistoryService(clock)
        # 营销套餐名称仅作登记，不构成知情同意
        self.package_labels: dict[str, str] = {}

    # ================= 基础档案 =================
    def register_site(self, site: Site):
        self.sites[site.id] = site

    def register_staff(self, staff: Staff):
        self.staff[staff.id] = staff

    def register_product(self, product: Product):
        self.products[product.id] = product

    def register_customer(self, customer: Customer):
        self.customers[customer.id] = customer

    def revoke_staff_license(self, staff_id: str):
        """资质注销/失效：把有效期推到过去，所有新核验立即失效；旧病历不改写。"""
        person = self.staff[staff_id]
        object.__setattr__(person, "license_expiry", self.clock.now().replace(year=self.clock.now().year - 1))

    # ================= 治疗前核验 =================
    def eligibility_report(self, site_id: str, doctor_id: str, nurse_id: str) -> list[dict]:
        """治疗前核验：机构科目、医师与护士资质，逐项给出通过/阻断。"""
        now = self.clock.now()
        site = self.sites[site_id]
        doctor = self.staff[doctor_id]
        nurse = self.staff[nurse_id]
        items: list[dict] = []

        def add(passed: bool, code: str, name: str, detail: str):
            items.append({"passed": passed, "code": code, "name": name, "detail": detail})

        add(site.is_medical, "R101", "合法医疗机构",
            "" if site.is_medical else f"{site.name} 非医疗机构，生活美容场所禁止注射")
        add(site.medical_subject_registered, "R102", "诊疗科目登记",
            site.subject_detail if site.medical_subject_registered
            else f"未登记医疗美容科诊疗科目")
        for code, person, label in (("R201", doctor, "医师"), ("R211", nurse, "护士")):
            valid = person.license_expiry >= now
            add(valid, code, f"{label}资质有效期",
                f"{person.license_no} 有效期至 {person.license_expiry:%Y-%m-%d}" if valid
                else f"{person.name} 资质已于 {person.license_expiry:%Y-%m-%d} 到期")
            in_scope = "医疗美容" in person.practice_scopes
            add(in_scope, code, f"{label}执业范围",
                "医疗美容" if in_scope else f"执业范围 {', '.join(person.practice_scopes) or '未登记'}")
            filed = site_id in person.filed_site_ids
            add(filed, code, f"{label}执业地点备案",
                f"已在 {site.name} 备案" if filed else f"未在 {site.name} 备案")
        return items

    # ================= 治疗全流程 =================
    def open_treatment(self, treatment_id, customer_id, site_id, doctor_id, nurse_id,
                       anatomy_sites, planned_product_ids=None) -> Treatment:
        for entity, registry, label in (
            (customer_id, self.customers, "顾客"),
            (site_id, self.sites, "门店"),
            (doctor_id, self.staff, "医师"),
            (nurse_id, self.staff, "护士"),
        ):
            if entity not in registry:
                raise NotFound(f"{label} {entity} 不存在")
        # 治疗前硬性核验：任何一项不过，不得开立治疗
        failing = [i for i in self.eligibility_report(site_id, doctor_id, nurse_id)
                   if not i["passed"]]
        if failing:
            first = failing[0]
            raise Blocked(first["code"], first["name"], first["detail"] or "治疗前核验未通过")
        treatment = Treatment(
            id=treatment_id,
            customer_id=customer_id,
            site_id=site_id,
            doctor_id=doctor_id,
            nurse_id=nurse_id,
            anatomy_sites=list(anatomy_sites),
            planned_product_ids=list(planned_product_ids or []),
        )
        self.treatments[treatment_id] = treatment
        return treatment

    def _require_open(self, treatment_id: str) -> Treatment:
        treatment = self.treatments.get(treatment_id)
        if treatment is None:
            raise NotFound(f"治疗 {treatment_id} 不存在")
        if treatment.finalized:
            raise ValidationFailed(f"治疗 {treatment_id} 已归档，记录不可变")
        return treatment

    def note_marketing_package(self, treatment_id: str, package_name: str):
        """登记顾客购买的营销套餐名（如“水光+”）。仅营销用途，不能替代成分确认。"""
        self._require_open(treatment_id)
        self.package_labels[treatment_id] = package_name

    def record_assessment(self, treatment_id: str, decision: Decision,
                          findings, contraindications_checked, notes: str):
        treatment = self._require_open(treatment_id)
        treatment.assessment = SkinAssessment(
            doctor_id=treatment.doctor_id,
            at=self.clock.now(),
            decision=decision,
            findings=tuple(findings),
            contraindications_checked=tuple(contraindications_checked),
            notes=notes,
        )
        return treatment.assessment

    def add_consent_line(self, treatment_id: str, unit_uid: str):
        """顾客逐项确认：一次确认一种成分，精确到批准文号/批次/支料条码。"""
        treatment = self._require_open(treatment_id)
        if treatment.assessment is None:
            raise ValidationFailed("须先完成医师皮肤适应性判断，顾客才能逐项确认成分")
        if treatment.assessment.decision == Decision.UNFIT:
            raise Blocked("R302", "不适应结论", "医师已判定不适应，不得签署成分确认")
        unit = self.inventory.get_unit(unit_uid)
        batch = self.inventory.batches.get(unit.batch_no)
        product = self.products.get(batch.product_id if batch else None)
        if product is None or not product.approval_no or not product.registered:
            raise Blocked(
                "R401", "注册批准文号与来源核验",
                f"支料 {unit_uid} 成分无有效批准文号，不得进入逐项确认（来源不明硬性阻断）",
            )
        key = (product.id, unit.batch_no, unit_uid)
        if key not in {(c.product_id, c.batch_no, c.unit_uid) for c in treatment.consents}:
            treatment.consents.append(ConsentLine(
                product_id=product.id,
                generic_name=product.generic_name,
                approval_no=product.approval_no,
                batch_no=unit.batch_no,
                unit_uid=unit_uid,
                confirmed_at=self.clock.now(),
            ))
        return treatment.consents[-1]

    def scan_unit(self, treatment_id: str, unit_uid: str):
        """扫描启封一支材料（重复扫描幂等，零扣减）。"""
        treatment = self._require_open(treatment_id)
        if treatment.assessment is None:
            raise ValidationFailed("治疗流程要求先完成皮肤适应性判断")
        return self.inventory.scan(unit_uid, treatment_id, self.clock.now())

    def record_usage(self, treatment_id: str, unit_uid: str, anatomical_site: str,
                     dose_ml: float, remainder_disposition: str = "余量当场按医疗废物弃置"):
        treatment = self._require_open(treatment_id)
        unit = self.inventory.get_unit(unit_uid)
        if unit.status.name != "OPENED" or unit.opened_in_treatment != treatment_id:
            raise ValidationFailed(
                f"支料 {unit_uid} 未在本次治疗启封，禁止登记用量（当前状态 {unit.status.value}）"
            )
        batch = self.inventory.batches[unit.batch_no]
        product = self.products[batch.product_id]
        if (product.id, unit.batch_no, unit_uid) not in {
            (c.product_id, c.batch_no, c.unit_uid) for c in treatment.consents
        }:
            raise Blocked(
                "R701", "逐项成分知情同意",
                f"{product.generic_name}（{unit.batch_no}/{unit_uid}）未经逐项确认，不得使用",
            )
        if anatomical_site not in treatment.anatomy_sites:
            raise ValidationFailed(
                f"注射部位 {anatomical_site} 不在本次治疗登记部位 {treatment.anatomy_sites} 内"
            )
        if dose_ml <= 0 or dose_ml > unit.fill_ml + 1e-9:
            raise ValidationFailed(f"用量 {dose_ml}ml 超出支料充填 {unit.fill_ml}ml")
        if any(u.uid == unit_uid for u in treatment.usages):
            raise ValidationFailed(f"支料 {unit_uid} 已登记用量，不得重复记账")
        remainder = round(unit.fill_ml - dose_ml, 6)
        if remainder > 0 and not remainder_disposition:
            raise Blocked("R504", "余量去向", f"支料 {unit_uid} 余量必须记录处置去向")
        usage = UsageRecord(
            uid=unit_uid,
            batch_no=unit.batch_no,
            product_id=product.id,
            anatomical_site=anatomical_site,
            dose_ml=dose_ml,
            remainder_ml=remainder,
            remainder_disposition=remainder_disposition if remainder > 0 else "无余量（整支用完）",
            at=self.clock.now(),
        )
        treatment.usages.append(usage)
        return usage

    def finalize(self, treatment_id: str):
        """归档放行：全量规则评估通过后原子核销；任何失败都不产生扣减。"""
        treatment = self._require_open(treatment_id)
        if treatment.assessment is None:
            raise Blocked("R301", "治疗前适应性判断", "缺少皮肤适应性判断记录")

        ctx = RuleContext(
            now=self.clock.now(),
            site=self.sites[treatment.site_id],
            doctor=self.staff[treatment.doctor_id],
            nurse=self.staff[treatment.nurse_id],
            treatment=treatment,
            products=self.products,
            batches=self.inventory.batches,
            units=self.inventory.units,
        )
        violations = evaluate(ctx)
        if violations:
            # 已启封支料不得回库：按医疗废物弃置；库存从未扣减
            for usage in treatment.usages:
                self.inventory.discard_opened(usage.uid)
            for unit in self.inventory.units.values():
                if unit.opened_in_treatment == treatment_id:
                    self.inventory.discard_opened(unit.uid)
            raise TreatmentBlocked(treatment_id, violations)

        self.inventory.consume_for(
            treatment_id,
            [u.uid for u in treatment.usages],
            self.clock.now(),
        )
        treatment.finalized = True
        treatment.finalized_at = self.clock.now()
        treatment.rule_book_version = RULE_BOOK_VERSION
        record = build_record(ctx, violations)
        self.records.save(record)
        return record

    # ================= 术后随访 =================
    def collect_follow_up(self, report_id: str, treatment_id: str, symptoms, chief_site: str):
        record = self.records.get(treatment_id)
        return self.follow_ups.collect(
            report_id=report_id,
            treatment_id=treatment_id,
            customer_id=record.customer_id,
            treatment_at=record.finalized_at,
            symptoms=tuple(symptoms),
            chief_site=chief_site,
        )

    # ================= 召回与授权（合法范围隔离）=================
    def recall_batch(self, recall):
        """批次召回仅作用于该批次：阻断新治疗，不删除既有病历。"""
        self.inventory.recall(recall)

    def grant_marketing(self, customer_id: str):
        customer = self.customers[customer_id]
        customer.consent_events.append(ConsentEvent(self.clock.now(), True))

    def withdraw_marketing(self, customer_id: str):
        """撤回营销授权：只影响营销触达范围；医疗服务与病历留存不受影响。"""
        customer = self.customers[customer_id]
        customer.consent_events.append(ConsentEvent(self.clock.now(), False))

    def marketing_targets(self) -> list[str]:
        """当前时刻允许营销触达的顾客名单（撤回即移出，不触碰医疗数据）。"""
        now = self.clock.now()
        return [cid for cid, c in self.customers.items() if c.marketing_allowed_at(now)]
