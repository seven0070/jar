"""Typed failure classes used by recovery, verification and the executive."""

from __future__ import annotations


class JarvisError(Exception):
    """Base class for every JARVIS failure."""

    kind = "error"
    recoverable = False


class TransientError(JarvisError):
    """Temporary failure (timeout, rate limit, locked file). Retryable."""

    kind = "transient"
    recoverable = True


class ToolError(JarvisError):
    """A tool rejected or could not complete the call."""

    kind = "tool_error"
    recoverable = True


class PermissionDenied(JarvisError):
    """Policy engine refused the action, or the user denied the approval."""

    kind = "permission_denied"
    recoverable = False


class Halted(JarvisError):
    """The emergency stop is engaged; no work may proceed."""

    kind = "halted"
    recoverable = False


class VerificationFailed(JarvisError):
    """Independent verification did not confirm the claimed result."""

    kind = "verification_failed"
    recoverable = True


class Unverifiable(VerificationFailed):
    """No independent evidence could be collected at all."""

    kind = "unverifiable"
    recoverable = False


class PlannerError(JarvisError):
    """No usable plan could be produced for the intent."""

    kind = "planner_error"
    recoverable = False


class ModelUnavailable(JarvisError):
    """No configured model could serve the request."""

    kind = "model_unavailable"
    recoverable = True
