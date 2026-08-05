"""Role matching and geography classification for job titles."""

import re

# ---------------------------------------------------------------------------
# Keyword sets for role cluster detection
# ---------------------------------------------------------------------------

INFRA_KEYWORDS = [
    "devops", "infrastructure", "sre", "site reliability",
    "platform engineer", "platform engineering",
    "kubernetes", "k8s", "docker", "terraform", "ansible", "puppet", "chef",
    # Regression finding 93: bare "aws" / "azure" / "gcp" are recognised
    # via `_WORD_BOUNDARY_KEYWORDS` below so titles like
    # "AWS Specialist", "Backend Engineer (AWS)", "Azure Developer"
    # classify into infra instead of staying unclassified. The suffix
    # forms below remain for clarity of intent but the word-boundary
    # entries are what actually catch the previously-missed 44/95 rows.
    "aws", "azure", "gcp",
    "aws engineer", "azure engineer", "gcp engineer",
    "google cloud", "alibaba cloud", "oracle cloud",
    "linux", "systems engineer", "systems administrator", "sysadmin",
    "network engineer", "network administrator", "network operations",
    "reliability engineer", "release engineer",
    "monitoring", "observability",
    "ci/cd", "cicd", "build engineer", "build and release",
    "finops", "mlops", "dataops", "gitops", "devsecops",
    # Regression finding 92: bare "cloud" matched "Cloud Sales
    # Manager", "Marketing Cloud Architect" etc — 68 FPs (2.8% of
    # infra cluster). The bare noun is removed; these compound
    # forms (plus the "cloud engineer" / "cloud native engineer"
    # below) preserve the legitimate matches. The general
    # sales/marketing guard in `_is_excluded_from_infra()` catches
    # the residual "Cloud Sales Architect" class of titles even
    # when a legit compound (e.g. "cloud architect") is present.
    "cloud architect", "cloud operations", "cloud infrastructure",
    "cloud engineer", "cloud native engineer",
    "infrastructure architect", "infrastructure automation",
    "site reliability", "production engineer",
    "datacenter", "data center",
    "openshift", "openstack",
    "containerization", "container orchestration",
    "configuration management", "infrastructure as code",
    "cloud native", "cloudops",
]

SECURITY_KEYWORDS = [
    # F264 — ``devsecops`` was previously in BOTH this list AND
    # INFRA_KEYWORDS (line 27). Since the role-matcher iterates both
    # lists, the cluster a devsecops job ended up in depended on
    # iteration order — effectively unreliable. Per the F264 ship
    # decision (Option C: move devsecops into the broader infra
    # cluster, keep security as a relevant-but-narrower bucket for
    # pure SOC/GRC/InfoSec roles), the keyword is removed from this
    # list. INFRA_KEYWORDS line 27 retains it. The DB-backed cluster
    # config is also updated in seed_data.py + a one-time prod
    # UPDATE so existing rows reclassify on the next reclassify_and_
    # rescore run.
    "security", "soc", "grc", "pentest",
    "penetration", "incident response", "red team", "offensive",
    "cyber", "infosec", "information security",
    "vulnerability", "threat", "appsec", "application security",
    "cloud security", "network security",
    "identity", "iam", "access management",
    "data protection", "privacy engineer",
    "forensic", "malware", "blue team",
    "security architect", "security operations",
    # Regression finding 91: bare "compliance" matched tax / HR /
    # legal / clinical / pharmaceutical compliance roles — 67 FPs
    # across 1,883 security rows (3.6%). Replaced with explicit
    # security-compliance compound forms. The residual ambiguous
    # titles (e.g. "Compliance Analyst" with no other signal) are
    # suppressed by the `_is_excluded_from_security()` negative
    # filter below when the title also carries tax/legal/hr words.
    "security compliance", "compliance engineer", "compliance analyst",
    "it compliance", "cloud compliance",
    "security analyst",
    # Regression finding 91: bare "risk" family matched financial /
    # operational risk roles. Require a security/cyber/it qualifier.
    "security risk", "cyber risk", "it risk",
    "zero trust", "endpoint security",
    "siem", "soar", "edr", "xdr",
    "threat intelligence", "threat detection", "threat hunting",
    "security automation", "secops",
    "cryptography", "encryption",
    "fraud", "anti-fraud",
    # Regression finding 91: bare "governance" matched product /
    # data governance PM roles; bare "audit" matched financial
    # auditors. Qualified compounds only.
    "data governance", "security governance", "it governance",
    "security audit", "it audit", "cloud audit", "soc audit",
]

QA_KEYWORDS = [
    "qa", "quality assurance", "quality engineering",
    "test engineer", "test engineering", "test automation",
    "sdet", "software development engineer in test",
    "quality engineer", "automated testing", "manual testing",
    "selenium", "cypress", "playwright",
    "test lead", "test manager", "qa engineer", "qa analyst",
    "performance testing", "load testing", "regression testing",
    "test framework", "testops", "test architect",
    "functional testing", "integration testing",
    "test infrastructure", "testing tools",
    "appium", "quality management", "qa manager",
    "software tester", "test coordinator",
]

# ---------------------------------------------------------------------------
# Approved role bases (used for title normalization / exact matching)
# ---------------------------------------------------------------------------

INFRA_ROLES = [
    "DevOps Engineer", "Cloud Engineer", "Infrastructure Engineer",
    "Site Reliability Engineer", "Platform Engineer",
    "Cloud Architect", "Infrastructure Architect",
    "Systems Engineer", "Systems Administrator",
    "Network Engineer", "Network Architect",
    "Release Engineer", "Build Engineer",
    "Production Engineer", "Reliability Engineer",
    "Cloud Operations Engineer", "CloudOps Engineer",
    "Kubernetes Engineer", "Container Engineer",
    "Linux Engineer", "Linux Administrator",
    "Automation Engineer",
    # F264 — DevSecOps belongs with infra/devops/SRE per the cluster
    # reshape decision. It's a build-pipeline-security role, not a
    # SOC/GRC role; the user's team treats it as part of "engineering
    # platform" rather than "security ops".
    "DevSecOps Engineer",
]

