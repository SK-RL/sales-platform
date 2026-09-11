# CLAUDE.md - Sales Data Scrape Global Remote Cloud

## Project Overview
Full-stack job aggregation and sales intelligence platform. Automatically scrapes ATS boards, classifies roles into configurable clusters, scores relevance, provides ATS resume scoring with AI-powered customization, and supports sales team workflows.

## Architecture
- **Backend**: FastAPI (async) + SQLAlchemy (async for API, sync for Celery) + PostgreSQL
- **Workers**: Celery + Redis for background scanning, scoring, and discovery tasks
- **Frontend**: React 18 + TypeScript + TanStack Query + Tailwind CSS + Recharts
- **Deployment**: Docker Compose (6 services: postgres, redis, backend, celery-worker, celery-beat, frontend)

## Directory Structure
```
platform/
  backend/
    app/
      api/v1/              # FastAPI routers
        auth.py            # Login, register, password reset
        users.py           # Admin user management
        jobs.py            # Job listing, filtering, bulk actions
        reviews.py         # Job review queue
        companies.py       # Company browsing and scoring
        platforms.py       # ATS board management, scan controls
        resume.py          # Resume upload, ATS scoring, AI customization
        pipeline.py        # Sales pipeline tracking
        analytics.py       # Dashboard analytics
        monitoring.py      # System health (admin)
        role_config.py     # Configurable role clusters (admin)
        discovery.py       # Platform discovery
        career_pages.py    # Career page scraping
        export.py          # Data export
        rules.py           # Filtering rules
      models/              # SQLAlchemy models
        job.py             # Job, CompanyATSBoard
        company.py         # Company
        user.py            # User (with password reset fields)
        resume.py          # Resume, ResumeScore, ResumeCustomization
        review.py          # Review
        pipeline.py        # PipelineEntry
        role_config.py     # RoleClusterConfig
        scan.py            # ScanLog
        discovery.py       # DiscoveryResult
        rule.py            # FilterRule
      schemas/             # Pydantic request/response schemas
      fetchers/            # ATS board fetchers (10 platforms)
        greenhouse.py      # Greenhouse API
        lever.py           # Lever API
        ashby.py           # Ashby GraphQL API
        workable.py        # Workable API
        bamboohr.py        # BambooHR API
        himalayas.py       # Himalayas REST API
        wellfound.py       # Wellfound GraphQL API
        jobvite.py         # Jobvite REST API
        smartrecruiters.py # SmartRecruiters REST API
        recruitee.py       # Recruitee REST API
        career_page.py     # Generic career page scraper
        base.py            # BaseFetcher abstract class
      workers/
        celery_app.py      # Celery configuration
        tasks/
          scan_task.py     # Full, per-platform, and per-board scanning
          discovery_task.py # Auto-discover and add new ATS boards
          _scoring.py      # Multi-signal relevance scoring (0-100)
          _role_matching.py # Role cluster classification
          _ats_scoring.py  # Resume-to-job ATS scoring
          _ai_resume.py    # Claude API resume customization
          _resume_parser.py # PDF/DOCX text extraction
          _db.py           # Sync DB session for Celery
          enrichment_task.py
          maintenance_task.py
          career_page_task.py
      config.py            # Settings from environment variables
      database.py          # Async DB engine + session factory
      main.py              # FastAPI app entrypoint
      seed_admin.py        # Create initial admin user
      seed_data.py         # Seed demo data
      seed_remote_companies.py # Seed known remote-friendly companies
    alembic/               # Database migrations
    requirements.txt
    Dockerfile
  frontend/
    src/
      pages/               # React page components
        DashboardPage.tsx
        JobsPage.tsx
        JobDetailPage.tsx
        ReviewQueuePage.tsx
        CompaniesPage.tsx
        PlatformsPage.tsx
        ResumeScorePage.tsx
        PipelinePage.tsx
        AnalyticsPage.tsx
        MonitoringPage.tsx    # Admin: system health + scan controls
        UserManagementPage.tsx # Admin: user CRUD
        RoleClustersPage.tsx  # Admin: configurable role clusters
        SettingsPage.tsx      # User settings + password change
        LoginPage.tsx
      components/          # Reusable UI components
        Layout.tsx, Sidebar.tsx, Card.tsx, Badge.tsx,
        Button.tsx, Table.tsx, Pagination.tsx,
        ScoreBar.tsx, StatusBadge.tsx
      lib/
        api.ts             # API client (all endpoints)
        types.ts           # TypeScript interfaces
        auth.tsx           # Auth context + ProtectedRoute
      App.tsx              # Route definitions
      main.tsx             # React entrypoint
      index.css            # Tailwind + custom styles
    package.json
    Dockerfile
  docker-compose.yml
  .env / .env.example
```

## Key Concepts

### Role Clusters (Configurable)
Jobs are classified into role clusters managed via the admin UI (`/role-clusters`).
Default clusters: **infra** (DevOps, Cloud, SRE, Infrastructure) and **security** (Security, DevSecOps, SOC, Compliance, Pentest).
Admins can add/remove clusters, toggle which are "relevant", and define matching keywords + approved role titles.
`_get_relevant_clusters(db)` dynamically reads config with fallback to `["infra", "security"]`.

