"""Central API router that includes all v1 sub-routers."""

from fastapi import APIRouter
from app.api.v1 import (
    auth, jobs, reviews, pipeline, analytics,
    companies, career_pages, discovery, rules, export, platforms, monitoring,
    resume, users, role_config, credentials, answer_book, applications, feedback,
    alerts, cover_letter, interview_prep, intelligence, audit, ai, insights,
    training_data, saved_filters, profiles, routine, work_window,
    interview_questions, notices,
)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router)
api_router.include_router(jobs.router)
api_router.include_router(reviews.router)
api_router.include_router(pipeline.router)
api_router.include_router(analytics.router)
api_router.include_router(companies.router)
api_router.include_router(career_pages.router)
api_router.include_router(discovery.router)
api_router.include_router(rules.router)
api_router.include_router(export.router)
api_router.include_router(platforms.router)
api_router.include_router(monitoring.router)
api_router.include_router(resume.router)
api_router.include_router(users.router)
api_router.include_router(role_config.router)
api_router.include_router(credentials.router)
api_router.include_router(answer_book.router)
api_router.include_router(applications.router)
api_router.include_router(feedback.router)
api_router.include_router(alerts.router)
api_router.include_router(cover_letter.router)
api_router.include_router(interview_prep.router)
api_router.include_router(intelligence.router)
api_router.include_router(audit.router)
# F236: cross-cutting AI utility endpoints (usage snapshot etc).
api_router.include_router(ai.router)
# F237: AI Intelligence endpoints — user insights + admin product insights.
api_router.include_router(insights.router)
# F238: training-data capture pipeline (admin-only export + stats + backfill).
api_router.include_router(training_data.router)
# F241: per-user saved filter presets.
api_router.include_router(saved_filters.router)
# KYC profile vault — admin/superadmin only. Stores personal HR
# documents (Aadhaar, PAN, marksheets, bank/PF) with role-gated
# access + audit trail.
api_router.include_router(profiles.router)
# v6 Claude Routine Apply — browser-automation orchestration endpoints
# (top-to-apply, runs CRUD, kill-switch, humanize helper). The routine
# runs as the caller; no admin-gated routes here.
api_router.include_router(routine.router)
# Work-time window enforcement: admin-set per-user IST shifts, one-off
# overrides, and a reviewer-driven extension-request workflow. The
# enforcement itself lives in ``api.deps.get_current_user`` — this
# router is purely the control plane.
api_router.include_router(work_window.router)
# Interview question repository (ticket 8ef0e9c2) — shared debriefs
# of client interview rounds so future candidates can prepare from
# real data. All-users read/create; author-or-admin edit/delete.
api_router.include_router(interview_questions.router)
# In-app login notices — per-user dismissible banners (admin->user
# comms; first use: "re-upload your lost documents" after 650514ad).
api_router.include_router(notices.router)