SECURITY_ROLES = [
    # F264 — "DevSecOps Engineer" moved to INFRA_ROLES. Pure security
    # roles stay here.
    "Security Engineer", "Cloud Security Engineer",
    "SOC Analyst", "SOC Engineer", "Compliance Analyst", "GRC Analyst",
    "Compliance Engineer", "Incident Response Engineer",
    "Penetration Tester", "Red Team Engineer",
    "Offensive Security Architect",
    "Security Architect", "Security Analyst",
    "Cybersecurity Engineer", "Cybersecurity Analyst",
    "Information Security Engineer", "Information Security Analyst",
    "Application Security Engineer",
    "Network Security Engineer",
    "Threat Analyst", "Threat Engineer",
    "Vulnerability Engineer", "Vulnerability Analyst",
    "Security Operations Engineer",
    "Privacy Engineer", "Privacy Analyst",
    "Risk Analyst", "Risk Engineer",
    "Identity Engineer", "IAM Engineer",
    "Governance Analyst", "Audit Engineer",
    "Fraud Analyst", "Fraud Engineer",
    "Malware Analyst",
]

QA_ROLES = [
    "QA Engineer", "Quality Assurance Engineer", "Quality Engineer",
    "SDET", "Software Development Engineer in Test",
    "Test Automation Engineer", "Automation Test Engineer",
    "QA Analyst", "Quality Analyst",
    "Test Engineer", "Software Test Engineer",
    "QA Lead", "QA Manager", "Test Lead", "Test Manager",
    "Performance Engineer", "Performance Test Engineer",
    "QA Architect", "Test Architect",
    "Software Tester", "Manual Tester",
]

# ---------------------------------------------------------------------------
# Seniority / level keywords
# ---------------------------------------------------------------------------

LEVEL_KEYWORDS = {
    "principal": "Principal",
    "staff": "Staff",
    "lead": "Lead",
    "senior": "Senior",
    "sr.": "Senior",
    "sr ": "Senior",
    "junior": "Junior",
    "jr.": "Junior",
    "jr ": "Junior",
    "manager": "Manager",
    "architect": "Architect",
    "director": "Director",
    "head of": "Head of",
    "vp": "VP",
}

# ---------------------------------------------------------------------------
# Geography classification — comprehensive signal lists
# ---------------------------------------------------------------------------

# Signals that strongly indicate the role is open globally / worldwide
GLOBAL_REMOTE_SIGNALS = [
    "worldwide", "global remote", "remote - anywhere", "remote (anywhere)",
    "work from anywhere", "fully remote", "remote - global",
    "home based - worldwide", "remote global", "anywhere in the world",
    "location independent", "remote - worldwide", "remote worldwide",
    "distributed", "100% remote",
]

# F263 — DEPRECATED. Was promoting any "3+ remote mentions" listing
# to global_remote, which mis-classified region-restricted jobs like
# "France, Remote; Netherlands, Remote; Spain, Remote; UK, Remote" as
# worldwide (feedback 3fc6b5c5 — Dataiku 5973407004 / 5963977004 are
# only available to residents of the four listed EU countries, NOT
# global). Kept as a placeholder regex that matches nothing so any
# remaining ``MULTI_COUNTRY_PATTERN.search`` callsites silently
# become no-ops; the real signal is now extracted from the explicit
# REGION_LOCKED + GLOBAL_REMOTE lists below. Multi-country listings
# without "global"/"worldwide" framing now classify as ``""``
# (unknown bucket) — the honest answer.
MULTI_COUNTRY_PATTERN = re.compile(r"(?!x)x", re.IGNORECASE)  # never matches

# USA-specific signals
USA_SIGNALS = [
    "us only", "usa only", "united states only", "us-based",
    "remote - us", "remote (us)", "remote - united states",
    "remote us", "usa - remote", "us - remote", "us-remote",
    "remote, united states", "remote, usa", "remote (usa)",
    "united states (remote)", "remote, us",
    "united states - remote", "within canada or united states",
    "north america",
    "u.s. only", "u.s. based", "us based",
    "united states or canada", "us or canada",
    "americas - remote", "americas remote",
]

# UAE-specific signals
#
# F350 (UAE sourcing push): expanded beyond the original
# dubai/abu-dhabi/uae set. Matched against location_raw +
# remote_scope only (never the description body), so short tokens
# like "gcc" can't collide with e.g. the GNU compiler in a JD.
UAE_SIGNALS = [
    # Bare "uae" — ATS location fields very often read exactly "UAE" or
    # "UAE, Remote" with no other qualifier. Without this token they
    # matched NONE of the multi-word signals below and fell through to
    # policy=unknown, vanishing from every UAE view (found 16+ live jobs
    # on greenhouse/lever/ashby doing exactly this). Safe as a substring:
    # "uae" doesn't occur inside other city/country names, and
    # UAE_SIGNALS is matched only against location_raw + remote_scope,
    # never the noisy description body.
    "uae",
    "uae only", "uae-based", "emirates", "dubai", "abu dhabi",
    "remote - uae", "remote (uae)",
    "united arab emirates", "uae remote", "remote uae",
    "middle east", "gcc",
    # F350 — remaining emirates + major cities. "ras al khaimah"
    # also appears as the RAK initialism in listings; "rak " with a
    # trailing space would be too fragile, so only the full name.
    "sharjah", "ajman", "ras al khaimah", "fujairah",
    "umm al quwain", "al ain",
    # F350 — free zones / business districts that listings use as
    # the location string instead of a city.
    "difc", "adgm", "jafza", "dubai internet city", "dubai media city",
    "masdar city", "dubai silicon oasis",
    # F350 — phrasing variants the original list missed.
    "based in uae", "based in dubai", "based in abu dhabi",
    "dubai-based", "abu dhabi-based",
    "uae residents", "uae national", "emirati",
]

# Country-specific signals that are NOT global (region-locked remote)
# Jobs matching these get no global_remote classification
REGION_LOCKED_SIGNALS = [
    "remote - uk", "remote uk", "uk - remote", "united kingdom (remote)",
    "uk only", "uk based", "uk-based",
    "remote - canada", "remote canada", "canada (remote)", "canada - remote",
    "canada only", "canada based",
    "remote - india", "remote india", "india (remote)",
    "india only", "india based",
    "remote - germany", "remote germany", "germany (remote)",
    "germany only", "germany based",
    "remote - philippines", "remote philippines",
    "remote - mexico", "remote mexico",
    "remote - ireland", "remote ireland", "ireland (remote)",
    "remote - poland", "remote poland",
    "remote - australia", "remote australia", "australia only",
    "remote - brazil", "remote brazil",
    "remote - sweden", "remote sweden",
    "remote - netherlands", "remote netherlands", "netherlands (remote)",
    "remote - switzerland", "remote switzerland",
    "remote - israel", "remote israel",
    "remote - estonia", "remote estonia",
    "remote - singapore", "singapore (remote)",
    "remote - spain", "remote spain", "spain (remote)",
    "remote - france", "remote france", "france (remote)",
    "remote - japan", "remote japan", "japan (remote)",
    "remote - south korea", "remote - korea",
    "remote - nigeria", "remote nigeria",
    "remote - south africa", "remote south africa",
    "remote - colombia", "remote colombia",
    "remote - argentina", "remote argentina",
    "remote - chile", "remote chile",
    "remote - romania", "remote romania",
    "remote - turkey", "remote turkey",
    "remote - portugal", "remote portugal",
    "remote emea", "remote - emea", "home based - emea",
    "emea only", "emea based",
    "remote - europe", "europe only", "eu only",
    "remote - americas", "americas only",
    "remote - apac", "apac only",
    "remote - latam", "latam only", "latin america only",
]


