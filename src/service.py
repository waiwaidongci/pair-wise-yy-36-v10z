from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ConflictError, ensure_role, normalize_severity,
                     normalize_timestamp, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, READING_ROLES,
                    RECORD_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, exceedance_window,
                    is_reading_exceedance, major_is_sticky,
                    major_threshold_reached, open_review_blocks_close, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def register_reading(self, item_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, READING_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)
        sampled_at = normalize_timestamp(payload.get("sampled_at"), "sampled_at")
        concentration = require_number(payload.get("concentration"), "concentration")
        limit_value = require_number(payload.get("limit_value"), "limit_value", 0.000001)
        report_no = require_text(payload.get("report_no"), "report_no", 100)

        # 同一报告编号重复提交沿用首次结果，不产生任何副作用
        existing = self.repository.get_reading_by_report_no(item_id, report_no)
        if existing is not None:
            result = self._reading_view(existing)
            result["duplicate"] = True
            return result

        is_exceedance = is_reading_exceedance(concentration, limit_value)
        try:
            reading = self.repository.insert_reading(
                item_id, sampled_at, concentration, limit_value, report_no,
                is_exceedance, actor)
        except ConflictError:
            # 并发下同一报告编号也沿用首次结果
            existing = self.repository.get_reading_by_report_no(item_id, report_no)
            if existing is not None:
                result = self._reading_view(existing)
                result["duplicate"] = True
                return result
            raise

        opened_review = None
        closed_reviews = 0
        escalated = False
        if is_exceedance:
            # 超出许可限值就建未结复查，等待之后最早的达标读数结清
            opened_review = self.repository.open_review_for_reading(reading)
            window_end = datetime.fromisoformat(sampled_at)
            window_start = exceedance_window(window_end).isoformat()
            count = self.repository.count_exceedances_since(
                item_id, window_start, sampled_at)
            item = self.repository.get_item(item_id)
            # 30天内第3次超标升为重大，读数回落也不降级
            if major_threshold_reached(count) and not major_is_sticky(item["severity"]):
                self.repository.escalate_item(item_id, actor)
                escalated = True
        else:
            # 之后最早的达标读数结清此前所有未结复查
            closed_reviews = self.repository.close_open_reviews(item_id, reading["id"])

        self.repository.append_audit("reading", ENTITY, item_id, actor, {
            "reading_id": reading["id"],
            "report_no": report_no,
            "sampled_at": sampled_at,
            "concentration": concentration,
            "limit_value": limit_value,
            "is_exceedance": is_exceedance,
            "review_opened": opened_review["id"] if opened_review else None,
            "reviews_closed": closed_reviews,
            "escalated_to_major": escalated,
        })
        return self._reading_view(reading)

    @staticmethod
    def _reading_view(reading: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(reading)
        result["is_exceedance"] = bool(reading["is_exceedance"])
        result.setdefault("duplicate", False)
        return result

    def list_readings(self, item_id: int, role: str) -> list:
        self._view(role)
        return [self._reading_view(r) for r in self.repository.list_readings(item_id)]

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        # 未结复查还在时关闭返回409并保持原状态
        if open_review_blocks_close(target, self.repository.open_review_count(item_id)):
            raise ConflictError("仍有未结复查，不能关闭")
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        now = datetime.fromisoformat(utc_now())
        result["exceedance_count_30d"] = self.repository.count_exceedances_since(
            item["id"], exceedance_window(now).isoformat(), now.isoformat())
        result["current_level"] = item["severity"]
        result["next_deadline"] = self.repository.next_review_deadline(item["id"])
        return result
