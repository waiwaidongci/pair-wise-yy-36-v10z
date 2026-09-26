from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import parse_instant, utc_now
from .domain import (ensure_role, normalize_severity, require_number,
                     require_text, require_timestamp)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, ESCALATION_TARGET,
                    RECORD_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    count_exceedances_within, escalation_required,
                    is_exceedance, nearest_review_deadline, priority_score,
                    response_deadline_hours, role_for_transition,
                    should_escalate, validate_transition)


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

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target,
                                       self.repository.open_record_count(item_id),
                                       self.repository.open_review_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def add_reading(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        sampled_at = require_timestamp(payload.get("sampled_at"), "sampled_at")
        concentration = require_number(payload.get("concentration"), "concentration")
        limit_value = require_number(payload.get("limit_value"), "limit_value", 0.000001)
        report_no = require_text(payload.get("report_no"), "report_no", 100)
        over = is_exceedance(concentration, limit_value)
        outcome = self.repository.register_reading(
            item_id, sampled_at, concentration, limit_value, report_no, over, actor)
        reading = outcome["reading"]
        reading["created"] = outcome["created"]
        if not outcome["created"]:
            return reading
        self.repository.append_audit("reading", ENTITY, item_id, actor, {
            "reading_id": reading["id"], "report_no": report_no,
            "sampled_at": sampled_at, "concentration": concentration,
            "limit_value": limit_value, "is_exceedance": over,
        })
        if outcome["opened_review"] is not None:
            self.repository.append_audit("review_open", ENTITY, item_id, actor, {
                "review_id": outcome["opened_review"]["id"],
                "reading_id": reading["id"],
            })
        if outcome["closed_reviews"]:
            self.repository.append_audit("review_close", ENTITY, item_id, actor, {
                "review_ids": [r["id"] for r in outcome["closed_reviews"]],
                "closed_by_reading_id": reading["id"],
            })
        if over:
            item = self.repository.get_item(item_id)
            count = count_exceedances_within(
                [parse_instant(s) for s in self.repository.exceedance_sampled_ats(item_id)],
                parse_instant(sampled_at))
            if should_escalate(count) and item["severity"] != ESCALATION_TARGET:
                self.repository.escalate_item(item_id, ESCALATION_TARGET, actor)
                self.repository.append_audit("escalate", ENTITY, item_id, actor, {
                    "from": item["severity"], "to": ESCALATION_TARGET,
                    "exceedance_count_30d": count,
                })
        return reading

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def list_readings(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_readings(item_id)

    def list_reviews(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_reviews(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        hours = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = hours
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["exceedance_count_30d"] = count_exceedances_within(
            [parse_instant(s) for s in self.repository.exceedance_sampled_ats(item["id"])],
            parse_instant(utc_now()))
        result["current_level"] = item["severity"]
        opened = [parse_instant(t) for t in
                  self.repository.open_review_opened_ats(item["id"])]
        result["open_reviews"] = len(opened)
        result["nearest_deadline"] = nearest_review_deadline(opened, hours)
        return result