# F263 — country names for the comma-form region-locked regex below.
# Pre-fix the substring list above caught "remote - france" / "remote
# france" / "france (remote)" but NOT "france, remote" — which is the
# format Greenhouse emits when a listing has multiple country/remote
# pairs separated by semicolons (e.g. Dataiku 5973407004:
# ``"France, Remote; Netherlands, Remote; Spain, Remote; UK, Remote"``).
# The MULTI_COUNTRY_PATTERN heuristic then promoted that to
# ``global_remote`` even though the role is region-restricted to those
# four EU countries (feedback 3fc6b5c5).
#
# Switching to a regex matcher catches every comma-form for the
# countries we already know are region-locked, without forcing us to
# triple-list every variation in REGION_LOCKED_SIGNALS. The matcher
# is conservative: only triggers on a country name immediately
# adjacent to "remote" via a comma, dash, slash, or paren — phrases
# like "We have offices in France and a remote team in Spain" don't
# match. ``\b`` word boundaries prevent "germany" matching inside
# "Germanys" or "South France" matching as "France".
_REGION_COUNTRIES = (
    "uk", "united kingdom", "canada", "india", "germany", "philippines",
    "mexico", "ireland", "poland", "australia", "brazil", "sweden",
    "netherlands", "switzerland", "israel", "estonia", "singapore",
    "spain", "france", "japan", "south korea", "korea", "nigeria",
    "south africa", "colombia", "argentina", "chile", "italy",
    "portugal", "denmark", "norway", "finland", "belgium", "austria",
    "romania", "turkey", "ukraine", "greece", "czech republic",
)
# Matches "<country>, remote" / "<country> - remote" / "<country> (remote)"
# AND the reversed form "remote, <country>" / etc. Used in addition to
# the substring REGION_LOCKED_SIGNALS list so the simple `signal in
# combined` lookup catches the easy cases (cheap path) and the regex
# catches the comma-form variants (broader path).
_REGION_LOCKED_COMMA_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(c) for c in _REGION_COUNTRIES) + r")\s*[,/\-(]\s*remote\b"
    r"|\bremote\s*[,/\-(]\s*(?:" + "|".join(re.escape(c) for c in _REGION_COUNTRIES) + r")\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower().strip())


# Keywords that are short enough to cause substring false positives
# (e.g. "soc" in "associate", "iam" in "claim", "edr" in "Pedro")
# These must match as whole words only.
#
# Regression finding 93: `aws`, `azure`, `gcp` were previously only in
# the compound forms "aws engineer", "azure engineer", "gcp engineer",
# which missed "AWS Specialist" / "AWS Connect Developer" / "Backend
# Engineer (AWS)" / "Azure Developer". Added here so the bare tokens
# match but only as whole words — prevents "laws"/"ocpdraws" FPs.
_WORD_BOUNDARY_KEYWORDS = frozenset([
    "soc", "iam", "grc", "edr", "xdr", "soar", "siem",
    "qa", "sdet",
    "aws", "azure", "gcp",
])


# Regression finding 91: tokens that disqualify a title from the
# `security` cluster even if a security keyword matches. These are
# the overlapping finance/legal/HR/clinical roles that historically
# hit "compliance", "risk", "governance", "privacy", "audit". Order
# is O(N) per match so keep this set small and focused.
_SECURITY_NEGATIVE_TITLE_SIGNALS = frozenset([
    # Finance / accounting
    "tax", "trade compliance", "financial compliance",
    # Legal / regulatory counsel
    "counsel", "attorney", "lawyer", "paralegal",
    "regulatory affairs", "regulatory counsel",
    # Life sciences
    "clinical", "pharmaceutical", "pharmacovigilance", "pharmacy",
    # HR / people ops
    "hr compliance", "people compliance", "labor compliance",
    "human resources", "talent acquisition",
    # F269 — parity with _INFRA_NEGATIVE_TITLE_SIGNALS. The infra
    # cluster excluded sales/marketing titles via F92 + F227, but the
    # security cluster never got the same guard. Manual sweep found
    # 50 sales-titled jobs misclassified as ``security`` (e.g.
    # "SALES TEAM LEADER (DevSecOps)", "Sales Engineer Cloud
    # Security", "Customer Success Manager — Cybersecurity") because
    # the title contains a security keyword AND a sales keyword, but
    # only the infra side guards against the sales pattern. Bringing
    # the lists into parity drops these from the relevant feed.
    "sales", "account executive", "account manager",
    "marketing", "customer success", "business development",
    "partner development", "go-to-market", "go to market",
    "demand generation", "revenue operations",
    "pre-sales", "pre sales", "presales", "solutions consultant",
    # F333 — same teaching / mentor / PM / frontend / risk-analyst
    # additions as infra; security cluster has the same drift class
    # ("Security Instructor", "Cybersecurity Mentor", "Security
    # Project Manager", "Security Compliance Analyst — Frontend").
    # Keep parity with _INFRA_NEGATIVE_TITLE_SIGNALS so future
    # audits can diff the three lists.
    "instructor", "mentor",
    "curriculum developer", "training specialist",
    "project manager", "program manager", "project engineer",
    "frontend", "front-end", "front end developer",
    "risk monitoring", "risk analyst", "risk operations",
    "compliance analyst",
])

