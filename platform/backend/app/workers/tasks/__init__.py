"""Celery task registry -- import all tasks so autodiscovery picks them up."""

from app.workers.tasks.scan_task import scan_all_platforms, scan_single_company
from app.workers.tasks.career_page_task import check_career_pages
from app.workers.tasks.discovery_task import run_discovery
from app.workers.tasks.maintenance_task import (
    expire_stale_jobs, rescore_jobs,
    reclassify_and_rescore, auto_target_companies, fix_stuck_enrichments,
)
from app.workers.tasks.enrichment_task import enrich_company, enrich_target_companies_batch, verify_stale_emails
from app.workers.tasks.resume_score_task import score_resume_task
from app.workers.tasks.feedback_task import process_review_feedback_task, decay_scoring_signals
from app.workers.tasks.question_collection_task import collect_questions
# F356 — these three were beat-scheduled in celery_app.py but never
# imported here, so their @celery_app.task decorators never ran and
# the worker rejected every firing with "Received unregistered task"
# (KeyError). Silent for months: weekly AI insights produced nothing
# (empty /insights), nightly backups didn't run, and funding-followup
# probes never fired. autodiscover_tasks(["app.workers.tasks"]) looks
# for a non-existent ``app.workers.tasks.tasks`` module, so
# registration depends ENTIRELY on the explicit imports in this file —
# a module absent here is a task that silently never runs.
from app.workers.tasks.ai_insights_task import run_weekly_insights
from app.workers.tasks.backup_task import run_backup
from app.workers.tasks.funding_followup_task import auto_probe_recent_funding

__all__ = [
    "scan_all_platforms",
    "scan_single_company",
    "check_career_pages",
    "run_discovery",
    "expire_stale_jobs",
    "rescore_jobs",
    "enrich_company",
    "score_resume_task",
    "process_review_feedback_task",
    "decay_scoring_signals",
    "collect_questions",
    "enrich_target_companies_batch",
    "verify_stale_emails",
    "reclassify_and_rescore",
    "auto_target_companies",
    "fix_stuck_enrichments",
    "run_weekly_insights",
    "run_backup",
    "auto_probe_recent_funding",
]
