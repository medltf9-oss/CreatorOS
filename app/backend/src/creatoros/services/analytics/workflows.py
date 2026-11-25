"""W1-W3: the three analytics workflows, built as deterministic, reviewed task chains.

The chief orchestrator can plan any objective from scratch with a model. A growth review must not:
it has to run the same reviewed chain every time, or the numbers a human reads depend on whatever the
model felt like delegating that day. These builders create the objective's tasks and their dependency
edges up front, so `Orchestrator.execute()` runs the documented graph instead of asking a model to
invent one.

Nothing here can publish. Every stage is analysis, reporting or a plan; the only write any analytics
agent can reach is a *draft* recommendation, and no builder imports a publishing path.

The chain *shape* lives in code (reviewable, testable). The wording lives in `prompt_template` rows
under `analytics.*`, seeded once by `ensure_analytics_prompts()` and editable afterwards without a
code change; the constants here are the fallback when a database has not been seeded yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from ...agents.catalog import MEASUREMENT_RULES
from ...core.logging import get_logger
from ...db.models import PromptTemplate

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ...db.models import AgentObjective, AgentTask

log = get_logger(__name__)

CHIEF_AGENT_ID = "chief_orchestrator"
ANALYTICS_DEPARTMENT = "analytics_growth"

#: Every analytics stage runs under this task type, which `TASK_TOOL_HINTS` maps to the read tools.
#: It is deliberately *not* `department_plan`: the engine auto-expands that task type into extra
#: model-chosen specialist work, which would break a deterministic chain.
ANALYTICS_TASK_TYPE = "analytics"
REVIEW_TASK_TYPE = "review"
COMPILE_TASK_TYPE = "final_compilation"

MEASUREMENT_RULES_KEY = "analytics.measurement_rules"
WORKFLOW_PROMPT_PREFIX = "analytics.workflow."


@dataclass(frozen=True, slots=True)
class Stage:
    """One task in a workflow: who runs it, what it answers, and what it depends on."""

    key: str
    agent_id: str
    task_type: str
    title: str
    step: str
    depends_on: tuple[str, ...] = ()
    depth: int = 2
    priority: int = 5


@dataclass(frozen=True, slots=True)
class Workflow:
    key: str
    title: str
    objective: str
    stages: tuple[Stage, ...]

    def agent_ids(self) -> list[str]:
        return [stage.agent_id for stage in self.stages]


def weekly_growth_review() -> Workflow:
    """W1 - weekly growth review: plan, eight analyses in parallel, report, direction, compile."""
    plan = Stage(
        "plan",
        "analytics_director",
        ANALYTICS_TASK_TYPE,
        "Weekly growth review plan",
        "Plan the weekly growth review: restate the period, name the metrics in scope, and say what "
        "each analysis must answer. Use only metrics the tool results show.",
        (),
        1,
        7,
    )
    analyses = (
        Stage(
            "performance",
            "performance_analysis_agent",
            ANALYTICS_TASK_TYPE,
            "Overall performance",
            "Summarise measured overall performance for the period. Name each metric, its window and "
            "its availability, and say plainly what is missing.",
            ("plan",),
            2,
            6,
        ),
        Stage(
            "views",
            "views_analysis_agent",
            ANALYTICS_TASK_TYPE,
            "Views analysis",
            "Explain where views came from using the observed view metrics only; do not infer reach "
            "from views.",
            ("plan",),
            2,
            5,
        ),
        Stage(
            "engagement",
            "engagement_analysis_agent",
            ANALYTICS_TASK_TYPE,
            "Engagement analysis",
            "Interpret likes, saves, shares and comments against the content's intent, normalised by "
            "views where the data allows.",
            ("plan",),
            2,
            5,
        ),
        Stage(
            "retention",
            "retention_analysis_agent",
            ANALYTICS_TASK_TYPE,
            "Retention analysis",
            "Explain where viewers dropped off, or state clearly that the retention curve is not "
            "available for this platform.",
            ("plan",),
            2,
            5,
        ),
        Stage(
            "audience_growth",
            "audience_growth_agent",
            ANALYTICS_TASK_TYPE,
            "Audience growth",
            "Report follower change for the period and its likely driver, without overclaiming cause.",
            ("plan",),
            2,
            5,
        ),
        Stage(
            "hashtags",
            "hashtag_performance_agent",
            ANALYTICS_TASK_TYPE,
            "Hashtag performance",
            "Evaluate hashtag outcomes where data exists, and say when it does not. Never attribute "
            "reach to hashtags alone.",
            ("plan",),
            2,
            5,
        ),
        Stage(
            "campaign",
            "campaign_analysis_agent",
            ANALYTICS_TASK_TYPE,
            "Campaign analysis",
            "Evaluate any campaign against its stated success condition, or say there is none.",
            ("plan",),
            2,
            5,
        ),
        Stage(
            "comparison",
            "content_comparison_agent",
            ANALYTICS_TASK_TYPE,
            "Content comparison",
            "Compare the best and worst measured publications on explicit, controlled dimensions.",
            ("plan",),
            2,
            5,
        ),
    )
    report = Stage(
        "report",
        "growth_reporting_agent",
        ANALYTICS_TASK_TYPE,
        "Growth report",
        "Write the growth report: measured results first, interpretation clearly marked as "
        "interpretation, and the next experiments to run.",
        tuple(stage.key for stage in analyses),
        2,
        4,
    )
    strategy = Stage(
        "strategy",
        "strategy_director",
        REVIEW_TASK_TYPE,
        "Direction and trade-offs",
        "State the direction and trade-offs the measured results imply. Do not schedule or publish "
        "anything.",
        ("report",),
        1,
        3,
    )
    compile_stage = Stage(
        "compile",
        CHIEF_AGENT_ID,
        COMPILE_TASK_TYPE,
        "Compile the weekly review",
        "Compile the weekly growth review from the analyses, the report and the strategy fit. State "
        "honestly what could not be measured.",
        ("report", "strategy"),
        0,
        2,
    )
    return Workflow(
        key="weekly_growth_review",
        title="Weekly growth review",
        objective="Weekly performance review",
        stages=(plan, *analyses, report, strategy, compile_stage),
    )


def single_publication_diagnosis() -> Workflow:
    """W2 - one publication: performance, then retention and engagement and comparison, then a verdict."""
    performance = Stage(
        "performance",
        "performance_analysis_agent",
        ANALYTICS_TASK_TYPE,
        "Publication performance",
        "Explain this publication's measured performance. Name the metrics, windows and availability, "
        "and say what is missing.",
        (),
        1,
        7,
    )
    retention = Stage(
        "retention",
        "retention_analysis_agent",
        ANALYTICS_TASK_TYPE,
        "Retention diagnosis",
        "Explain where viewers dropped off in this publication, or state that the retention curve is "
        "not available.",
        ("performance",),
        2,
        5,
    )
    engagement = Stage(
        "engagement",
        "engagement_analysis_agent",
        ANALYTICS_TASK_TYPE,
        "Engagement diagnosis",
        "Interpret this publication's engagement against its intent; compare like with like.",
        ("performance",),
        2,
        5,
    )
    comparison = Stage(
        "comparison",
        "content_comparison_agent",
        ANALYTICS_TASK_TYPE,
        "Content comparison",
        "Compare this publication with the most similar measured publications, keeping the comparison "
        "dimensions explicit.",
        ("performance",),
        2,
        5,
    )
    verdict = Stage(
        "verdict",
        "analytics_director",
        ANALYTICS_TASK_TYPE,
        "Diagnosis verdict",
        "Give the diagnosis verdict and one concrete change to make next, citing the measurements "
        "you were given.",
        ("retention", "engagement", "comparison"),
        1,
        3,
    )
    return Workflow(
        key="single_publication_diagnosis",
        title="Single-publication diagnosis",
        objective="Explain one publication's performance",
        stages=(performance, retention, engagement, comparison, verdict),
    )


def experiment_design() -> Workflow:
    """W3 - experiment design: one variable, a pre-declared metric, a stop rule, then two reviews."""
    design = Stage(
        "design",
        "ab_testing_agent",
        ANALYTICS_TASK_TYPE,
        "Experiment design",
        "Design one experiment: change one variable, declare the success metric before the test, "
        "state the sample requirement honestly and give the stop rule. Output is a draft, never a "
        "schedule.",
        (),
        2,
        7,
    )
    review = Stage(
        "review",
        "analytics_director",
        REVIEW_TASK_TYPE,
        "Experiment review",
        "Review the experiment design for feasibility and measurement validity; refuse one that "
        "cannot be measured.",
        ("design",),
        1,
        5,
    )
    fit = Stage(
        "fit",
        "strategy_director",
        REVIEW_TASK_TYPE,
        "Strategy fit check",
        "Check whether the experiment fits the current direction. Do not schedule or publish "
        "anything.",
        ("review",),
        1,
        3,
    )
    return Workflow(
        key="experiment_design",
        title="Experiment design",
        objective="Design one measurable growth experiment",
        stages=(design, review, fit),
    )


WORKFLOWS: dict[str, Workflow] = {
    "weekly_growth_review": weekly_growth_review(),
    "single_publication_diagnosis": single_publication_diagnosis(),
    "experiment_design": experiment_design(),
}


# --------------------------------------------------------------------- prompt seeds


def _prompt_exists(session: Session, key: str, version: int = 1) -> bool:
    return bool(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(PromptTemplate)
            .where(PromptTemplate.key == key, PromptTemplate.version == version)
        )
    )


def ensure_analytics_prompts(session: Session) -> dict[str, Any]:
    """Seed the analytics prompt templates once. Insert-only: a human edit is never overwritten."""
    created: list[str] = []
    seeds: dict[str, dict[str, Any]] = {
        MEASUREMENT_RULES_KEY: {"kind": "rules"},
    }
    for key, workflow in WORKFLOWS.items():
        seeds[f"{WORKFLOW_PROMPT_PREFIX}{key}"] = {
            "kind": "workflow",
            "objective": workflow.objective,
            "stages": [stage.key for stage in workflow.stages],
            "agents": workflow.agent_ids(),
        }
    for key, schema in seeds.items():
        if _prompt_exists(session, key):
            continue
        body = MEASUREMENT_RULES if key == MEASUREMENT_RULES_KEY else WORKFLOWS[key.split(".")[-1]].objective
        session.add(PromptTemplate(key=key, version=1, body=body, schema_json=schema, active=True))
        created.append(key)
    if created:
        session.flush()
        log.info("analytics prompt seeds created: %s", created)
    return {"created": created}


def measurement_rules(session: Session) -> str:
    """The stored measurement rules, so a human edit wins; the constant is the fallback."""
    body = session.scalar(
        sa.select(PromptTemplate.body)
        .where(PromptTemplate.key == MEASUREMENT_RULES_KEY, PromptTemplate.active.is_(True))
        .order_by(PromptTemplate.version.desc())
        .limit(1)
    )
    return str(body or MEASUREMENT_RULES)


# --------------------------------------------------------------------- builders


def _context_line(context: dict[str, Any]) -> str:
    parts = [f"{key}={value}" for key, value in context.items() if value not in (None, "")]
    return "Context: " + ", ".join(parts) if parts else ""


def create_workflow(
    session: Session,
    *,
    key: str,
    account_id: str,
    objective: str | None = None,
    project_id: str | None = None,
    metric_key: str | None = None,
    window_kind: str | None = None,
    period: str | None = None,
    publication_id: str | None = None,
    budgets: Any | None = None,
    enqueue: bool = False,
) -> AgentObjective:
    """Create an objective whose task graph *is* the workflow, ready for `Orchestrator.execute`."""
    from ..orchestrator import Orchestrator

    workflow = WORKFLOWS.get(key)
    if workflow is None:
        raise ValueError(f"unknown analytics workflow {key!r}; known: {sorted(WORKFLOWS)}")
    if not account_id:
        raise ValueError("an analytics workflow needs an account_id")

    orchestrator = Orchestrator(session)
    text = objective or f"{workflow.objective} for account {account_id}"
    row = orchestrator.create_objective(text, project_id=project_id, budgets=budgets, enqueue=enqueue)

    rules = measurement_rules(session)
    context = {
        "account": account_id,
        "period": period,
        "publication": publication_id,
        "metric": metric_key,
        "window": window_kind,
    }
    context_line = _context_line(context)
    created: dict[str, AgentTask] = {}
    for stage in workflow.stages:
        definition = orchestrator.registry.get(stage.agent_id)
        payload: dict[str, Any] = {
            "objective": row.objective,
            "project_id": project_id,
            "account_id": account_id,
            "step": "\n\n".join(part for part in (rules, context_line, stage.step) if part),
        }
        if metric_key:
            payload["metric_key"] = metric_key
        if window_kind:
            payload["window_kind"] = window_kind
        if publication_id:
            payload["publication_id"] = publication_id
        depend_on = [created[dependency].id for dependency in stage.depends_on]
        created[stage.key] = orchestrator.add_task(
            row,
            agent_id=stage.agent_id,
            department=definition.team_id,
            task_type=stage.task_type,
            title=stage.title,
            depth=stage.depth,
            priority=stage.priority,
            input_payload=payload,
            depend_on=depend_on,
        )

    row.plan_json = {
        "workflow": key,
        "title": workflow.title,
        "account_id": account_id,
        "stages": [stage.key for stage in workflow.stages],
        "agents": workflow.agent_ids(),
        "deterministic": True,
    }
    row.departments_json = [
        {"department": department, "state": "pending"}
        for department in sorted({created[stage.key].team_id for stage in workflow.stages})
    ]
    session.commit()
    return row


__all__ = [
    "ANALYTICS_TASK_TYPE",
    "CHIEF_AGENT_ID",
    "MEASUREMENT_RULES_KEY",
    "WORKFLOWS",
    "Stage",
    "Workflow",
    "create_workflow",
    "ensure_analytics_prompts",
    "experiment_design",
    "measurement_rules",
    "single_publication_diagnosis",
    "weekly_growth_review",
]