# F269 — QA cluster previously had NO negative signal list (only
# infra and security did). 3 sales-titled jobs slipped into the
# qa cluster post-F260 because of QA-keyword-bearing sales titles
# like "Sales Quality Specialist". Adding the same revenue /
# marketing / people-ops guards as the other two clusters. Order
# of inclusion mirrors _INFRA_NEGATIVE_TITLE_SIGNALS so future
# audits can diff the three lists with grep.
_QA_NEGATIVE_TITLE_SIGNALS = frozenset([
    # Revenue / marketing — same as infra/security
    "sales", "account executive", "account manager",
    "marketing", "customer success", "business development",
    "partner development", "go-to-market", "go to market",
    "demand generation", "revenue operations",
    "pre-sales", "pre sales", "presales", "solutions consultant",
    # HR / people ops
    "human resources", "talent acquisition",
    "hr compliance", "people compliance",
    # Finance — "Quality Assurance Analyst, Investment Operations" etc.
    "tax", "fund monitoring", "fund accounting",
    "financial compliance",
    # Manufacturing / hardware QA — these are LEGITIMATE QA roles but
    # for hardware/manufacturing contexts that don't match the
    # software-engineering audience our cluster targets. Leaving the
    # call here for now: software QA is the relevant cluster, hardware
    # QA isn't.
    "manufacturing quality", "hardware quality",
    "supplier quality", "operations quality",
])

# Regression finding 92: tokens that disqualify a title from the
# `infra` cluster. Mostly revenue/marketing org roles whose titles
# happen to contain "cloud" / "systems" / "network". Also: hardware
# and mechanical engineering where the word "systems" overmatches.
#
# Regression finding 227 (extension): the tester's audit of 4,672
# classified jobs found 315 user-visible rows at relevance ≥ 73 that
# would correctly unclassify with an expanded negative list. Common
# FPs: "Fund Monitoring" (finance), "HR Systems Engineer" (people
# ops), "Recruiting Infrastructure Manager" (talent acquisition),
# "UX Designer - Infrastructure" (design). Also: "human resources"
# and "talent acquisition" were only in the security negative list
# (F91) by copy-paste oversight — infra sees the same FPs.
_INFRA_NEGATIVE_TITLE_SIGNALS = frozenset([
    # Revenue / marketing (F92)
    "sales", "account executive", "account manager",
    "marketing", "customer success", "business development",
    "partner development", "go-to-market", "go to market",
    "demand generation", "revenue operations",
    "pre-sales", "pre sales", "presales", "solutions consultant",
    # Hardware / mechanical (F92) — bare "systems engineer" FP class
    "hardware", "mechanical", "electrical", "quality systems",
    "semiconductor", "aerospace", "asic", "embedded hardware",
    # Finance (F227) — "Fund Monitoring - Associate" etc.
    "fund monitoring", "fund accounting",
    # People ops (F227) — parity with _SECURITY_NEGATIVE_TITLE_SIGNALS
    "human resources", "talent acquisition",
    "hr compliance", "people compliance",
    "recruiting infrastructure", "recruiting operations",
    # Design (F227) — "UX Designer - Infrastructure"
    "ux designer", "ui designer", "product designer",
    "visual designer", "graphic designer",
    # F333 — Teaching / mentoring roles. The 2026-05-09 audit
    # found Udacity "Cloud Technical Mentor — Independent
    # Contractor" (score 80) and Correlation One "Lead Instructor:
    # Data Center Technician" (score 73) sitting in the infra
    # cluster. Their JDs are genuinely about Cloud / Data Center
    # tech, but the actual day-to-day work is curriculum +
    # mentorship — not the IC infra/DevOps work the relevance
    # filter is supposed to surface.
    "instructor", "mentor",
    "curriculum developer", "training specialist",
    # F333 — Project management. Audit hit two TensorWave roles:
    # "Data Center Project Engineer" (score 71) and "Data Center
    # Sr. Project Manager" (score 71). Both are construction /
    # commissioning roles, not software infra. ``project manager``
    # is broad enough to catch the variants ("Sr. Project Manager",
    # "Technical Project Manager") and narrow enough that real
    # engineering-management titles ("Engineering Manager,
    # Infrastructure", "Director of Engineering") don't false-
    # positive. Pairs with ``program manager`` for the same class
    # of non-IC role.
    "project manager", "program manager",
    "project engineer",
    # F333 — Frontend roles. Audit hit Marbis "Frontend DevOps
    # Engineer" (score 70) and Dutchie "Staff Platform Engineer —
    # Frontend" (score 57). The keyword scorer fired on
    # ``platform engineer`` / ``devops`` but the actual scope is
    # browser-side. Both forms (``frontend`` and ``front-end``)
    # since job-board titles vary on the hyphen.
    "frontend", "front-end", "front end developer",
    # F333 — Risk / compliance analyst roles. Audit hit Bitfinex
    # "Junior Risk Monitoring Analyst" (score 57) sitting in the
    # infra cluster because the JD mentions "monitoring" and
    # "remote". Genuine risk/compliance analyst work is finance
    # / governance, not infra.
    "risk monitoring", "risk analyst", "risk operations",
    "compliance analyst",
])

# Regression finding 227: short tokens that must match with word
# boundaries to avoid false negatives (e.g. "hr" in "share" /
# "chair" / "thread", "ux" in "luxury" / "xux"). Tested separately
# from the substring-match set because `in` is O(1) per token and
# regex is O(n) per title — keep this set minimal.
_INFRA_NEGATIVE_WORD_BOUNDARY = frozenset([
    "hr",          # "Sr. HR Systems Engineer" but not "share" / "thread"
    "ux",          # "UX Designer" but not "luxury"
    "fund",        # "Fund Monitoring" but not "foundation"
    "recruiter",   # "Recruiter, Infrastructure" (rarely used alone)
    "recruiting",  # "Recruiting Coordinator" (substring-safe at 10 chars
                   # but kept here so related "recruiter" stays grouped)
])


def _keyword_in_text(keyword: str, text: str) -> bool:
    """Check if a keyword matches in text, using word boundaries for short/ambiguous keywords."""
    if keyword in _WORD_BOUNDARY_KEYWORDS:
        return bool(re.search(r'\b' + re.escape(keyword) + r'\b', text))
    return keyword in text


def _title_has_signal(text: str, signals: frozenset[str]) -> bool:
    """Return True if any negative-signal token is present in `text`.

    Uses the same substring-or-word-boundary semantics as
    `_keyword_in_text`: short 2-3 char tokens would be unsafe here,
    so the signal sets only contain ≥4-char tokens and we can use
    plain `in` for the check.
    """
    return any(sig in text for sig in signals)


def _title_has_word_boundary_signal(text: str, signals: frozenset[str]) -> bool:
    """F227: word-boundary variant for short tokens (2-3 chars) that
    would over-match with plain `in`.

    Compiles one regex per call (tiny — typical signal set is <10
    tokens) and tests against the already-lowercased title. Runs in
    addition to `_title_has_signal` for sets that need both kinds
    of matching (see `_is_excluded_from_infra`).
    """
    if not signals:
        return False
    pattern = r"\b(?:" + "|".join(re.escape(s) for s in signals) + r")\b"
    return bool(re.search(pattern, text))


