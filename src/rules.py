from __future__ import annotations
from datetime import timedelta
from .domain import ConflictError, ValidationError
TITLE='企业排污许可与超标处置'; ENTITY='排污事件'; ID_PREFIX='ED'
SEVERITIES=['normal', 'watch', 'exceedance', 'major']; STATES=['reported', 'assessing', 'remediation', 'inspection', 'closed']; TRANSITIONS={'reported': ['assessing'], 'assessing': ['remediation'], 'remediation': ['inspection'], 'inspection': ['closed'], 'closed': []}; TRANSITION_ROLES={'assessing': ['compliance_officer'], 'remediation': ['operator'], 'inspection': ['compliance_officer'], 'closed': ['director']}
CREATE_ROLES=set(['operator', 'compliance_officer']); RECORD_ROLES=set(['operator', 'compliance_officer']); AUDIT_ROLES=set(['director', 'viewer']); VIEW_ROLES=set(['operator', 'compliance_officer', 'director', 'viewer'])
SEVERITY_WEIGHT={'normal': 1.0, 'watch': 3.0, 'exceedance': 6.0, 'major': 9.0}; DEADLINE_HOURS={'normal': 72, 'watch': 24, 'exceedance': 8, 'major': 4}; TERMINAL_STATES=set(['closed'])
EXCEEDANCE_WINDOW_DAYS=30; ESCALATION_THRESHOLD=3; ESCALATION_TARGET=SEVERITIES[-1]; REVIEW_STATUSES=['open', 'closed']
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def is_exceedance(concentration,limit_value): return concentration>limit_value
def count_exceedances_within(instants,end,days=EXCEEDANCE_WINDOW_DAYS):
    start=end-timedelta(days=days)
    return sum(1 for t in instants if start<=t<=end)
def should_escalate(count): return count>=ESCALATION_THRESHOLD
def nearest_review_deadline(opened_ats,hours):
    if not opened_ats: return None
    return (min(opened_ats)+timedelta(hours=hours)).isoformat()
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records,open_reviews=0):
    if target not in TERMINAL_STATES: return []
    blockers=[]
    if open_records>0: blockers.append("仍有未关闭事项")
    if open_reviews>0: blockers.append("仍有未结复查")
    return blockers
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
