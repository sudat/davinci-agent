"""Budget/retry policy bound to the Todo-11 failure-classification table.

Only the transient class may retry, bounded by the resolved budget; permanent,
blocking-human, and unknown error codes never retry (``classify_error`` maps
unknown codes to permanent). All costs are integer units — floats never appear.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.job_runner.stage_classify import classify_error
from services.job_runner.stage_runner_models import RetryPolicy

if TYPE_CHECKING:
    from services.config.models import BudgetPolicy
    from services.job_runner.stage_runner_models import FailureClass


class BudgetExceededError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def max_attempts_for(budget: BudgetPolicy, failure_class: FailureClass) -> int:
    """Maximum attempts for one failure class under this budget."""

    match failure_class:
        case "transient":
            return budget.transient_max_attempts
        case "permanent":
            return budget.permanent_max_attempts
        case "blocking_human":
            return budget.blocking_human_max_attempts


def max_attempts_for_error_code(budget: BudgetPolicy, error_code: str) -> int:
    """Maximum attempts for a concrete error code via the Todo-11 table."""

    return max_attempts_for(budget, classify_error(error_code))


def retry_policy_for(budget: BudgetPolicy) -> RetryPolicy:
    """The Todo-11 ``RetryPolicy`` bound to this budget (transient-only retry)."""

    return RetryPolicy(max_attempts=budget.transient_max_attempts)


def assert_cost_within_ceiling(
    budget: BudgetPolicy,
    *,
    stage: str,
    job_spent_units: int,
    requested_units: int,
) -> None:
    """Refuse a stage's integer cost request that breaks a cost ceiling."""

    if requested_units > budget.max_stage_cost_units:
        raise BudgetExceededError(
            "stage_cost_exceeded",
            f"stage '{stage}' requests {requested_units} units above the per-stage "
            f"ceiling {budget.max_stage_cost_units}",
        )
    if job_spent_units + requested_units > budget.max_job_cost_units:
        raise BudgetExceededError(
            "job_cost_exceeded",
            f"stage '{stage}' would push the job to "
            f"{job_spent_units + requested_units} units above the ceiling "
            f"{budget.max_job_cost_units}",
        )


__all__ = [
    "BudgetExceededError",
    "assert_cost_within_ceiling",
    "max_attempts_for",
    "max_attempts_for_error_code",
    "retry_policy_for",
]