def _is_excluded_from_security(norm_title: str) -> bool:
    """Regression finding 91: disqualify known non-security titles
    that happen to match a security keyword (mostly compliance-like).
    """
    return _title_has_signal(norm_title, _SECURITY_NEGATIVE_TITLE_SIGNALS)


def _is_excluded_from_infra(norm_title: str) -> bool:
    """Regression finding 92: disqualify cloud-sales / hardware
    titles that hit an infra keyword.

    F227 extension: also check word-boundary short tokens (`hr`, `ux`,
    `fund`, etc.) that would produce false negatives if matched via
    substring `in` (e.g. "hr" would fire on "share", "chair",
    "thread"). Both sets are checked in order — substring first
    (cheap `in` per token), regex word-boundary second (one compile
    per call).
    """
    return (
        _title_has_signal(norm_title, _INFRA_NEGATIVE_TITLE_SIGNALS)
        or _title_has_word_boundary_signal(norm_title, _INFRA_NEGATIVE_WORD_BOUNDARY)
    )


def _is_excluded_from_qa(norm_title: str) -> bool:
    """F269 — disqualify sales / marketing / people-ops titles that
    happen to match a QA keyword (e.g. ``Sales Quality Specialist``,
    ``Customer Success Quality Analyst``). Pre-F269 the QA cluster
    had no negative-signal list at all — every sales/marketing role
    bearing a QA keyword landed in the relevant feed. Mirrors the
    infra + security guard pattern.
    """
    return _title_has_signal(norm_title, _QA_NEGATIVE_TITLE_SIGNALS)


def load_cluster_config_sync(session) -> dict:
    """Load role cluster config from DB for use in matching.

    Returns {cluster_name: {"keywords": set, "approved_roles": set}}.
    Falls back to empty dict if no active configs found.
    """
    from app.models.role_config import RoleClusterConfig

    rows = session.execute(
        __import__("sqlalchemy").select(RoleClusterConfig).where(
            RoleClusterConfig.is_active.is_(True)
        )
    ).scalars().all()

    config = {}
    for row in rows:
        keywords = {k.strip().lower() for k in (row.keywords or "").split(",") if k.strip()}
        approved_roles = [r.strip() for r in (row.approved_roles or "").split(",") if r.strip()]
        config[row.name] = {
            "keywords": keywords,
            "approved_roles": approved_roles,
        }
    return config


def get_relevant_clusters_sync(session) -> list[str]:
    """Sync mirror of ``api.v1.jobs._get_relevant_clusters`` for
    use inside Celery tasks (which run sync sessions).

    Returns the list of cluster names where ``is_relevant=True``
    AND ``is_active=True``. Falls back to the hardcoded
    ``["infra", "security"]`` pair if no rows match — same fallback
    contract as the async helper, so async/sync callers see the
    same set.

    F313: auto_target_companies and any future task that wants
    "is this company hiring for RELEVANT roles?" should use this
    helper instead of treating "any non-empty cluster" as relevant.
    Pre-F313 ``auto_target_companies`` over-counted — admins who
    added a non-relevant cluster (like ``data`` for analytics-only
    tracking) had those jobs flip companies to ``is_target=True``
    against intent.
    """
    from app.models.role_config import RoleClusterConfig
    sa = __import__("sqlalchemy")
    rows = session.execute(
        sa.select(RoleClusterConfig.name).where(
            RoleClusterConfig.is_relevant.is_(True),
            RoleClusterConfig.is_active.is_(True),
        )
    ).scalars().all()
    return list(rows) if rows else ["infra", "security"]


def match_role_from_config(title: str, cluster_config: dict) -> dict:
    """Match a job title using DB-driven cluster config.

    Same logic as match_role() but iterates dynamic config instead of hardcoded lists.
    """
    norm = _normalize(title)
    result = {
        "matched_role": "",
        "role_cluster": "",
        "title_normalized": title.strip(),
        "level": "",
    }

    # Detect seniority level
    for kw, level in LEVEL_KEYWORDS.items():
        if kw in norm:
            result["level"] = level
            break

    # Regression findings 91 + 92 (extended by F269): apply the same
    # cluster-specific negative-signal guards here. The config system
    # uses the cluster name verbatim, so ``infra``, ``security``, and
    # ``qa`` get their tailored negative lists; any admin-added
    # cluster (e.g. ``data_science``) passes through unguarded because
    # we have no FP profile for it.
    #
    # F269 — added the ``qa`` guard. Pre-fix, QA was alone in having
    # NO negative list, so 3 sales-titled jobs ended up tagged as qa
    # in prod. ``_is_excluded_from_qa`` now mirrors the infra/security
    # guards' shape.
    excluded_from_security = _is_excluded_from_security(norm)
    excluded_from_infra = _is_excluded_from_infra(norm)
    excluded_from_qa = _is_excluded_from_qa(norm)

    def _skip_cluster(cluster_name: str) -> bool:
        if cluster_name == "infra":
            return excluded_from_infra
        if cluster_name == "security":
            return excluded_from_security
        if cluster_name == "qa":
            return excluded_from_qa
        return False

    # Try exact-ish matching against approved roles from each cluster
    for cluster_name, cfg in cluster_config.items():
        if _skip_cluster(cluster_name):
            continue
        for role in cfg["approved_roles"]:
            if _normalize(role) in norm:
                result["matched_role"] = role
                result["role_cluster"] = cluster_name
                if result["level"]:
                    result["title_normalized"] = f"{result['level']} {role}"
                else:
                    result["title_normalized"] = role
                return result

    # Fallback: keyword-based cluster detection
    for cluster_name, cfg in cluster_config.items():
        if _skip_cluster(cluster_name):
            continue
        for kw in cfg["keywords"]:
            if _keyword_in_text(kw, norm):
                result["role_cluster"] = cluster_name
                result["matched_role"] = title.strip()
                return result

    return result


