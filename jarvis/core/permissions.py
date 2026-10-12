"""Authorization: what JARVIS is allowed to do, and when it must ask first.

Three layers:

1. **Mode** - the global posture (``readonly`` / ``standard`` / ``autonomous``).
2. **Scopes** - explicit allow/deny lists of capability scopes such as ``fs.write``.
3. **Risk** - every action declares a risk level; the mode decides which levels need a
   human approval and which are refused outright.

The emergency stop short-circuits everything to DENY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from jarvis.core.models import ApprovalRequest, Risk
from jarvis.errors import Halted, PermissionDenied

# Capability scopes used across the tool registry.
SCOPES: Dict[str, str] = {
    "fs.read": "Read files inside the workspace",
    "fs.write": "Create or modify files inside the workspace",
    "shell.exec": "Execute commands inside the workspace",
    "code.run": "Run project code and test suites",
    "network.fetch": "Fetch remote URLs",
    "network.search": "Query a search provider",
    "computer.control": "Drive the desktop (keyboard, mouse, windows)",
    "system.install": "Install or remove software",
    "goal.manage": "Create, pause or cancel durable goals",
    "schedule.manage": "Create or cancel scheduled work",
    "memory.manage": "Write or delete persistent memory",
    "self.modify": "Change JARVIS configuration, prompts or tools",
    "publish": "Push, release or otherwise expose work outside the machine",
}

MODES = ("readonly", "standard", "autonomous")


@dataclass
class Decision:
    allow: bool
    needs_approval: bool
    reason: str
    scope: str = ""
    risk: Risk = Risk.READ

    @property
    def kind(self) -> str:
        if not self.allow:
            return "deny"
        return "require_approval" if self.needs_approval else "allow"


@dataclass
class PolicyEngine:
    """Evaluates every action before a tool is allowed to run it."""

    mode: str = "standard"
    allow: Set[str] = field(default_factory=lambda: set(SCOPES))
    deny: Set[str] = field(default_factory=set)
    max_auto_risk: int = 2          # risk levels auto-approved without asking
    max_allowed_risk: int = 5       # anything above this is refused in any mode
    halted: bool = False
    history: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown autonomy mode {self.mode!r}; expected one of {MODES}")
        self.allow = set(self.allow)
        self.deny = set(self.deny)
        self._apply_mode_defaults()

    # -- configuration ------------------------------------------------------ #
    def _apply_mode_defaults(self) -> None:
        if self.mode == "readonly":
            self.max_auto_risk = 1
            self.max_allowed_risk = 1
        elif self.mode == "standard":
            self.max_auto_risk = 2
            self.max_allowed_risk = 4
        else:  # autonomous
            self.max_auto_risk = 3
            self.max_allowed_risk = 5

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown autonomy mode {mode!r}")
        self.mode = mode
        self._apply_mode_defaults()

    def grant(self, scope: str) -> None:
        self.allow.add(scope)
        self.deny.discard(scope)

    def revoke(self, scope: str) -> None:
        self.deny.add(scope)
        self.allow.discard(scope)

    # -- emergency stop ----------------------------------------------------- #
    def halt(self) -> None:
        self.halted = True

    def resume(self) -> None:
        self.halted = False

    def assert_running(self) -> None:
        if self.halted:
            raise Halted("emergency stop engaged - all execution is refused")

    # -- evaluation --------------------------------------------------------- #
    def evaluate(
        self,
        scope: str,
        risk: Risk | str = Risk.READ,
        action: str = "",
        detail: str = "",
    ) -> Decision:
        risk = Risk(risk) if not isinstance(risk, Risk) else risk

        if self.halted:
            return self._record(Decision(False, False, "emergency stop engaged", scope, risk))
        if scope not in SCOPES:
            return self._record(
                Decision(False, False, f"unknown capability scope {scope!r}", scope, risk)
            )
        if scope in self.deny:
            return self._record(Decision(False, False, f"scope {scope} is denied", scope, risk))
        if risk.level > self.max_allowed_risk:
            return self._record(
                Decision(
                    False, False,
                    f"risk {risk.value} exceeds the maximum allowed by mode {self.mode!r}",
                    scope, risk,
                )
            )
        if scope not in self.allow:
            return self._record(
                Decision(True, True, f"scope {scope} is not pre-approved", scope, risk)
            )
        if risk.level > self.max_auto_risk:
            return self._record(
                Decision(True, True, f"risk {risk.value} requires approval in mode {self.mode!r}",
                         scope, risk)
            )
        return self._record(Decision(True, False, "permitted", scope, risk))

    def authorize(
        self,
        scope: str,
        risk: Risk | str = Risk.READ,
        action: str = "",
        detail: str = "",
        approver: Optional["Approver"] = None,
        task_id: str = "",
    ) -> ApprovalRequest | None:
        """Evaluate and, when required, obtain an approval.

        Returns the approval record when one was needed (approved), otherwise ``None``.
        Raises :class:`PermissionDenied` when the action may not proceed.
        """
        self.assert_running()
        decision = self.evaluate(scope, risk, action, detail)
        if not decision.allow:
            raise PermissionDenied(f"{scope}: {decision.reason}")
        if not decision.needs_approval:
            return None
        if approver is None:
            raise PermissionDenied(
                f"{scope}: approval required ({decision.reason}) but no approver is available"
            )
        approval = approver.request(
            task_id=task_id, action=action or scope, scope=scope,
            risk=risk.value, detail=detail or decision.reason,
        )
        if approval.state != "approved":
            raise PermissionDenied(
                f"{scope}: approval {approval.state} - {approval.note or decision.reason}"
            )
        return approval

    # -- helpers ------------------------------------------------------------ #
    def describe(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "halted": self.halted,
            "max_auto_risk": self.max_auto_risk,
            "max_allowed_risk": self.max_allowed_risk,
            "allowed": sorted(self.allow),
            "denied": sorted(self.deny),
            "recent_decisions": self.history[-20:],
        }

    def _record(self, decision: Decision) -> Decision:
        self.history.append(
            {
                "scope": decision.scope,
                "risk": decision.risk.value,
                "kind": decision.kind,
                "reason": decision.reason,
            }
        )
        if len(self.history) > 500:
            self.history = self.history[-500:]
        return decision


class Approver:
    """Bridge between the policy engine and a human.

    Two modes:

    * ``blocking=False`` (default) - the request is persisted and returned while
      still pending. The executive parks the task in ``awaiting_approval`` and the
      cycle resumes when a decision arrives. Nothing blocks.
    * ``blocking=True`` - ``wait`` polls the store until a decision is recorded,
      which is what an interactive CLI or a test hook wants.
    """

    def __init__(self, store, waiter=None, auto_approve: Sequence[str] = (),
                 blocking: bool = False, timeout: float = 120.0):
        self.store = store
        self._waiter = waiter
        self.auto_approve = set(auto_approve)
        self.blocking = blocking
        self.timeout = timeout

    def request(
        self, task_id: str, action: str, scope: str, risk: str, detail: str
    ) -> ApprovalRequest:
        req = ApprovalRequest(
            task_id=task_id, action=action, scope=scope, risk=risk, detail=detail
        )
        if scope in self.auto_approve:
            req.state = "approved"
            req.note = "auto-approved by policy"
            from jarvis.core.models import now as _now

            req.decided_at = _now()
            self.store.save_approval(req)
            self.store.audit("approver", "auto_approved", {"id": req.id, "scope": scope})
            return req
        self.store.save_approval(req)
        self.store.audit("approver", "approval_requested", {"id": req.id, "scope": scope,
                                                           "action": action})
        if self._waiter is not None:
            return self._waiter(req, self.timeout)
        if self.blocking:
            return self.wait(req, self.timeout)
        return req                     # park it; the cycle resumes on decision

    def wait(self, req: ApprovalRequest, timeout: Optional[float] = None) -> ApprovalRequest:
        """Block until a decision arrives (desktop UI, CLI or test hook)."""
        if self._waiter is not None:
            return self._waiter(req, self.timeout)
        import time

        deadline = time.time() + (timeout if timeout is not None else self.timeout)
        while time.time() < deadline:
            fresh = self.store.get_approval(req.id)
            if fresh and fresh.state != "pending":
                return fresh
            time.sleep(0.05)
        from jarvis.core.models import now as _now

        req.state = "expired"
        req.decided_at = _now()
        req.note = "no decision within timeout"
        self.store.save_approval(req)
        return req

    def decide(self, approval_id: str, approve: bool, note: str = "") -> ApprovalRequest:
        req = self.store.get_approval(approval_id)
        if req is None:
            raise KeyError(approval_id)
        from jarvis.core.models import now as _now

        req.state = "approved" if approve else "denied"
        req.decided_at = _now()
        req.note = note
        self.store.save_approval(req)
        self.store.audit("user", "approval_decided",
                         {"id": approval_id, "decision": req.state, "note": note})
        return req


def policy_from(goal_autonomy: Optional[str], base: PolicyEngine) -> PolicyEngine:
    """A goal may tighten (never loosen) the running policy."""
    if not goal_autonomy or goal_autonomy == base.mode:
        return base
    order = {m: i for i, m in enumerate(MODES)}
    if order.get(goal_autonomy, 1) < order[base.mode]:
        clone = PolicyEngine(
            mode=goal_autonomy, allow=set(base.allow), deny=set(base.deny),
            halted=base.halted,
        )
        return clone
    return base