### Platform Fetchers (10 Platforms)
- **Tier 1** (highest source score): Greenhouse, Lever, Ashby, Workable, BambooHR
- **Tier 2**: SmartRecruiters, Jobvite, Recruitee, Wellfound
- **Tier 3**: Himalayas
All fetchers extend `BaseFetcher` and implement `fetch_jobs(slug) -> list[dict]`.

### Geography Classification
- `global_remote`: Worldwide/anywhere remote
- `usa_only`: US-restricted remote
- `uae_only`: UAE-restricted remote

### Relevance Scoring (0-100)
Weighted: 40% title match, 20% company fit, 20% geography clarity, 10% source priority, 10% freshness.

### ATS Resume Scoring
Users upload resumes (PDF/DOCX), text extracted, scored against ALL relevant jobs:
- 50% keyword overlap, 30% role alignment, 20% format/completeness
AI customization (Claude API) rewrites resume for target score. Restricted to relevant jobs only.

### Auth & Roles
- JWT in httpOnly cookie
- Four roles (hierarchy in `app/api/deps.py` `ROLE_HIERARCHY`): `super_admin` > `admin` > `reviewer` > `viewer`
  - `super_admin`: full platform control — user management (`/users` CRUD, password resets, role assignment), everything `admin` can do
  - `admin`: monitoring, role clusters, feedback management, scan controls, view all resumes, sales performance
  - `reviewer`: job review queue, manage own pipeline
  - `viewer`: read-only
  - The seeded `admin@jobplatform.io` account has role `admin`, NOT `super_admin`. User-management endpoints require `super_admin` specifically and will 403 for `admin`. `GET /api/v1/users/roles` is the authoritative source for the current role catalog.
- `require_role("admin")` dependency for admin-only endpoints; hierarchy means higher roles pass lower-role guards automatically
- 403 responses use the generic message `"Insufficient privileges for this action"` — do NOT name the required role in the detail string (F185: leaking the role gives attackers a precise privilege-escalation target)
- Password: SHA-256 + salt, with change/reset flows

### Scan Controls (Admin)
- Full scan (all platforms), per-platform scan, per-board scan
- Discovery scan: probes slugs across platforms, auto-creates Company + Board records
- All scans are async Celery tasks with status polling

## Backups, disk, and the VM (read `docs/BACKUPS.md` first)
- One backup stream: nightly Celery `run_backup` at 03:00 UTC, kept to the last 3 (`BACKUP_KEEP_LAST=3`), in the `sales-platform_backups` docker volume. Pre-deploy `pg_dump`s were retired on 2026-09-11 after they filled the disk; do not add a per-deploy dump.
- Off-VM copy: a launchd job on Sarthak's laptop pulls the newest complete backup every Sunday 10:00 local into the gitignored `db-backups/` (keeps 3). Script `platform/scripts/pull-backup-to-laptop.sh`, installer `platform/scripts/install-laptop-backup-job.sh`.
- VM access from the laptop: `ssh -i ~/.ssh/Sarthak-Betaque -o IdentitiesOnly=yes ubuntu@161.118.207.119`. The VM's `/opt/sales-platform/scripts/ci-deploy.sh` is NOT shipped by deploys — copy it by hand (with a `.bak`) after changing it.
- Worker visibility: `GET /api/v1/monitoring/celery` (admin) shows workers, active tasks and queue lengths; `POST /api/v1/monitoring/celery/revoke/{task_id}` stops a runaway task. The VM's `.env` pins `CELERY_CONCURRENCY=2`.
- This checkout is shared by several Claude sessions that switch branches. Commit or stash before switching, and never rely on an uncommitted file surviving.

## Running
```bash
cd platform
docker compose build
docker compose up -d
docker compose exec backend alembic upgrade head
docker compose exec backend python -m app.seed_admin
docker compose exec backend python -m app.seed_remote_companies
```

## Environment Variables (.env)
- `DATABASE_URL` - PostgreSQL connection string
- `REDIS_URL` - Redis connection string
- `JWT_SECRET` - JWT signing secret
- `ANTHROPIC_API_KEY` - For AI resume customization (optional)
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` - OAuth (optional)

## API Conventions
- Pagination: `{ items, total, page, page_size, total_pages }`
- Auth: JWT in cookie, `get_current_user` dependency
- Admin-only: `require_role("admin")` dependency
- File uploads: multipart/form-data (resume upload)
- Errors: `{ detail: string }` with appropriate HTTP status

## Frontend Conventions
- All API calls through `lib/api.ts` using `request<T>()` helper
- State management via TanStack Query (`useQuery` / `useMutation`)
- Types in `lib/types.ts` mirroring backend schemas
- Routing in `App.tsx`, navigation in `components/Sidebar.tsx`
- Admin pages gated by `user?.role === "admin"` in Sidebar