def match_role_with_config(title: str, cluster_config: dict | None = None) -> dict:
    """Match a role using DB config with a hardcoded-list safety net.

    F235 regression fix — the DB ``RoleClusterConfig`` rows are seeded
    once and drift as the hardcoded ``INFRA_KEYWORDS`` /
    ``SECURITY_KEYWORDS`` lists get refined each regression round
    (F91, F92, F93, F227, …). Pre-fix, ``match_role_from_config`` was
    effectively a strict SUBSET of ``match_role``, so every
    ``reclassify_and_rescore`` run under-classified hundreds of titles
    that the hardcoded matcher would have caught (tester observed
    315 stale infra + 440 stale security rows after a full sweep).

    Strategy:

    1. Run the config-driven matcher first. If it lands on a
       non-empty cluster, trust it — admins explicitly configured
       the cluster and any custom clusters (e.g. "data_science")
       only exist on that side.
    2. If the config-driven matcher came back empty *and* the config
       has an active cluster for infra/security/qa, fall back to
       the hardcoded matcher for that same built-in cluster. The
       fallback only fires when (a) config has the cluster defined
       and (b) config matcher produced no answer, so we can never
       resurrect a cluster the admin explicitly disabled.
    3. If no config is supplied, run the hardcoded matcher directly
       (the legacy single-matcher path used by callers that haven't
       loaded the cluster config yet).

    Net effect: config wins when it has an opinion; the hardcoded
    superset catches what the DB config narrows off; disabled clusters
    are still respected.
    """
    if not cluster_config:
        return match_role(title)

    config_result = match_role_from_config(title, cluster_config)
    if config_result["role_cluster"]:
        return config_result

    # No config-side match — try the hardcoded superset, but only for
    # built-in clusters the admin hasn't disabled in the DB.
    hardcoded = match_role(title)
    hc_cluster = hardcoded["role_cluster"]
    if hc_cluster and hc_cluster in cluster_config:
        return hardcoded

    # Either no hardcoded match either, or the hardcoded match lives
    # in a cluster the admin removed/disabled — return the (empty)
    # config result so the admin's disable decision wins.
    return config_result


def match_role(title: str) -> dict:
    """Match a job title against approved roles.

    Returns dict with:
        matched_role: str   - best matching approved role or empty
        role_cluster: str   - 'infra' | 'security' | ''
        title_normalized: str - cleaned title
        level: str          - detected seniority level
    """
    norm = _normalize(title)
    result = {
        "matched_role": "",
        "role_cluster": "",
        "title_normalized": title.strip(),
        "level": "",
    }

    # Detect seniority level
    for kw, level in LEVEL_KEYWORDS.items():
        if kw in norm:
            result["level"] = level
            break

    # Regression findings 91 + 92 + F269: check negative-signal guards
    # once per title. The approved-role loop below short-circuits on
    # the first match, so we apply the same guards there too — e.g.
    # a "Tax Compliance Analyst" title matches the approved role
    # "Compliance Analyst" and would otherwise land in security; a
    # "Sales Quality Analyst" matches "Quality Analyst" and would
    # otherwise land in qa (F269 fix).
    excluded_from_security = _is_excluded_from_security(norm)
    excluded_from_infra = _is_excluded_from_infra(norm)
    excluded_from_qa = _is_excluded_from_qa(norm)

    # Try exact-ish matching against approved role bases
    for role in INFRA_ROLES:
        if _normalize(role) in norm:
            if excluded_from_infra:
                # A cloud-sales title shouldn't match "Cloud Architect" even
                # if the substring is present. Continue to subsequent
                # clusters (unlikely to match) and finally fall through
                # to unclassified.
                break
            result["matched_role"] = role
            result["role_cluster"] = "infra"
            if result["level"]:
                result["title_normalized"] = f"{result['level']} {role}"
            else:
                result["title_normalized"] = role
            return result

    for role in SECURITY_ROLES:
        if _normalize(role) in norm:
            if excluded_from_security:
                break
            result["matched_role"] = role
            result["role_cluster"] = "security"
            if result["level"]:
                result["title_normalized"] = f"{result['level']} {role}"
            else:
                result["title_normalized"] = role
            return result

    for role in QA_ROLES:
        if _normalize(role) in norm:
            # F269 — same exclusion semantics as infra/security: bail
            # if the title carries a sales/marketing/people-ops
            # negative signal.
            if excluded_from_qa:
                break
            result["matched_role"] = role
            result["role_cluster"] = "qa"
            if result["level"]:
                result["title_normalized"] = f"{result['level']} {role}"
            else:
                result["title_normalized"] = role
            return result

    # Fallback: keyword-based cluster detection
    if not excluded_from_infra:
        for kw in INFRA_KEYWORDS:
            if _keyword_in_text(kw, norm):
                result["role_cluster"] = "infra"
                result["matched_role"] = title.strip()
                return result

    if not excluded_from_security:
        for kw in SECURITY_KEYWORDS:
            if _keyword_in_text(kw, norm):
                result["role_cluster"] = "security"
                result["matched_role"] = title.strip()
                return result

    if not excluded_from_qa:
        for kw in QA_KEYWORDS:
            if _keyword_in_text(kw, norm):
                result["role_cluster"] = "qa"
                result["matched_role"] = title.strip()
                return result

    return result


def classify_geography(location_raw: str, remote_scope: str) -> str:
    """Classify a job into a geography bucket based on location and remote scope text.

    Returns one of: 'global_remote', 'usa_only', 'uae_only', or '' (unknown).
    """
    combined = _normalize(f"{location_raw} {remote_scope}")

    # Check region-locked FIRST — these are NOT global even though they say "remote"
    for signal in REGION_LOCKED_SIGNALS:
        if signal in combined:
            return ""  # Region-locked, not classifiable into our buckets

    # F263 — comma-form region-locked check. Catches Greenhouse-style
    # listings like "France, Remote; Netherlands, Remote; …" that the
    # substring list above misses (it has "remote - france" but not
    # "france, remote"). If ANY country in the listing is region-
    # locked, the whole job is treated as not-globally-remote — better
    # to surface "unclassified" than to falsely promise global.
    if _REGION_LOCKED_COMMA_RE.search(combined):
        return ""

    # Check global remote signals
    for signal in GLOBAL_REMOTE_SIGNALS:
        if signal in combined:
            return "global_remote"

    # F263 — MULTI_COUNTRY_PATTERN was previously a heuristic that
    # promoted "3+ remote mentions" to global_remote. It mis-classified
    # region-restricted multi-country jobs (feedback 3fc6b5c5). Now a
    # no-op regex that never matches; if every country in a multi-
    # country listing is region-locked, the regex above already
    # returned "" — anything that gets here is genuinely ambiguous and
    # we'd rather classify as unknown than falsely as global.
    if MULTI_COUNTRY_PATTERN.search(combined):
        return "global_remote"

    # USA signals
    for signal in USA_SIGNALS:
        if signal in combined:
            return "usa_only"

    # UAE signals
    for signal in UAE_SIGNALS:
        if signal in combined:
            return "uae_only"

    # Heuristic: bare "remote" with NO country/region → treat as potentially global
    # This catches "Remote" with remote_scope="remote" but no location qualifier
    if remote_scope and "remote" in remote_scope.lower():
        loc = _normalize(location_raw)
        # If location is just "Remote" or empty, it's likely global
        if not loc or loc == "remote" or loc == "fully remote" or loc == "remote, remote":
            return "global_remote"

    # Additional heuristic: location_raw itself is a global-sounding phrase
    loc_lower = _normalize(location_raw)
    if loc_lower in (
        "anywhere", "worldwide", "global", "remote",
        "anywhere in the world", "work from anywhere",
        "fully remote", "100% remote", "remote - anywhere",
    ):
        return "global_remote"

    # Location contains "united states" or "usa" without "remote" prefix — likely USA
    if any(sig in loc_lower for sig in ("united states", "usa", "u.s.")):
        return "usa_only"

    return ""


