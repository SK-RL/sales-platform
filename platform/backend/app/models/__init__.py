from app.models.user import User
from app.models.company import Company, CompanyATSBoard
from app.models.job import Job, JobDescription
from app.models.review import Review
from app.models.pipeline import PotentialClient
from app.models.scan import ScanLog, CareerPageWatch
from app.models.rule import RoleRule
from app.models.discovery import DiscoveryRun, DiscoveredCompany
from app.models.resume import Resume, ResumeScore, AICustomizationLog
from app.models.role_config import RoleClusterConfig
from app.models.platform_credential import PlatformCredential
from app.models.answer_book import AnswerBookEntry
from app.models.application import Application
from app.models.scoring_signal import ScoringSignal
from app.models.job_question import JobQuestion
from app.models.company_contact import CompanyContact, JobContactRelevance
from app.models.company_office import CompanyOffice
from app.models.feedback import Feedback
from app.models.pipeline_stage import PipelineStage
from app.models.audit_log import AuditLog
from app.models.insight import UserInsight, ProductInsight
from app.models.training_example import TrainingExample
from app.models.saved_filter import SavedFilter
from app.models.user_notice import UserNotice

# F373 — every table module must be imported here. The Celery worker
# loads models lazily per task, and SQLAlchemy's unit-of-work sorts
# tables by foreign key at flush time across the WHOLE metadata: with
# `applications.routine_run_id -> routine_runs.id` present but
# `routine_runs` never imported, the first flush in
# submit_application_task raised NoReferencedTableError on production
# — before any browser was opened. No server-side submission had ever
# run. The API never saw it because routers import these modules
# themselves. tests/test_f373_worker_model_registry.py pins this list.
from app.models import (  # noqa: F401
    alert,
    application_submission,
    humanization_corpus,
    interview_question,
    profile,
    routine_kill_switch,
    routine_run,
    routine_target,
    work_time,
)

__all__ = [
    "User", "Company", "CompanyATSBoard", "Job", "JobDescription",
    "Review", "PotentialClient", "ScanLog", "CareerPageWatch",
    "RoleRule", "DiscoveryRun", "DiscoveredCompany",
    "Resume", "ResumeScore", "AICustomizationLog", "RoleClusterConfig",
    "PlatformCredential", "AnswerBookEntry", "Application",
    "ScoringSignal", "JobQuestion",
    "CompanyContact", "JobContactRelevance", "CompanyOffice",
    "Feedback",
    "PipelineStage",
    "AuditLog",
    "UserInsight", "ProductInsight",
    "TrainingExample",
    "SavedFilter",
    "UserNotice",
]