# ---------------------------------------------------------------------------
# Remote-policy classification (d0e1f2g3h4i5)
#
# Replacement for ``classify_geography`` with a richer return shape:
# ``(policy, countries)`` where ``policy`` is one of
# ``worldwide`` | ``country_restricted`` | ``region_restricted`` |
# ``hybrid`` | ``onsite`` | ``unknown`` and ``countries`` carries the
# ISO-3166 alpha-2 codes for the ``country_restricted`` case.
#
# See ``app/utils/remote_policy.py`` for the canonical definitions.
# The legacy ``classify_geography`` is kept for backward compatibility
# during the transition window — both functions are called by the
# scan + maintenance tasks so the legacy ``geography_bucket`` column
# stays in sync via shadow-write.
# ---------------------------------------------------------------------------

# On-site signals — full office presence, no remote work.
ONSITE_SIGNALS = (
    "on-site only", "onsite only", "on site only",
    "no remote", "in-office only", "in office only",
    "fully on-site", "fully onsite", "fully on site",
    "must be on-site", "must be onsite",
    "100% on-site", "100% onsite",
)

# Hybrid signals — mix of office + remote.
HYBRID_SIGNALS = (
    "hybrid",
    "hybrid remote",
    "hybrid - ",
    "hybrid (",
    "days a week in",
    "days/week in",
    "in office",  # weak — only used after stronger signals miss
    "flexible work arrangement",
    "flex remote",
)

# Region-restricted signals — broader than a single country, narrower
# than worldwide. These overlap with REGION_LOCKED_SIGNALS but are
# specifically the *region-named* ones (EU, EMEA, APAC, LATAM…) — a
# job that names a single country goes into ``country_restricted``
# with a populated countries list.
REGION_RESTRICTED_SIGNALS = (
    "emea only", "emea based", "remote emea", "remote - emea",
    "home based - emea",
    "europe only", "eu only", "remote - europe",
    "apac only", "remote - apac",
    "latam only", "latin america only", "remote - latam",
    "americas only", "remote - americas",
    "north america",  # treats as regional rather than country_restricted
    "middle east", "gcc",
)

# Country-name → ISO-3166 alpha-2 mapping for the existing
# REGION_LOCKED_SIGNALS recognition. Drives the
# ``country_restricted`` countries list when the classifier sees a
# single-country region-locked signal. Future iteration: parse multi-
# country listings ("US or Canada", "remote, US/CA") into a list.
_COUNTRY_NAME_TO_ISO: dict[str, str] = {
    "united states": "US", "usa": "US", "u.s.": "US", "us": "US",
    "united arab emirates": "AE", "uae": "AE",
    "united kingdom": "GB", "uk": "GB",
    "canada": "CA",
    "india": "IN",
    "germany": "DE",
    "philippines": "PH",
    "mexico": "MX",
    "ireland": "IE",
    "poland": "PL",
    "australia": "AU",
    "brazil": "BR",
    "sweden": "SE",
    "netherlands": "NL",
    "switzerland": "CH",
    "israel": "IL",
    "estonia": "EE",
    "singapore": "SG",
    "spain": "ES",
    "france": "FR",
    "japan": "JP",
    "south korea": "KR", "korea": "KR",
    "nigeria": "NG",
    "south africa": "ZA",
    "colombia": "CO",
    "argentina": "AR",
    "chile": "CL",
    "romania": "RO",
    "turkey": "TR",
    "portugal": "PT",
}


def _detect_country_codes(combined: str) -> list[str]:
    """Best-effort ISO alpha-2 country detection for a job's location.

    Mirrors the country-restricted precedence used inside
    ``classify_remote_policy`` (region-locked signals → USA → UAE) but
    factored out so the **hybrid** branch can reuse it. A hybrid job
    that also names a country ("Hybrid - Dubai", "Hybrid (UAE-based)")
    should keep that country: without it the UAE hybrid roles a UAE
    sourcing push depends on were classified as bare ``hybrid`` with
    an empty country list and vanished from every UAE view
    (``?remote_policy=hybrid&remote_country=AE`` returned nothing).

    Returns a single-element list (first match wins, matching the
    country_restricted convention) or ``[]`` when no country is named.
    """
    for signal in REGION_LOCKED_SIGNALS:
        if signal in combined:
            for country_name, iso in _COUNTRY_NAME_TO_ISO.items():
                if country_name in signal:
                    return [iso]
    for signal in USA_SIGNALS:
        if signal in combined:
            return ["US"]
    for signal in UAE_SIGNALS:
        if signal in combined:
            return ["AE"]
    return []


def classify_remote_policy(
    location_raw: str, remote_scope: str
) -> tuple[str, list[str]]:
    """Classify a job into the ``remote_policy`` enum + country list.

    Returns ``(policy, countries)`` where ``policy`` ∈
    ``{"worldwide","country_restricted","region_restricted","hybrid",
    "onsite","unknown"}`` and ``countries`` is a list of ISO alpha-2
    codes. Populated for ``country_restricted`` and — since the UAE
    sourcing push — for ``hybrid`` when the location names a country
    ("Hybrid - Dubai" → ("hybrid", ["AE"])). Empty for worldwide /
    region_restricted / onsite / unknown and for bare hybrid.

    Order of detection (first match wins):

      1. **On-site** — explicit "no remote" markers. Beats hybrid
         because "on-site only" sometimes co-occurs with "hybrid"
         in scraped descriptions.
      2. **Hybrid** — explicit hybrid markers. Keeps any country the
         location names (so UAE hybrid roles stay filterable).
      3. **Region restricted** — region-named signals (EMEA, APAC,
         "Europe only", etc.). Stays as policy=region_restricted with
         empty countries list — we don't try to enumerate the region.
      4. **Country restricted** — single country mentioned via the
         legacy region-locked list, mapped to ISO codes.
      5. **Worldwide** — global remote signals from the legacy list.
      6. **Country restricted (US/UAE special)** — fallback for
         "remote - us" / "us only" / "remote - uae" patterns the
         legacy classifier already recognises.
      7. **Unknown** — everything else.

    Note: this function does NOT shadow-write ``geography_bucket`` —
    that's the caller's job (scan_task / maintenance_task), which
    derives the legacy bucket via ``legacy_bucket_for(policy,
    countries)`` so there's only one source of truth.
    """
    combined = _normalize(f"{location_raw} {remote_scope}")
    loc_lower = _normalize(location_raw)

    # 1. On-site — explicit hard-no-remote signals beat everything.
    for signal in ONSITE_SIGNALS:
        if signal in combined:
            return "onsite", []

    # 2. Hybrid — explicit "hybrid" wording. Skip when the strong
    # remote signals are present (rare but happens in noisy scrapes).
    # Unlike the pre-F350-followup behaviour, a hybrid job keeps any
    # country it names ("Hybrid - Dubai" → ("hybrid", ["AE"])) so UAE
    # hybrid roles are findable via ?remote_policy=hybrid&
    # remote_country=AE. Bare hybrid (no country) still returns [].
    has_strong_remote = any(
        sig in combined for sig in ("100% remote", "fully remote", "remote - anywhere")
    )
    if not has_strong_remote:
        for signal in HYBRID_SIGNALS:
            if signal in combined:
                return "hybrid", _detect_country_codes(combined)

    # 3. Region-restricted — region-named signals (EMEA / APAC / etc).
    for signal in REGION_RESTRICTED_SIGNALS:
        if signal in combined:
            return "region_restricted", []

    # 4. Country-restricted via the legacy single-country signals. We
    # walk the region-locked list and try to extract the country
    # name; the ISO map below converts to alpha-2.
    for signal in REGION_LOCKED_SIGNALS:
        if signal in combined:
            for country_name, iso in _COUNTRY_NAME_TO_ISO.items():
                if country_name in signal:
                    return "country_restricted", [iso]
            # Region-locked signal we didn't have an ISO mapping for
            # — fall back to region_restricted rather than dropping
            # to unknown (we know it's not worldwide).
            return "region_restricted", []

    # 5. Worldwide — explicit global signals.
    for signal in GLOBAL_REMOTE_SIGNALS:
        if signal in combined:
            return "worldwide", []

    # 6. Country-restricted special cases — legacy USA/UAE detection.
    for signal in USA_SIGNALS:
        if signal in combined:
            return "country_restricted", ["US"]
    for signal in UAE_SIGNALS:
        if signal in combined:
            return "country_restricted", ["AE"]

    # 7. Bare "remote" + empty/remote location → worldwide
    if remote_scope and "remote" in remote_scope.lower():
        if not loc_lower or loc_lower in ("remote", "fully remote", "remote, remote"):
            return "worldwide", []
    if loc_lower in (
        "anywhere", "worldwide", "global", "remote",
        "anywhere in the world", "work from anywhere",
        "fully remote", "100% remote", "remote - anywhere",
    ):
        return "worldwide", []

    # USA fallback (location text without "remote" prefix).
    if any(sig in loc_lower for sig in ("united states", "usa", "u.s.")):
        return "country_restricted", ["US"]

    return "unknown", []


# Strong hybrid signals we trust from the job DESCRIPTION body. Kept
# separate from — and stricter than — HYBRID_SIGNALS because prose is
# far noisier than the short location/scope fields: we require
# unambiguous multi-word phrases and deliberately DROP weak tokens like
# a bare "hybrid" or "in office" that would false-match "...this is NOT
# a hybrid role, we're fully remote..." in a paragraph. Used only by
# refine_uae_hybrid (UAE-scoped), never by the base classifier.
DESCRIPTION_HYBRID_SIGNALS = (
    "hybrid working", "hybrid work model", "hybrid work arrangement",
    "hybrid working model", "hybrid model", "hybrid role",
    "hybrid position", "hybrid setup", "hybrid schedule",
    "hybrid arrangement", "work model: hybrid", "working model: hybrid",
    # "workplace"/"work environment"/"work culture" unambiguously refer
    # to the work SETUP — safe to include. We still exclude bare
    # "hybrid", which in UAE tech listings overwhelmingly means "hybrid
    # cloud / multicloud / infrastructure" or a "#li-hybrid" LinkedIn
    # tag, not a hybrid work arrangement (verified against prod bodies).
    "hybrid workplace", "hybrid work environment", "hybrid work culture",
    "days a week in office", "days a week in the office",
    "days per week in office", "days per week in the office",
    "days/week in office", "days in the office each week",
)


def refine_uae_hybrid(
    policy: str, countries: list[str], description: str
) -> tuple[str, list[str]]:
    """UAE-only: upgrade a UAE country-restricted job to ``hybrid`` when
    its DESCRIPTION carries a strong hybrid signal.

    Why this exists: UAE ATS listings almost never put "hybrid" in the
    ``location_raw`` / ``remote_scope`` fields the base classifier reads
    — the location is just "Dubai, United Arab Emirates" — so a
    genuinely-hybrid Dubai role is tagged ``country_restricted`` (UAE
    remote). The hybrid signal lives in the body. This refinement reads
    the description for those jobs and re-tags them ``hybrid`` while
    keeping ``["AE"]`` so they're findable via
    ``?remote_policy=hybrid&remote_country=AE``.

    Scoped deliberately (product decision — UAE sourcing push):
      * **AE only** — ``countries == ["AE"]`` — so US/UK/other
        country counts are untouched.
      * **country_restricted only** — never downgrades a truly
        worldwide-remote or already-hybrid posting.

    A no-op (returns the inputs unchanged) for every non-UAE job and
    for UAE jobs whose body has no strong hybrid phrase.
    """
    if (
        policy == "country_restricted"
        and list(countries) == ["AE"]
        and description
    ):
        body = _normalize(description)
        if any(sig in body for sig in DESCRIPTION_HYBRID_SIGNALS):
            return "hybrid", ["AE"]
    return policy, list(countries)
