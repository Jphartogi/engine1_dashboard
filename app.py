"""
Engine 1 Command Center
Deal Execution Tracker & Strategy Dashboard for Engine 1, covering three PODS
(PODS 1, PODS 2, PODS 3) in one shared app.

Multi-POD data isolation: every opportunity, task, and per-team target belongs
to exactly one POD, and every pod-scoped role (admin, account_manager,
management, solution, project, product) only ever sees their own POD's data.
The engine1_exec role belongs to no single POD - it gets a read-only view
across all three PODS combined (or drilled into one), and is the only role
that can edit the Engine-1-wide Stages/Pillars taxonomy shared by all PODS.

Row-level access control within a POD: each Account Manager can only edit the
opportunities assigned to them; that POD's admin can edit everything in it;
that POD's management is read-only within it.
"""
import io
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, date, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, render_template, request, send_file
from werkzeug.security import check_password_hash, generate_password_hash

# Semantic version (MAJOR.MINOR.PATCH) for this deployment - bump on every
# feature/fix and record it in CHANGELOG.md, so "which version is live" is
# always answerable from the UI (bottom of the nav rail) or GET /api/version.
APP_VERSION = "1.3.0"

# Keep the database next to app.py so it persists in a predictable location
# regardless of the host's working directory (Render, PythonAnywhere, Docker, etc.).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "db.sqlite3"))

app = Flask(__name__)

# In-memory token store: token -> {user_id, username, role, full_name}
TOKENS = {}

VALID_ROLES = ("admin", "pod_head", "account_manager", "management", "solution", "project", "product",
               "engine1_exec", "super_admin")
# Cross-functional roles that can only edit the Team Tasks section of an opportunity
# (never TCV, the execution framework, or any other sales-owned field).
CROSS_FUNCTIONAL_ROLES = ("solution", "project", "product")
# Every other role belongs to exactly one POD and only ever sees that POD's data.
# engine1_exec and super_admin belong to none of them (pod is NULL): engine1_exec gets a
# read-only view across all three plus exclusive ownership of the Engine-1-wide Stages/
# Pillars taxonomy; super_admin gets full read/write everywhere in any POD, and is the
# only role that can create/edit users of any role (including other super_admin or
# engine1_exec accounts) in any POD.
PODLESS_ROLES = ("engine1_exec", "super_admin")
PODS = ("pods1", "pods2", "pods3")
POD_LABELS = {"pods1": "PODS 1", "pods2": "PODS 2", "pods3": "PODS 3"}
TASK_STATUSES = ("not_started", "in_progress", "blocked", "needs_discussion", "done")
VALID_STAGES = ("Prospecting", "Negotiation", "Closed", "Blocked")

DEFAULT_PILLARS = [
    "IoT Connectivity",
    "Device Bundling",
    "CCTV & Vision Analytics",
    "Enterprise Solutions",
    "Digital Reward",
]
DEFAULT_SQUADS = ["Volume Squad", "Tender Squad", "Strategic Squad"]
DEFAULT_STAGES = ["Prospecting", "Negotiation", "Closed", "Blocked"]

# --------------------------------------------------------------------------
# Execution Framework - the 8 Enterprise Proofs (PODS 2 execution standard)
# --------------------------------------------------------------------------
PROOFS = [
    ("qualification", "Proof of Qualification",
     "Ensure target customers (B500) have the need, budget, and alignment with our solution"),
    ("engagement", "Proof of Engagement",
     "Secure deep engagement with all key customer stakeholders (PODS, SPIN selling, Sales card)"),
    ("concept", "Proof of Concept",
     "Verify the proposed solution functions exactly as specified (TQC)"),
    ("value", "Proof of Value",
     "Confirm the solution delivers a profitable outcome for the customer (TQC & outcome-based solution)"),
    ("contract", "Proof of Contract",
     "Ensure all TCLRF terms are covered (Technical, Commercial, Legal, Risk, Framework)"),
    ("delivery", "Proof of Delivery",
     "Ensure the final solution is delivered according to all expectations (TQC)"),
    ("operation", "Proof of Operation",
     "Commit on agreed-upon Service Level Agreement (SLA) and ensure payment"),
    ("clm", "Proof of CLM",
     "Nurture the customer relationship to prevent churn and enable upselling (Customer card)"),
]
PROOF_KEYS = [p[0] for p in PROOFS]
PROOF_NAMES = {p[0]: p[1] for p in PROOFS}
PROOF_STATUSES = ("not_started", "in_progress", "done", "na")
ENTRY_STATUSES = ("not_started", "planned", "in_progress", "done")
ENTRY_STATUS_LABELS = {
    "not_started": "Not started",
    "planned": "Planned",
    "in_progress": "In progress",
    "done": "Done",
}


def normalize_proofs(raw):
    """Always return the full 8-proof structure, preserving whatever was stored.

    Each proof holds a list of evidence `entries` ({text, date, end, status}).
    `date` is the start date; `end` is optional — a blank end means a single-day
    activity. A legacy single `notes` string is migrated into the first entry so
    nothing is ever lost. Entries without their own status fall back to the
    parent proof's status (an "na" proof falls back to "not_started") so older
    data keeps working.
    """
    incoming = raw if isinstance(raw, dict) else {}
    out = {}
    for key in PROOF_KEYS:
        item = incoming.get(key) or {}
        if not isinstance(item, dict):
            item = {}
        status = str(item.get("status", "not_started")).strip().lower().replace(" ", "_")
        if status not in PROOF_STATUSES:
            status = "not_started"
        fallback_entry_status = status if status in ENTRY_STATUSES else "not_started"

        entries = []
        for e in (item.get("entries") or []):
            if isinstance(e, dict):
                text = str(e.get("text", "") or "").strip()
                date = str(e.get("date", "") or "").strip()[:10]
                end = str(e.get("end", "") or "").strip()[:10]
                if end and date and end < date:
                    end = ""  # guard against an inverted range
                estatus = str(e.get("status", "") or "").strip().lower().replace(" ", "_")
                if estatus not in ENTRY_STATUSES:
                    estatus = fallback_entry_status
            else:
                text, date, end, estatus = str(e or "").strip(), "", "", fallback_entry_status
            if text:
                entries.append({"text": text, "date": date, "end": end, "status": estatus})

        # Migrate a legacy notes string into the evidence list.
        legacy = str(item.get("notes", "") or "").strip()
        if legacy and not any(e["text"] == legacy for e in entries):
            entries.insert(0, {"text": legacy, "date": "", "end": "", "status": fallback_entry_status})

        out[key] = {
            "status": status,
            "due": str(item.get("due", "") or "")[:10],
            "entries": entries,
            # kept for backward compatibility with older clients/exports
            "notes": entries[0]["text"] if entries else "",
        }
    return out


def proof_progress(proofs):
    """% of applicable (non-N/A) proofs completed."""
    p = normalize_proofs(proofs)
    applicable = [v for v in p.values() if v["status"] != "na"]
    if not applicable:
        return 0
    done = sum(1 for v in applicable if v["status"] == "done")
    return round(done / len(applicable) * 100)


# Seed Account Managers: (username, password, full_name)
SEED_AMS = [
    ("anisa", "anisa123", "Anisa Rahmy"),
    ("arie", "arie123", "Arie Prabowo"),
    ("ashari", "ashari123", "Ashari"),
    ("dimas", "dimas123", "Dimas"),
]

# Example per-AM revenue targets for H2 2026 (admin can edit in Settings).
DEFAULT_AM_TARGETS = {
    "Anisa Rahmy": 15_000_000_000,
    "Arie Prabowo": 13_000_000_000,
    "Ashari": 14_000_000_000,
    "Dimas": 15_000_000_000,
}


def _hash(pw):
    return generate_password_hash(pw, method="pbkdf2:sha256")


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Seed opportunity dataset for PODS 1 (the team's real pipeline, carried over
# from the original single-POD PODS 2 tracker this app was built from).
# Column order: deal_name, customer, assigned_am, squad, strategic_pillar,
#               estimated_value, target_quarter, stage, progress,
#               is_blocked, blocker_description, next_actions(JSON)
def _seed_deals():
    def na(items):
        return json.dumps([{"action": a, "done": d} for a, d in items])

    rows = [
        # ---------------- Anisa Rahmy ----------------
        ("Simcard IoT for DAOP (12 DAOP)", "PT Kereta Api Indonesia", "Anisa Rahmy",
         "Tender Squad", "IoT Connectivity", 600_000_000, "Q3 2026", "Negotiation", 40,
         0, "", na([("Confirm DAOP rollout scope", True), ("Finalize pricing", False)])),
        ("MDM – MS Connectivity + IoT (450 unit)", "PT Samsung Electronic Indonesia", "Anisa Rahmy",
         "Volume Squad", "Device Bundling", 2_400_000_000, "Q3 2026", "Negotiation", 50,
         0, "", na([("Align MDM spec", True), ("Commercial proposal", False)])),
        ("Tender IP Transit & Metronet Link 2", "PT Kereta Api Indonesia", "Anisa Rahmy",
         "Tender Squad", "Enterprise Solutions", 1_680_000_000, "Q3 2026", "Negotiation", 45,
         0, "", na([("Prepare tender docs", True), ("Target RFS Q3 2026", False)])),
        ("IoT Simcard – Wifi Kereta New Generation (450 unit)", "PT Kereta Api Indonesia", "Anisa Rahmy",
         "Volume Squad", "IoT Connectivity", 2_400_000_000, "Q3 2026", "Negotiation", 55,
         0, "", na([("Confirm August implementation", True), ("SIM provisioning plan", False)])),
        ("Simcard IoT for ADAS", "PT Trans Jakarta", "Anisa Rahmy",
         "Tender Squad", "IoT Connectivity", 1_250_000_000, "Q3 2026", "Prospecting", 30,
         0, "", na([("ADAS integration scoping", False), ("Target RFS Q3 2026", False)])),
        ("Connectivity HO", "GMF", "Anisa Rahmy",
         "Strategic Squad", "IoT Connectivity", 2_400_000_000, "Q3 2026", "Prospecting", 25,
         0, "", na([("HO connectivity survey", False)])),
        ("Fuel Management System", "PT Kereta Api Indonesia", "Anisa Rahmy",
         "Strategic Squad", "Enterprise Solutions", 4_000_000_000, "Q4 2026", "Prospecting", 20,
         0, "", na([("Complete POC (in progress)", False), ("2026 revenue plan", False)])),
        ("CCTV HO (Pengadaan Langsung)", "PT Trans Jakarta", "Anisa Rahmy",
         "Tender Squad", "CCTV & Vision Analytics", 200_000_000, "Q3 2026", "Prospecting", 30,
         0, "", na([("Prepare pengadaan langsung docs", False)])),

        # ---------------- Arie Prabowo ----------------
        ("Device Bundling Samsung A17 EE", "Glico Indonesia", "Arie Prabowo",
         "Volume Squad", "Device Bundling", 256_000_000, "Q3 2026", "Prospecting", 30,
         0, "", na([("Maintain fixed partner price", False), ("Confirm delivery timeline", False)])),
        ("Smart Asset Rental", "YIMM", "Arie Prabowo",
         "Strategic Squad", "Enterprise Solutions", 80_800_000, "Q3 2026", "Prospecting", 25,
         0, "", na([("Scope asset rental", False)])),
        ("Device Bundling Samsung A17 EE", "Karoseri Laksana", "Arie Prabowo",
         "Volume Squad", "Device Bundling", 1_300_000_000, "Q3 2026", "Prospecting", 35,
         0, "", na([("Lock partner price for large qty", False), ("Delivery schedule", False)])),
        ("Enterprise Asset Management", "Kobelindo Compressors", "Arie Prabowo",
         "Strategic Squad", "Enterprise Solutions", 385_000_000, "Q3 2026", "Prospecting", 30,
         0, "", na([("EAM requirement workshop", False)])),
        ("People Tracking Solution", "Triputra Agro Persada", "Arie Prabowo",
         "Strategic Squad", "Enterprise Solutions", 2_400_000_000, "Q4 2026", "Prospecting", 20,
         0, "", na([("Assign dedicated PIC", False)])),
        ("CCTV AI Crowd Analytics", "YIMM", "Arie Prabowo",
         "Strategic Squad", "CCTV & Vision Analytics", 180_000_000, "Q3 2026", "Prospecting", 30,
         0, "", na([("Frequent customer visit", False)])),
        ("CCTV Portable", "YIMM", "Arie Prabowo",
         "Volume Squad", "CCTV & Vision Analytics", 1_100_000_000, "Q3 2026", "Prospecting", 25,
         0, "", na([("Manage customer expectations", False)])),
        ("Smart Vehicle Monitoring System", "Triputra Agro Persada", "Arie Prabowo",
         "Strategic Squad", "Enterprise Solutions", 4_900_000_000, "Q4 2026", "Prospecting", 15,
         0, "", na([("Assign skilled implementation team", False)])),
        ("ESTA Vision", "Waresix", "Arie Prabowo",
         "Strategic Squad", "CCTV & Vision Analytics", 248_000_000, "Q3 2026", "Prospecting", 30,
         0, "", na([("ESTA vision demo", False)])),
        ("IoT Connectivity (Consortium)", "Consortium Mastrans Bandung", "Arie Prabowo",
         "Tender Squad", "IoT Connectivity", 1_200_000_000, "Q4 2026", "Prospecting", 20,
         0, "", na([("Consortium alignment", False)])),

        # ---------------- Ashari ----------------
        ("Strategic Account – AnterAja", "Tri Adi Bersama (AnterAja)", "Ashari",
         "Strategic Squad", "Enterprise Solutions", 7_300_000_000, "H2 2026", "Negotiation", 35,
         0, "", na([("Account plan", True), ("Solution proposal", False)])),
        ("Strategic Account – Mayora", "Cipta Niaga Semesta (Mayora Group)", "Ashari",
         "Strategic Squad", "Enterprise Solutions", 540_000_000, "H2 2026", "Prospecting", 25,
         0, "", na([("Discovery meeting", False)])),
        ("Strategic Account – IMP", "Integrasi Multi Persada (IMP)", "Ashari",
         "Strategic Squad", "Enterprise Solutions", 4_200_000_000, "H2 2026", "Prospecting", 25,
         0, "", na([("Needs assessment", False)])),
        ("Strategic Account – Cikarang Listrindo", "PT Cikarang Listrindo, Tbk.", "Ashari",
         "Strategic Squad", "Enterprise Solutions", 1_000_000_000, "H2 2026", "Prospecting", 20,
         0, "", na([("Intro meeting", False)])),
        ("Strategic Account – Indo Lysaght", "PT Indo Lysaght", "Ashari",
         "Strategic Squad", "Enterprise Solutions", 360_000_000, "H2 2026", "Prospecting", 20,
         0, "", na([("Qualify opportunity", False)])),

        # ---------------- Dimas ----------------
        ("Device Bundling – 2250 Drivers", "PT Green SM", "Dimas",
         "Volume Squad", "Device Bundling", 8_100_000_000, "Q3-Q4 2026", "Negotiation", 40,
         0, "", na([("Confirm 2250 unit rollout", True), ("Delivery scheduling", False)])),
        ("M2M IoT Simcard – Green SM Bike (10k)", "PT Green SM", "Dimas",
         "Volume Squad", "IoT Connectivity", 990_000_000, "Q3 2026", "Negotiation", 50,
         0, "", na([("August implementation plan", True), ("SIM activation", False)])),
        ("M2M IoT Simcard – Green SM EVEE Car (6k)", "PT Green SM", "Dimas",
         "Volume Squad", "IoT Connectivity", 691_000_000, "Q3 2026", "Negotiation", 45,
         0, "", na([("EVEE fleet onboarding", False)])),
        ("Device Bundling – Dexa & Ferron Pharma", "PT Dexa Group", "Dimas",
         "Volume Squad", "Device Bundling", 1_600_000_000, "Q3-Q4 2026", "Prospecting", 30,
         0, "", na([("Employee bundling scope", False)])),
        ("Digital Reward – 60TB", "PT Via Yotta Byte", "Dimas",
         "Strategic Squad", "Digital Reward", 1_900_000_000, "Q3 2026", "Negotiation", 40,
         0, "", na([("Q3 implementation", False)])),
        ("CCTV Analytics – 3 MOR Stores (30 titik)", "PT OT Group", "Dimas",
         "Strategic Squad", "CCTV & Vision Analytics", 500_000_000, "Q3 2026", "Prospecting", 30,
         0, "", na([("Site survey 3 lokasi", False), ("Installation plan 30 titik", False)])),
        ("Smart Water AI Inspection – Crystalin", "PT OT Group", "Dimas",
         "Strategic Squad", "CCTV & Vision Analytics", 332_000_000, "Q3 2026", "Prospecting", 25,
         0, "", na([("Label & coding inspection PoC", False)])),
        ("Smart Building Energy Saving", "PT Kawanlama Group", "Dimas",
         "Strategic Squad", "Enterprise Solutions", 400_000_000, "Q3 2026", "Prospecting", 25,
         0, "", na([("Energy saving assessment", False)])),
    ]
    return rows


def _widen_user_roles(db):
    """Older databases have a narrower CHECK(role IN (...)) / no `pod` column on users.
    SQLite can't ALTER a CHECK constraint in place, so when the constraint or columns are
    out of date for the current role/POD model we rebuild the table - copying every row
    across unchanged - rather than touching any data."""
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not row or not row["sql"] or "'super_admin'" in row["sql"]:
        return  # table doesn't exist yet, or already migrated
    has_pod = "pod" in {r[1] for r in db.execute("PRAGMA table_info(users)")}
    db.executescript(
        f"""
        ALTER TABLE users RENAME TO users_pre_roles_widen;
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN
                ('admin', 'account_manager', 'management', 'solution', 'project', 'product',
                 'engine1_exec', 'super_admin')),
            pod TEXT,
            full_name TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK ((role IN ('engine1_exec', 'super_admin') AND pod IS NULL) OR
                   (role NOT IN ('engine1_exec', 'super_admin') AND pod IN ('pods1', 'pods2', 'pods3')))
        );
        INSERT INTO users (id, username, password, role, {"pod, " if has_pod else ""}full_name, created_at)
            SELECT id, username, password, role, {"pod, " if has_pod else ""}full_name, created_at
            FROM users_pre_roles_widen;
        DROP TABLE users_pre_roles_widen;
        """
    )
    if not has_pod:
        # Pre-multi-POD accounts all belonged to the original single-team tracker.
        db.execute("UPDATE users SET pod = 'pods1' WHERE role != 'engine1_exec' AND pod IS NULL")


def _widen_user_roles_for_pod_head(db):
    """Add 'pod_head' to the users.role CHECK constraint for databases that were
    already migrated past the super_admin rollout (so _widen_user_roles's own
    guard skips them) but predate the PODS Head role. Same rebuild-in-place
    approach - SQLite can't ALTER a CHECK constraint - copying every row across
    unchanged."""
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not row or not row["sql"] or "'pod_head'" in row["sql"]:
        return  # table doesn't exist yet, or already migrated
    db.executescript(
        """
        ALTER TABLE users RENAME TO users_pre_pod_head;
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN
                ('admin', 'pod_head', 'account_manager', 'management', 'solution', 'project', 'product',
                 'engine1_exec', 'super_admin')),
            pod TEXT,
            full_name TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK ((role IN ('engine1_exec', 'super_admin') AND pod IS NULL) OR
                   (role NOT IN ('engine1_exec', 'super_admin') AND pod IN ('pods1', 'pods2', 'pods3')))
        );
        INSERT INTO users (id, username, password, role, pod, full_name, created_at)
            SELECT id, username, password, role, pod, full_name, created_at
            FROM users_pre_pod_head;
        DROP TABLE users_pre_pod_head;
        """
    )


def migrate_db(db):
    """Add columns introduced after the first release, without touching data."""
    def columns(table):
        return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}

    _widen_user_roles(db)
    _widen_user_roles_for_pod_head(db)

    deal_cols = columns("deals")
    if "pod" not in deal_cols:
        db.execute("ALTER TABLE deals ADD COLUMN pod TEXT")
        db.execute("UPDATE deals SET pod = 'pods1' WHERE pod IS NULL")
    if "pod" not in columns("deal_tasks") and "deal_tasks" in {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        db.execute("ALTER TABLE deal_tasks ADD COLUMN pod TEXT")
        db.execute(
            """UPDATE deal_tasks SET pod = (SELECT pod FROM deals WHERE deals.id = deal_tasks.deal_id)
               WHERE pod IS NULL"""
        )
    if "performance" in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} \
            and "pod" not in columns("performance"):
        db.execute("ALTER TABLE performance ADD COLUMN pod TEXT")
        db.execute("UPDATE performance SET pod = 'pods1' WHERE pod IS NULL")
    if "login_logs" in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} \
            and "pod" not in columns("login_logs"):
        db.execute("ALTER TABLE login_logs ADD COLUMN pod TEXT")
        db.execute("UPDATE login_logs SET pod = 'pods1' WHERE pod IS NULL")
    if "pod" not in columns("config"):
        db.execute("ALTER TABLE config ADD COLUMN pod TEXT")
        # A pre-multi-POD config row carried both the shared taxonomy and PODS 1's own
        # figures together; split it into a shared row (pod IS NULL) plus a pods1 row so
        # each keeps working under the new per-POD config model, with nothing recomputed.
        legacy = db.execute("SELECT * FROM config WHERE pod IS NULL ORDER BY id LIMIT 1").fetchone()
        if legacy is not None:
            db.execute("UPDATE config SET pod = 'pods1' WHERE id = ?", (legacy["id"],))
            db.execute(
                """INSERT INTO config (pod, target_amount, strategic_pillars, squads, stages,
                   max_login_logs, updated_at)
                   VALUES (NULL, 0, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (legacy["strategic_pillars"], legacy["squads"], legacy["stages"],
                 legacy["max_login_logs"] if "max_login_logs" in legacy.keys() else 100),
            )
    if "revenue_2026" not in deal_cols:
        db.execute("ALTER TABLE deals ADD COLUMN revenue_2026 INTEGER DEFAULT 0")
        db.execute("UPDATE deals SET revenue_2026 = estimated_value WHERE revenue_2026 = 0")
    if "strategy" not in deal_cols:
        db.execute("ALTER TABLE deals ADD COLUMN strategy TEXT DEFAULT ''")
    if "proofs" not in deal_cols:
        db.execute("ALTER TABLE deals ADD COLUMN proofs TEXT DEFAULT '{}'")
    if "expected_po_date" not in deal_cols:
        db.execute("ALTER TABLE deals ADD COLUMN expected_po_date TEXT DEFAULT ''")
    if "expected_revenue_date" not in deal_cols:
        db.execute("ALTER TABLE deals ADD COLUMN expected_revenue_date TEXT DEFAULT ''")
    if "is_closed_lost" not in deal_cols:
        db.execute("ALTER TABLE deals ADD COLUMN is_closed_lost BOOLEAN DEFAULT 0")
    if "closed_lost_reason" not in deal_cols:
        db.execute("ALTER TABLE deals ADD COLUMN closed_lost_reason TEXT DEFAULT ''")

    config_cols = columns("config")
    if "am_targets" not in config_cols:
        db.execute("ALTER TABLE config ADD COLUMN am_targets TEXT DEFAULT '{}'")
        db.execute("UPDATE config SET am_targets = ?", (json.dumps(DEFAULT_AM_TARGETS),))
    if "current_achievement" not in config_cols:
        db.execute("ALTER TABLE config ADD COLUMN current_achievement INTEGER DEFAULT 0")
    if "recurring_revenue" not in config_cols:
        db.execute("ALTER TABLE config ADD COLUMN recurring_revenue INTEGER DEFAULT 0")
    if "stages" not in config_cols:
        db.execute("ALTER TABLE config ADD COLUMN stages TEXT")
        db.execute("UPDATE config SET stages = ?", (json.dumps(DEFAULT_STAGES),))
    if "am_achievements" not in config_cols:
        db.execute("ALTER TABLE config ADD COLUMN am_achievements TEXT DEFAULT '{}'")
    if "am_recurring" not in config_cols:
        db.execute("ALTER TABLE config ADD COLUMN am_recurring TEXT DEFAULT '{}'")
    if "auto_stage" not in config_cols:
        # Column kept for backward compatibility with older backups; stage automation
        # has been removed from the app, so this is no longer read anywhere.
        db.execute("ALTER TABLE config ADD COLUMN auto_stage INTEGER DEFAULT 1")
    if "stage_rules" not in config_cols:
        db.execute("ALTER TABLE config ADD COLUMN stage_rules TEXT DEFAULT '{}'")
    if "max_login_logs" not in config_cols:
        db.execute("ALTER TABLE config ADD COLUMN max_login_logs INTEGER DEFAULT 100")

    if "deal_tasks" in {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )} and "due" not in columns("deal_tasks"):
        db.execute("ALTER TABLE deal_tasks ADD COLUMN due TEXT DEFAULT ''")

    if "deal_tasks" in {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}:
        task_cols = columns("deal_tasks")
        if "source_team" not in task_cols:
            db.execute("ALTER TABLE deal_tasks ADD COLUMN source_team TEXT DEFAULT 'sales'")
            # Best-effort backfill for existing rows: a cross-functional user who filed
            # a task under their own team is the source; everything else defaults to
            # 'sales' (admin/pod_head/account_manager), which is already the column default.
            db.execute(
                """UPDATE deal_tasks SET source_team = team
                   WHERE EXISTS (
                       SELECT 1 FROM users
                       WHERE (users.full_name = deal_tasks.created_by OR users.username = deal_tasks.created_by)
                         AND users.role = deal_tasks.team
                         AND users.role IN ('solution', 'project', 'product')
                   )"""
            )
        if "assigned_to" not in task_cols:
            db.execute("ALTER TABLE deal_tasks ADD COLUMN assigned_to TEXT DEFAULT ''")
            # Best-effort backfill: a task a cross-functional team filed on their own
            # initiative was implicitly for the opportunity's AM to see - anything
            # sales/admin filed already names its target via the `team` column, so
            # leave those blank rather than guess an individual.
            db.execute(
                """UPDATE deal_tasks SET assigned_to = (
                       SELECT assigned_am FROM deals WHERE deals.id = deal_tasks.deal_id
                   ) WHERE source_team != 'sales' AND (assigned_to IS NULL OR assigned_to = '')"""
            )

    # A database that already existed before super_admin was introduced won't get one
    # from the empty-table seed path below, so add a bootstrap account here instead -
    # non-destructive, and skipped entirely once any super_admin account exists (or on a
    # brand-new database, where the seed path below creates the full seed set instead).
    user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count > 0 and db.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'super_admin'"
    ).fetchone()[0] == 0:
        username = "super_admin"
        if db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
            username = "super_admin2"  # extremely unlikely collision fallback
        db.execute(
            "INSERT INTO users (username, password, role, pod, full_name) VALUES (?, ?, ?, ?, ?)",
            (username, _hash("changeme123"), "super_admin", None, "Super Admin"),
        )


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pod TEXT NOT NULL CHECK(pod IN ('pods1', 'pods2', 'pods3')),
            deal_name TEXT NOT NULL,
            customer TEXT DEFAULT '',
            assigned_am TEXT DEFAULT '',
            squad TEXT NOT NULL,
            strategic_pillar TEXT NOT NULL,
            estimated_value INTEGER NOT NULL,
            revenue_2026 INTEGER DEFAULT 0,
            target_quarter TEXT DEFAULT '',
            stage TEXT NOT NULL,
            progress INTEGER DEFAULT 0,
            is_blocked BOOLEAN DEFAULT 0,
            blocker_description TEXT,
            is_closed_lost BOOLEAN DEFAULT 0,
            closed_lost_reason TEXT DEFAULT '',
            next_actions TEXT,
            strategy TEXT DEFAULT '',
            proofs TEXT DEFAULT '{}',
            expected_po_date TEXT DEFAULT '',
            expected_revenue_date TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN
                ('admin', 'pod_head', 'account_manager', 'management', 'solution', 'project', 'product',
                 'engine1_exec', 'super_admin')),
            pod TEXT,
            full_name TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK ((role IN ('engine1_exec', 'super_admin') AND pod IS NULL) OR
                   (role NOT IN ('engine1_exec', 'super_admin') AND pod IN ('pods1', 'pods2', 'pods3')))
        );

        CREATE TABLE IF NOT EXISTS deal_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id INTEGER NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
            pod TEXT NOT NULL CHECK(pod IN ('pods1', 'pods2', 'pods3')),
            text TEXT NOT NULL,
            team TEXT NOT NULL CHECK(team IN ('solution', 'project', 'product')),
            source_team TEXT NOT NULL DEFAULT 'sales' CHECK(source_team IN
                ('sales', 'solution', 'project', 'product')),
            status TEXT NOT NULL DEFAULT 'not_started' CHECK(status IN
                ('not_started', 'in_progress', 'blocked', 'needs_discussion', 'done')),
            note TEXT DEFAULT '',
            due TEXT DEFAULT '',
            created_by TEXT DEFAULT '',
            assigned_to TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS login_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            pod TEXT,
            username TEXT DEFAULT '',
            full_name TEXT DEFAULT '',
            role TEXT DEFAULT '',
            ip_address TEXT DEFAULT '',
            login_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pod TEXT NOT NULL CHECK(pod IN ('pods1', 'pods2', 'pods3')),
            label TEXT DEFAULT '',
            source_file TEXT DEFAULT '',
            am_summary TEXT DEFAULT '[]',
            accounts TEXT DEFAULT '[]',
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- One row with pod IS NULL holds the Engine-1-wide shared taxonomy (Stages,
        -- Strategic Pillars, and the login-log retention setting), editable only by
        -- engine1_exec. One row per POD ('pods1'/'pods2'/'pods3') holds that POD's own
        -- target/achievement/recurring figures, editable only by that POD's admin.
        CREATE TABLE IF NOT EXISTS config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pod TEXT UNIQUE CHECK(pod IN ('pods1', 'pods2', 'pods3') OR pod IS NULL),
            target_amount INTEGER DEFAULT 163000000000,
            strategic_pillars TEXT,
            squads TEXT,
            am_targets TEXT DEFAULT '{}',
            current_achievement INTEGER DEFAULT 0,
            recurring_revenue INTEGER DEFAULT 0,
            stages TEXT,
            am_achievements TEXT DEFAULT '{}',
            am_recurring TEXT DEFAULT '{}',
            auto_stage INTEGER DEFAULT 1,
            stage_rules TEXT,
            max_login_logs INTEGER DEFAULT 100,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # Safe, additive migrations for databases created by an earlier version.
    # ALTER only when the column is missing, so existing data is preserved.
    migrate_db(db)

    # Seed users if empty: PODS 1 gets the real team (carried over from the original
    # single-POD tracker); PODS 2 and PODS 3 each get one bootstrap admin so that team
    # can log in and start configuring their own POD; one engine1_exec account gets the
    # combined cross-POD view. All bootstrap passwords should be changed after first login.
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        seed_users = [
            ("admin", "admin123", "admin", "pods1", "Administrator"),
            ("exec", "exec123", "management", "pods1", "Management Viewer"),
            ("pods2_admin", "changeme123", "admin", "pods2", "PODS 2 Administrator"),
            ("pods3_admin", "changeme123", "admin", "pods3", "PODS 3 Administrator"),
            ("engine1_exec", "changeme123", "engine1_exec", None, "Engine 1 Executive"),
            ("super_admin", "changeme123", "super_admin", None, "Super Admin"),
        ]
        for username, password, role, pod, full_name in seed_users:
            db.execute(
                "INSERT INTO users (username, password, role, pod, full_name) VALUES (?, ?, ?, ?, ?)",
                (username, _hash(password), role, pod, full_name),
            )
        for username, password, full_name in SEED_AMS:
            db.execute(
                "INSERT INTO users (username, password, role, pod, full_name) VALUES (?, ?, ?, ?, ?)",
                (username, _hash(password), "account_manager", "pods1", full_name),
            )

    # Seed config if empty: one shared row (pod IS NULL) for the Engine-1-wide Stages/
    # Pillars taxonomy, plus one row per POD for that POD's own target/achievement figures.
    if db.execute("SELECT COUNT(*) FROM config").fetchone()[0] == 0:
        db.execute(
            """INSERT INTO config (pod, target_amount, strategic_pillars, squads, stages)
               VALUES (NULL, 0, ?, ?, ?)""",
            (json.dumps(DEFAULT_PILLARS), json.dumps(DEFAULT_SQUADS), json.dumps(DEFAULT_STAGES)),
        )
        db.execute(
            """INSERT INTO config (pod, target_amount, strategic_pillars, squads, am_targets, stages)
               VALUES ('pods1', ?, ?, ?, ?, ?)""",
            (163_000_000_000, json.dumps(DEFAULT_PILLARS), json.dumps(DEFAULT_SQUADS),
             json.dumps(DEFAULT_AM_TARGETS), json.dumps(DEFAULT_STAGES)),
        )
        for pod in ("pods2", "pods3"):
            db.execute(
                """INSERT INTO config (pod, target_amount, strategic_pillars, squads, stages)
                   VALUES (?, 0, ?, ?, ?)""",
                (pod, json.dumps(DEFAULT_PILLARS), json.dumps(DEFAULT_SQUADS), json.dumps(DEFAULT_STAGES)),
            )

    # Seed deals if empty: the real pipeline goes to PODS 1; PODS 2 and PODS 3 start empty.
    if db.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 0:
        db.executemany(
            """INSERT INTO deals
               (pod, deal_name, customer, assigned_am, squad, strategic_pillar,
                estimated_value, target_quarter, stage, progress,
                is_blocked, blocker_description, next_actions)
               VALUES ('pods1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            _seed_deals(),
        )
        # Default 2026 realizable revenue to the full TCV; admin refines per deal.
        db.execute("UPDATE deals SET revenue_2026 = estimated_value WHERE pod = 'pods1'")

    db.commit()
    db.close()


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------
def get_current_user():
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip() if auth else request.args.get("token", "")
    return TOKENS.get(token)


def login_required(roles=None):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = get_current_user()
            if not user:
                return jsonify({"error": "Unauthorized"}), 401
            if roles and user["role"] not in roles:
                return jsonify({"error": "Forbidden"}), 403
            g.current_user = user
            return f(*args, **kwargs)

        return wrapper

    return decorator


def deal_visible_to(user, deal_row):
    """Read access: engine1_exec and super_admin see every POD; everyone else only theirs."""
    if user["role"] in PODLESS_ROLES:
        return True
    return deal_row["pod"] == user.get("pod")


def can_edit_deal(user, deal_row):
    """super_admin edits anything, anywhere. ADMIN edits anything in their own POD; an AM
    edits only opportunities assigned to them, also within their own POD - a deal can
    never be edited across PODS by anyone except super_admin."""
    if user["role"] == "super_admin":
        return True
    if deal_row["pod"] != user.get("pod"):
        return False
    if user["role"] in ("admin", "pod_head"):
        return True
    if user["role"] == "account_manager":
        return (deal_row["assigned_am"] or "") == (user["full_name"] or "")
    return False


def is_valid_assignee(db, pod, name):
    """A task's assigned_to must be a real, currently-registered user in that same
    POD who isn't admin/pod_head/management - it's a picker, not free text."""
    if not name or not pod:
        return False
    row = db.execute(
        "SELECT 1 FROM users WHERE full_name = ? AND pod = ? "
        "AND role NOT IN ('admin', 'pod_head', 'management')",
        (name, pod),
    ).fetchone()
    return row is not None


def query_pod_for(user):
    """Which POD's data a request should be scoped to.

    Pod-scoped roles always see only their own POD, regardless of any ?pod= they pass.
    engine1_exec/super_admin have no POD of their own: passing a valid ?pod= drills into
    that one POD, otherwise (the default) it means "all three PODS combined" and callers
    get None back to signal an unfiltered, cross-POD read."""
    if user["role"] not in PODLESS_ROLES:
        return user.get("pod")
    requested = request.args.get("pod")
    return requested if requested in PODS else None


def mutation_pod_for(user, data=None):
    """The POD a create/import mutation by a podless role (super_admin only - engine1_exec
    never mutates deal/task/backup data) should act on: an explicit `pod` in the JSON body,
    else the query string, else a multipart form field. Returns None if none of those gave
    a valid POD, which callers must treat as a 400 (a podless role can't create/import
    without saying which POD it's for)."""
    if user.get("pod"):
        return user["pod"]
    if data and data.get("pod") in PODS:
        return data["pod"]
    requested = request.args.get("pod")
    if requested in PODS:
        return requested
    if request.form and request.form.get("pod") in PODS:
        return request.form.get("pod")
    return None


def get_shared_config_row(db):
    return db.execute("SELECT * FROM config WHERE pod IS NULL ORDER BY id DESC LIMIT 1").fetchone()


def get_pod_config_row(db, pod):
    return db.execute("SELECT * FROM config WHERE pod = ? ORDER BY id DESC LIMIT 1", (pod,)).fetchone()


def current_config(db):
    """The Engine-1-wide shared settings row (Stages/Pillars/login-log retention) -
    used where a POD-specific figure would not make sense, e.g. login log retention."""
    return config_to_dict(get_shared_config_row(db))


def resolve_stage(db, data, proofs, is_blocked, requested_stage, fallback_stage):
    """Stage is set manually by the AM/admin - no auto-derivation from the framework."""
    return requested_stage or fallback_stage


def deal_to_dict(row):
    return {
        "id": row["id"],
        "pod": row["pod"] if "pod" in row.keys() else "",
        "pod_label": POD_LABELS.get(row["pod"] if "pod" in row.keys() else "", ""),
        "deal_name": row["deal_name"],
        "customer": row["customer"] or "",
        "assigned_am": row["assigned_am"] or "",
        "squad": row["squad"],
        "strategic_pillar": row["strategic_pillar"],
        "estimated_value": row["estimated_value"],
        "revenue_2026": row["revenue_2026"] if row["revenue_2026"] is not None else 0,
        "target_quarter": row["target_quarter"] or "",
        "stage": row["stage"],
        "progress": row["progress"],
        "is_blocked": bool(row["is_blocked"]),
        "blocker_description": row["blocker_description"] or "",
        "is_closed_lost": bool(row["is_closed_lost"] if "is_closed_lost" in row.keys() else 0),
        "closed_lost_reason": (row["closed_lost_reason"] if "closed_lost_reason" in row.keys() else "") or "",
        "next_actions": json.loads(row["next_actions"] or "[]"),
        "strategy": (row["strategy"] if "strategy" in row.keys() else "") or "",
        "proofs": normalize_proofs(
            json.loads((row["proofs"] if "proofs" in row.keys() else "") or "{}")
        ),
        "expected_po_date": (row["expected_po_date"] if "expected_po_date" in row.keys() else "") or "",
        "expected_revenue_date": (row["expected_revenue_date"] if "expected_revenue_date" in row.keys() else "") or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", app_version=APP_VERSION)


@app.route("/api/version", methods=["GET"])
def get_version():
    return jsonify({"version": APP_VERSION})


# --------------------------------------------------------------------------
# Auth API
# --------------------------------------------------------------------------
JAKARTA_TZ = timezone(timedelta(hours=7))  # GMT+7 / WIB, no DST


def jakarta_now_str():
    return datetime.now(JAKARTA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def trim_login_logs(db, max_logs):
    """Keep only the most recent `max_logs` rows PER POD, so one busy POD's sign-ins
    can never crowd out another POD's history, and the table never grows unbounded."""
    for pod in (None,) + PODS:
        db.execute(
            f"""DELETE FROM login_logs WHERE pod {"IS" if pod is None else "="} ? AND id NOT IN (
                   SELECT id FROM login_logs WHERE pod {"IS" if pod is None else "="} ?
                   ORDER BY login_at DESC, id DESC LIMIT ?
               )""",
            (pod, pod, max_logs),
        )


def log_login(db, row):
    """Record a successful sign-in (timestamped in Jakarta/WIB, GMT+7) and trim
    to the admin-configured retention limit."""
    db.execute(
        """INSERT INTO login_logs (user_id, pod, username, full_name, role, ip_address, login_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (row["id"], row["pod"] if "pod" in row.keys() else None, row["username"],
         row["full_name"] or row["username"], row["role"], client_ip(), jakarta_now_str()),
    )
    max_logs = current_config(db).get("max_login_logs", 100)
    trim_login_logs(db, max_logs)


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not row or not check_password_hash(row["password"], password):
        return jsonify({"error": "Invalid username or password"}), 401

    pod = row["pod"] if "pod" in row.keys() else None
    token = secrets.token_hex(24)
    TOKENS[token] = {
        "user_id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "pod": pod,
        "full_name": row["full_name"] or row["username"],
    }
    log_login(db, row)
    db.commit()
    return jsonify({
        "token": token,
        "role": row["role"],
        "pod": pod,
        "pod_label": POD_LABELS.get(pod, "All PODS (Engine 1)"),
        "username": row["username"],
        "full_name": row["full_name"] or row["username"],
    })


@app.route("/api/logout", methods=["POST"])
def api_logout():
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip()
    TOKENS.pop(token, None)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Login Logs (ADMIN only) - who signed in and when, capped at a configurable
# number of most-recent rows so the table can never grow unbounded.
# --------------------------------------------------------------------------
def login_log_to_dict(row):
    return {
        "id": row["id"],
        "username": row["username"] or "",
        "full_name": row["full_name"] or "",
        "role": row["role"] or "",
        "ip_address": row["ip_address"] or "",
        "login_at": row["login_at"],
    }


@app.route("/api/login_logs", methods=["GET"])
@login_required(roles=("admin", "pod_head", "super_admin"))
def get_login_logs():
    db = get_db()
    pod = query_pod_for(g.current_user)
    if pod is not None:
        rows = db.execute(
            "SELECT * FROM login_logs WHERE pod = ? ORDER BY login_at DESC, id DESC", (pod,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM login_logs ORDER BY login_at DESC, id DESC").fetchall()
    return jsonify({
        "logs": [login_log_to_dict(r) for r in rows],
        "max_login_logs": current_config(db).get("max_login_logs", 100),
    })


@app.route("/api/login_logs/export_xlsx", methods=["GET"])
@login_required(roles=("admin", "pod_head", "super_admin"))
def export_login_logs_xlsx():
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        return jsonify({"error": "openpyxl is not installed on the server. "
                                 "Run: pip install --user openpyxl, then reload."}), 500

    db = get_db()
    pod = query_pod_for(g.current_user)
    if pod is not None:
        rows = db.execute(
            "SELECT * FROM login_logs WHERE pod = ? ORDER BY login_at DESC, id DESC", (pod,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM login_logs ORDER BY login_at DESC, id DESC").fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Login Logs"
    ws.append(["Username", "Full Name", "Role", "Login At (GMT+7)", "IP Address"])
    for c in range(1, 6):
        cell = ws.cell(row=1, column=c)
        cell.fill = PatternFill("solid", fgColor="1A73E8")
        cell.font = Font(color="FFFFFF", bold=True)
    ws.freeze_panes = "A2"
    for r in rows:
        ws.append([r["username"], r["full_name"], r["role"], r["login_at"], r["ip_address"]])
    for col, width in zip("ABCDE", [18, 24, 16, 20, 16]):
        ws.column_dimensions[col].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"login_logs_{date.today().isoformat()}.xlsx"
    return send_file(
        buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=filename)


# --------------------------------------------------------------------------
# Account Managers (for filters / assignment dropdowns) - any authenticated user
# --------------------------------------------------------------------------
@app.route("/api/proof_framework", methods=["GET"])
@login_required()
def get_proof_framework():
    """The 8 Enterprise Proofs execution framework (static definition)."""
    return jsonify([{"key": k, "name": n, "description": d} for k, n, d in PROOFS])


@app.route("/api/account_managers", methods=["GET"])
@login_required()
def get_account_managers():
    db = get_db()
    pod = query_pod_for(g.current_user)
    if pod is not None:
        rows = db.execute(
            "SELECT full_name FROM users WHERE role = 'account_manager' AND pod = ? ORDER BY full_name",
            (pod,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT full_name FROM users WHERE role = 'account_manager' ORDER BY full_name"
        ).fetchall()
    names = [r["full_name"] for r in rows if r["full_name"]]
    return jsonify(names)


# --------------------------------------------------------------------------
# Deals API
# --------------------------------------------------------------------------
@app.route("/api/deals", methods=["GET"])
@login_required()
def get_deals():
    db = get_db()
    user = g.current_user
    pod = query_pod_for(user)
    query = "SELECT * FROM deals WHERE 1=1"
    params = []
    if pod is not None:
        query += " AND pod = ?"
        params.append(pod)
    for field, col in (("squad", "squad"), ("pillar", "strategic_pillar"),
                       ("am", "assigned_am"), ("stage", "stage"),
                       ("quarter", "target_quarter")):
        val = request.args.get(field)
        if val:
            query += f" AND {col} = ?"
            params.append(val)
    query += " ORDER BY estimated_value DESC"
    rows = db.execute(query, params).fetchall()
    return jsonify([deal_to_dict(r) for r in rows])


@app.route("/api/deals", methods=["POST"])
@login_required(roles=("admin", "pod_head", "account_manager", "super_admin"))
def create_deal():
    data = request.get_json(force=True) or {}
    user = g.current_user

    pod = mutation_pod_for(user, data)
    if not pod:
        return jsonify({"error": "pod is required (pods1/pods2/pods3) when creating as super_admin"}), 400

    # An AM can only create opportunities assigned to themselves.
    if user["role"] == "account_manager":
        assigned_am = user["full_name"]
    else:
        assigned_am = data.get("assigned_am", "")

    est_value = int(data.get("estimated_value", 0) or 0)
    rev_2026 = data.get("revenue_2026")
    rev_2026 = int(rev_2026) if rev_2026 not in (None, "") else est_value

    db = get_db()
    new_proofs = normalize_proofs(data.get("proofs"))
    new_blocked = bool(data.get("is_blocked"))
    stage = resolve_stage(db, data, new_proofs, new_blocked,
                          data.get("stage", "Prospecting"), "Prospecting")
    new_closed_lost = bool(data.get("is_closed_lost"))
    cur = db.execute(
        """INSERT INTO deals
           (pod, deal_name, customer, assigned_am, squad, strategic_pillar, estimated_value,
            revenue_2026, target_quarter, stage, progress, is_blocked, blocker_description,
            is_closed_lost, closed_lost_reason,
            next_actions, strategy, proofs, expected_po_date, expected_revenue_date, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (
            # A deal always belongs to its creator's own POD, or the POD a podless
            # super_admin explicitly picked - never anything else client-supplied.
            pod,
            data.get("deal_name", "Untitled Opportunity"),
            data.get("customer", ""),
            assigned_am,
            data.get("squad", "") or "",
            data.get("strategic_pillar"),
            est_value,
            rev_2026,
            data.get("target_quarter", ""),
            stage,
            int(data.get("progress", 0) or 0),
            1 if new_blocked else 0,
            data.get("blocker_description", ""),
            1 if new_closed_lost else 0,
            data.get("closed_lost_reason", ""),
            json.dumps(data.get("next_actions", [])),
            data.get("strategy", ""),
            json.dumps(new_proofs),
            str(data.get("expected_po_date", "") or "").strip()[:10],
            str(data.get("expected_revenue_date", "") or "").strip()[:10],
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM deals WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(deal_to_dict(row)), 201


@app.route("/api/deals/<int:deal_id>", methods=["PUT"])
@login_required(roles=("admin", "pod_head", "account_manager", "super_admin"))
def update_deal(deal_id):
    data = request.get_json(force=True) or {}
    user = g.current_user
    db = get_db()
    row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if not row:
        return jsonify({"error": "Deal not found"}), 404
    if not can_edit_deal(user, row):
        return jsonify({"error": "You can only edit opportunities assigned to you"}), 403

    # AMs cannot reassign a deal to someone else.
    if user["role"] == "account_manager":
        assigned_am = user["full_name"]
    else:
        assigned_am = data.get("assigned_am", row["assigned_am"])

    existing_strategy = row["strategy"] if "strategy" in row.keys() else ""
    existing_proofs = normalize_proofs(
        json.loads((row["proofs"] if "proofs" in row.keys() else "") or "{}")
    )
    upd_proofs = normalize_proofs(data.get("proofs", existing_proofs))
    upd_blocked = bool(data.get("is_blocked", row["is_blocked"]))
    existing_closed_lost = bool(row["is_closed_lost"] if "is_closed_lost" in row.keys() else 0)
    upd_closed_lost = bool(data.get("is_closed_lost", existing_closed_lost))
    existing_closed_lost_reason = (row["closed_lost_reason"] if "closed_lost_reason" in row.keys() else "") or ""
    upd_stage = resolve_stage(db, data, upd_proofs, upd_blocked,
                              data.get("stage", row["stage"]), row["stage"])
    existing_po = row["expected_po_date"] if "expected_po_date" in row.keys() else ""
    existing_rev = row["expected_revenue_date"] if "expected_revenue_date" in row.keys() else ""
    db.execute(
        """UPDATE deals SET
             deal_name = ?, customer = ?, assigned_am = ?, squad = ?, strategic_pillar = ?,
             estimated_value = ?, revenue_2026 = ?, target_quarter = ?, stage = ?, progress = ?,
             is_blocked = ?, blocker_description = ?, is_closed_lost = ?, closed_lost_reason = ?,
             next_actions = ?, strategy = ?, proofs = ?,
             expected_po_date = ?, expected_revenue_date = ?,
             updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (
            data.get("deal_name", row["deal_name"]),
            data.get("customer", row["customer"]),
            assigned_am,
            data.get("squad", row["squad"]),
            data.get("strategic_pillar", row["strategic_pillar"]),
            int(data.get("estimated_value", row["estimated_value"]) or 0),
            int(data.get("revenue_2026", row["revenue_2026"] or 0) or 0),
            data.get("target_quarter", row["target_quarter"]),
            upd_stage,
            int(data.get("progress", row["progress"]) or 0),
            1 if upd_blocked else 0,
            data.get("blocker_description", row["blocker_description"]),
            1 if upd_closed_lost else 0,
            data.get("closed_lost_reason", existing_closed_lost_reason),
            json.dumps(data.get("next_actions", json.loads(row["next_actions"] or "[]"))),
            data.get("strategy", existing_strategy),
            json.dumps(upd_proofs),
            str(data.get("expected_po_date", existing_po) or "").strip()[:10],
            str(data.get("expected_revenue_date", existing_rev) or "").strip()[:10],
            deal_id,
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    return jsonify(deal_to_dict(row))


@app.route("/api/deals/<int:deal_id>", methods=["DELETE"])
@login_required(roles=("admin", "pod_head", "account_manager", "super_admin"))
def delete_deal(deal_id):
    db = get_db()
    row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if not row:
        return jsonify({"error": "Deal not found"}), 404
    if not can_edit_deal(g.current_user, row):
        return jsonify({"error": "You can only delete opportunities assigned to you"}), 403
    db.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/deals/bulk_delete", methods=["POST"])
@login_required(roles=("admin", "pod_head", "account_manager", "super_admin"))
def bulk_delete_deals():
    """Delete several opportunities in one request. Same permission rule as the
    single-delete endpoint (can_edit_deal), just checked per id - an account_manager
    can only ever delete their own opportunities, admin only within their own POD,
    super_admin anywhere. IDs that don't exist or aren't editable by this user are
    skipped and reported back rather than failing the whole batch."""
    data = request.get_json(force=True) or {}
    ids = data.get("ids") or []
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return jsonify({"error": "ids must be a list of integers"}), 400
    if not ids:
        return jsonify({"error": "No opportunities selected"}), 400

    db = get_db()
    user = g.current_user
    deleted_ids, skipped = [], []
    for deal_id in ids:
        row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if not row:
            skipped.append({"id": deal_id, "reason": "not found"})
            continue
        if not can_edit_deal(user, row):
            skipped.append({"id": deal_id, "reason": "not permitted", "deal_name": row["deal_name"]})
            continue
        db.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
        deleted_ids.append(deal_id)
    db.commit()
    return jsonify({"ok": True, "deleted": len(deleted_ids), "deleted_ids": deleted_ids, "skipped": skipped})


@app.route("/api/deals/<int:deal_id>/progress", methods=["PUT"])
@login_required(roles=("admin", "pod_head", "account_manager", "super_admin"))
def update_progress(deal_id):
    data = request.get_json(force=True) or {}
    progress = max(0, min(100, int(data.get("progress", 0))))
    db = get_db()
    row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if not row:
        return jsonify({"error": "Deal not found"}), 404
    if not can_edit_deal(g.current_user, row):
        return jsonify({"error": "You can only edit opportunities assigned to you"}), 403
    db.execute(
        "UPDATE deals SET progress = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (progress, deal_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    return jsonify(deal_to_dict(row))


@app.route("/api/deals/<int:deal_id>/blocker", methods=["PUT"])
@login_required(roles=("admin", "pod_head", "account_manager", "super_admin"))
def update_blocker(deal_id):
    data = request.get_json(force=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if not row:
        return jsonify({"error": "Deal not found"}), 404
    if not can_edit_deal(g.current_user, row):
        return jsonify({"error": "You can only edit opportunities assigned to you"}), 403
    blocked = bool(data.get("is_blocked"))
    proofs = normalize_proofs(json.loads((row["proofs"] if "proofs" in row.keys() else "") or "{}"))
    stage = resolve_stage(db, data, proofs, blocked, row["stage"], row["stage"])
    db.execute(
        """UPDATE deals SET is_blocked = ?, blocker_description = ?, stage = ?,
           updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (1 if blocked else 0, data.get("blocker_description", ""), stage, deal_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    return jsonify(deal_to_dict(row))


# --------------------------------------------------------------------------
# Team Tasks API - the bridge between Sales and Solution / Project / Product.
# Sales (admin/account_manager) create and fully manage tasks on their own deals.
# Cross-functional roles (solution/project/product) may only flip the status and
# edit the note on tasks assigned to their own team - never the task text, the
# team assignment, or any other opportunity field.
# --------------------------------------------------------------------------
def task_to_dict(row):
    keys = row.keys()
    d = {
        "id": row["id"],
        "deal_id": row["deal_id"],
        "pod": row["pod"] if "pod" in keys else "",
        "pod_label": POD_LABELS.get(row["pod"] if "pod" in keys else "", ""),
        "text": row["text"],
        "team": row["team"],
        "source_team": (row["source_team"] if "source_team" in keys else "sales") or "sales",
        "status": row["status"],
        "note": row["note"] or "",
        "due": (row["due"] or "") if "due" in keys else "",
        "assigned_to": (row["assigned_to"] if "assigned_to" in keys else "") or "",
        "created_by": row["created_by"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if "deal_name" in row.keys():
        d["deal_name"] = row["deal_name"]
        d["customer"] = row["customer"] or ""
        d["assigned_am"] = row["assigned_am"] or ""
    return d


@app.route("/api/deals/<int:deal_id>/tasks", methods=["GET"])
@login_required()
def get_deal_tasks(deal_id):
    db = get_db()
    deal_row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if not deal_row or not deal_visible_to(g.current_user, deal_row):
        return jsonify({"error": "Deal not found"}), 404
    rows = db.execute(
        "SELECT * FROM deal_tasks WHERE deal_id = ? ORDER BY created_at", (deal_id,)
    ).fetchall()
    return jsonify([task_to_dict(r) for r in rows])


@app.route("/api/deals/<int:deal_id>/tasks", methods=["POST"])
@login_required(roles=("admin", "pod_head", "account_manager", "super_admin") + CROSS_FUNCTIONAL_ROLES)
def create_deal_task(deal_id):
    data = request.get_json(force=True) or {}
    user = g.current_user
    db = get_db()
    deal_row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if not deal_row or not deal_visible_to(user, deal_row):
        return jsonify({"error": "Deal not found"}), 404

    if user["role"] in CROSS_FUNCTIONAL_ROLES:
        # Solution/Project/Product can flag a follow-up on any opportunity, but only
        # ever under their own team - they can't file work on another team's behalf.
        team = user["role"]
        source_team = user["role"]
    else:
        if not can_edit_deal(user, deal_row):
            return jsonify({"error": "You can only add tasks to opportunities assigned to you"}), 403
        team = str(data.get("team", "") or "").strip()
        if team not in CROSS_FUNCTIONAL_ROLES:
            return jsonify({"error": "A valid team (solution/project/product) is required"}), 400
        source_team = "sales"

    text = str(data.get("text", "") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400

    # Every task must name who it's actually for, not just which team - so the
    # weekly review can see who to chase without opening the opportunity. It's a
    # picker over registered users of the same POD (excluding admin/pod_head/
    # management), not free text.
    assigned_to = str(data.get("assigned_to", "") or "").strip()
    if not is_valid_assignee(db, deal_row["pod"], assigned_to):
        return jsonify({"error": "Choose who this task is assigned to from the user list"}), 400

    # The creator decides the starting status and an optional target date right away,
    # rather than always starting at "not_started" and having to change it afterward.
    status = str(data.get("status", "") or "").strip()
    if status not in TASK_STATUSES:
        status = "not_started"
    due = str(data.get("due", "") or "").strip()[:10]

    creator = user.get("full_name") or user.get("username", "")
    cur = db.execute(
        """INSERT INTO deal_tasks (deal_id, pod, text, team, source_team, status, due,
           created_by, assigned_to)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (deal_id, deal_row["pod"], text, team, source_team, status, due, creator, assigned_to),
    )
    db.commit()
    row = db.execute("SELECT * FROM deal_tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(task_to_dict(row)), 201


@app.route("/api/tasks/<int:task_id>", methods=["PUT"])
@login_required()
def update_task(task_id):
    data = request.get_json(force=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM deal_tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return jsonify({"error": "Task not found"}), 404

    user = g.current_user
    text, team, status, note = row["text"], row["team"], row["status"], row["note"]
    due = row["due"] if "due" in row.keys() else ""
    assigned_to = row["assigned_to"] if "assigned_to" in row.keys() else ""

    if user["role"] in CROSS_FUNCTIONAL_ROLES:
        # Own team's tasks only, and only within their own POD; text/status/note/due/
        # assigned_to are theirs to keep current, but the team assignment itself is
        # fixed - they can't move a task to another team.
        if row["team"] != user["role"] or row["pod"] != user.get("pod"):
            return jsonify({"error": "You can only update your own team's tasks"}), 403
        if str(data.get("text", "")).strip():
            text = str(data["text"]).strip()
        if "status" in data:
            new_status = str(data["status"] or "")
            if new_status not in TASK_STATUSES:
                return jsonify({"error": "Invalid status"}), 400
            status = new_status
        if "note" in data:
            note = str(data["note"] or "")
        if "due" in data:
            due = str(data["due"] or "").strip()[:10]
        if "assigned_to" in data:
            new_assignee = str(data["assigned_to"] or "").strip()
            if not is_valid_assignee(db, row["pod"], new_assignee):
                return jsonify({"error": "Choose who this task is assigned to from the user list"}), 400
            assigned_to = new_assignee
    elif user["role"] in ("admin", "pod_head", "account_manager", "super_admin"):
        deal_row = db.execute("SELECT * FROM deals WHERE id = ?", (row["deal_id"],)).fetchone()
        if not deal_row or not can_edit_deal(user, deal_row):
            return jsonify({"error": "You can only edit tasks on opportunities assigned to you"}), 403
        if str(data.get("text", "")).strip():
            text = str(data["text"]).strip()
        if "team" in data:
            new_team = str(data["team"] or "")
            if new_team not in CROSS_FUNCTIONAL_ROLES:
                return jsonify({"error": "Invalid team"}), 400
            team = new_team
        if "status" in data:
            new_status = str(data["status"] or "")
            if new_status not in TASK_STATUSES:
                return jsonify({"error": "Invalid status"}), 400
            status = new_status
        if "note" in data:
            note = str(data["note"] or "")
        if "due" in data:
            due = str(data["due"] or "").strip()[:10]
        if "assigned_to" in data:
            new_assignee = str(data["assigned_to"] or "").strip()
            if not is_valid_assignee(db, row["pod"], new_assignee):
                return jsonify({"error": "Choose who this task is assigned to from the user list"}), 400
            assigned_to = new_assignee
    elif user["role"] in ("management", "engine1_exec"):
        # Read-only everywhere else, but these roles run the weekly review, so they
        # can mark a task's checklist state (done / needs discussion / etc.) and log
        # a note there - not the text, team, due date, or assignee, which stay owned
        # by sales or the cross-functional team that filed the task. engine1_exec
        # must stay within a POD it's currently viewing (or any, if none selected).
        pod = query_pod_for(user)
        if pod is not None and row["pod"] != pod:
            return jsonify({"error": "Task not found"}), 404
        if "status" in data:
            new_status = str(data["status"] or "")
            if new_status not in TASK_STATUSES:
                return jsonify({"error": "Invalid status"}), 400
            status = new_status
        if "note" in data:
            note = str(data["note"] or "")
    else:
        return jsonify({"error": "Forbidden"}), 403

    db.execute(
        """UPDATE deal_tasks SET text = ?, team = ?, status = ?, note = ?, due = ?,
           assigned_to = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (text, team, status, note, due, assigned_to, task_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM deal_tasks WHERE id = ?", (task_id,)).fetchone()
    return jsonify(task_to_dict(row))


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
@login_required()
def delete_task(task_id):
    db = get_db()
    row = db.execute("SELECT * FROM deal_tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return jsonify({"error": "Task not found"}), 404
    user = g.current_user
    if user["role"] in CROSS_FUNCTIONAL_ROLES:
        if row["team"] != user["role"] or row["pod"] != user.get("pod"):
            return jsonify({"error": "You can only delete your own team's tasks"}), 403
    elif user["role"] in ("admin", "pod_head", "account_manager", "super_admin"):
        deal_row = db.execute("SELECT * FROM deals WHERE id = ?", (row["deal_id"],)).fetchone()
        if not deal_row or not can_edit_deal(user, deal_row):
            return jsonify({"error": "You can only delete tasks on opportunities assigned to you"}), 403
    else:
        return jsonify({"error": "Forbidden"}), 403
    db.execute("DELETE FROM deal_tasks WHERE id = ?", (task_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/tasks", methods=["GET"])
@login_required()
def list_tasks():
    """Cross-opportunity task list.

    Default behaviour: cross-functional roles only ever see their own team's tasks
    (their personal follow-up inbox, "My Team Tasks"). Passing ?scope=all opts any
    authenticated role - including cross-functional ones - into seeing every task,
    optionally filtered by ?team= and ?status=; this powers the shared "Weekly
    Meeting" board that everyone (sales included) can see ahead of the sync."""
    db = get_db()
    user = g.current_user
    scope_all = request.args.get("scope") == "all"
    query = """SELECT t.*, d.deal_name, d.customer, d.assigned_am
               FROM deal_tasks t JOIN deals d ON d.id = t.deal_id WHERE 1=1"""
    params = []
    pod = query_pod_for(user)
    if pod is not None:
        query += " AND t.pod = ?"
        params.append(pod)
    if user["role"] in CROSS_FUNCTIONAL_ROLES and not scope_all:
        query += " AND t.team = ?"
        params.append(user["role"])
    else:
        team = request.args.get("team")
        if team in CROSS_FUNCTIONAL_ROLES:
            query += " AND t.team = ?"
            params.append(team)
    status = request.args.get("status")
    if status in TASK_STATUSES:
        query += " AND t.status = ?"
        params.append(status)
    query += " ORDER BY t.updated_at DESC"
    rows = db.execute(query, params).fetchall()
    return jsonify([task_to_dict(r) for r in rows])


@app.route("/api/assignable_users", methods=["GET"])
@login_required()
def get_assignable_users():
    """Everyone a Team Task can actually be assigned to within one POD: every role
    except admin/pod_head/management, who run the POD rather than execute
    follow-ups. Used to populate the "Assigned to" picker so it's a real
    selection, not free text. A pod-scoped caller always gets their own POD; a
    podless caller (engine1_exec/super_admin) must pass ?pod=."""
    db = get_db()
    pod = query_pod_for(g.current_user)
    if pod is None:
        return jsonify([])  # podless with no ?pod= - nothing to scope to
    rows = db.execute(
        """SELECT full_name, role FROM users
           WHERE pod = ? AND role NOT IN ('admin', 'pod_head', 'management') AND full_name != ''
           ORDER BY full_name""",
        (pod,),
    ).fetchall()
    return jsonify([{"full_name": r["full_name"], "role": r["role"]} for r in rows])


# --------------------------------------------------------------------------
# Users API - a POD's admin manages only that POD's users, and can never create,
# edit, or delete a user in a different POD or a podless (engine1_exec/super_admin)
# account. super_admin has no such limits: it manages users in any POD, and is the
# only role allowed to create/edit/delete another engine1_exec or super_admin account.
# --------------------------------------------------------------------------
POD_USER_ROLES = tuple(r for r in VALID_ROLES if r not in PODLESS_ROLES)


@app.route("/api/users", methods=["GET"])
@login_required(roles=("admin", "pod_head", "super_admin"))
def get_users():
    db = get_db()
    user = g.current_user
    if user["role"] == "super_admin":
        pod = request.args.get("pod")
        if pod in PODS:
            rows = db.execute(
                "SELECT id, username, role, pod, full_name, created_at FROM users WHERE pod = ? ORDER BY id",
                (pod,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, username, role, pod, full_name, created_at FROM users ORDER BY pod, id"
            ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, username, role, pod, full_name, created_at FROM users WHERE pod = ? ORDER BY id",
            (user["pod"],),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/users", methods=["POST"])
@login_required(roles=("admin", "pod_head", "super_admin"))
def create_user():
    data = request.get_json(force=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = data.get("role", "")
    full_name = data.get("full_name", "").strip() or username
    user = g.current_user

    if user["role"] == "super_admin":
        if not username or not password or role not in VALID_ROLES:
            return jsonify({"error": "username, password and a valid role are required"}), 400
        if role in PODLESS_ROLES:
            pod = None
        else:
            pod = data.get("pod")
            if pod not in PODS:
                return jsonify({"error": "pod is required (pods1/pods2/pods3) for this role"}), 400
    else:
        if not username or not password or role not in POD_USER_ROLES:
            return jsonify({"error": "username, password and a valid role are required"}), 400
        pod = user["pod"]

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        return jsonify({"error": "Username already exists"}), 409

    cur = db.execute(
        "INSERT INTO users (username, password, role, pod, full_name) VALUES (?, ?, ?, ?, ?)",
        (username, _hash(password), role, pod, full_name),
    )
    db.commit()
    row = db.execute(
        "SELECT id, username, role, pod, full_name, created_at FROM users WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/users/<int:user_id>", methods=["PUT"])
@login_required(roles=("admin", "pod_head", "super_admin"))
def update_user(user_id):
    data = request.get_json(force=True) or {}
    db = get_db()
    user = g.current_user
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return jsonify({"error": "User not found"}), 404
    if user["role"] != "super_admin" and row["pod"] != user["pod"]:
        return jsonify({"error": "User not found"}), 404

    role = data.get("role", row["role"])
    if user["role"] == "super_admin":
        if role not in VALID_ROLES:
            return jsonify({"error": "Invalid role"}), 400
        if role in PODLESS_ROLES:
            pod = None
        else:
            pod = data.get("pod", row["pod"]) if row["pod"] in PODS else data.get("pod")
            if pod not in PODS:
                return jsonify({"error": "pod is required (pods1/pods2/pods3) for this role"}), 400
    else:
        if role not in POD_USER_ROLES:
            return jsonify({"error": "Invalid role"}), 400
        pod = row["pod"]  # a POD's own admin can never move a user to another POD

    full_name = data.get("full_name", row["full_name"])
    password_hash = row["password"]
    if data.get("password"):
        password_hash = _hash(data["password"])

    username = row["username"]
    if user["role"] == "super_admin" and str(data.get("username", "")).strip():
        new_username = str(data["username"]).strip()
        if new_username != username:
            clash = db.execute(
                "SELECT id FROM users WHERE username = ? AND id != ?", (new_username, user_id)
            ).fetchone()
            if clash:
                return jsonify({"error": "Username already exists"}), 409
            username = new_username

    db.execute(
        "UPDATE users SET username = ?, role = ?, pod = ?, full_name = ?, password = ? WHERE id = ?",
        (username, role, pod, full_name, password_hash, user_id),
    )
    db.commit()
    row = db.execute(
        "SELECT id, username, role, pod, full_name, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return jsonify(dict(row))


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
@login_required(roles=("admin", "pod_head", "super_admin"))
def delete_user(user_id):
    if g.current_user["user_id"] == user_id:
        return jsonify({"error": "Cannot delete your own account while logged in"}), 400
    db = get_db()
    user = g.current_user
    row = db.execute("SELECT pod FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return jsonify({"error": "User not found"}), 404
    if user["role"] != "super_admin" and row["pod"] != user["pod"]:
        return jsonify({"error": "User not found"}), 404
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Config API
# --------------------------------------------------------------------------
def config_to_dict(row):
    keys = row.keys()

    def jload(col, default):
        if col not in keys:
            return default
        try:
            val = json.loads(row[col] or "null")
            return default if val is None else val
        except (TypeError, ValueError):
            return default

    return {
        "target_amount": row["target_amount"],
        "strategic_pillars": json.loads(row["strategic_pillars"]),
        "squads": json.loads(row["squads"]),
        "stages": jload("stages", list(DEFAULT_STAGES)),
        "am_targets": jload("am_targets", {}),
        "am_achievements": jload("am_achievements", {}),
        "am_recurring": jload("am_recurring", {}),
        "current_achievement": (row["current_achievement"] if "current_achievement" in keys else 0) or 0,
        "recurring_revenue": (row["recurring_revenue"] if "recurring_revenue" in keys else 0) or 0,
        "max_login_logs": (row["max_login_logs"] if "max_login_logs" in keys and row["max_login_logs"] is not None else 100) or 100,
        "updated_at": row["updated_at"],
    }


def merged_pod_config(shared, pod_row, pod):
    pod_cfg = config_to_dict(pod_row)
    return {
        **shared,
        "target_amount": pod_cfg["target_amount"],
        "am_targets": pod_cfg["am_targets"],
        "am_achievements": pod_cfg["am_achievements"],
        "am_recurring": pod_cfg["am_recurring"],
        "current_achievement": pod_cfg["current_achievement"],
        "recurring_revenue": pod_cfg["recurring_revenue"],
        "pod": pod,
        "pod_label": POD_LABELS.get(pod, ""),
    }


def resolve_config_for(db, user):
    """The config dict a request should see: one POD's figures merged with the shared
    taxonomy, or - for engine1_exec with no ?pod= - all three PODS combined (summed
    numbers, merged AM maps) plus a pods_breakdown list of each POD's own figures."""
    shared = config_to_dict(get_shared_config_row(db))
    pod = query_pod_for(user)
    if pod is not None:
        return merged_pod_config(shared, get_pod_config_row(db, pod), pod)

    breakdown = [merged_pod_config(shared, get_pod_config_row(db, p), p) for p in PODS]
    return {
        **shared,
        "target_amount": sum(b["target_amount"] for b in breakdown),
        "current_achievement": sum(b["current_achievement"] for b in breakdown),
        "recurring_revenue": sum(b["recurring_revenue"] for b in breakdown),
        "am_targets": {k: v for b in breakdown for k, v in b["am_targets"].items()},
        "am_achievements": {k: v for b in breakdown for k, v in b["am_achievements"].items()},
        "am_recurring": {k: v for b in breakdown for k, v in b["am_recurring"].items()},
        "pod": None,
        "pod_label": "All PODS (Engine 1)",
        "pods_breakdown": breakdown,
    }


@app.route("/api/config", methods=["GET"])
@login_required()
def get_config():
    return jsonify(resolve_config_for(get_db(), g.current_user))


def _update_shared_taxonomy(db, data):
    """engine1_exec/super_admin only: the Engine-1-wide Stages/Pillars/log-retention row."""
    row = get_shared_config_row(db)
    current = config_to_dict(row)
    strategic_pillars = data.get("strategic_pillars", current["strategic_pillars"])
    stages = data.get("stages", current["stages"]) or list(DEFAULT_STAGES)
    squads = data.get("squads", current["squads"])
    max_login_logs = int(data.get("max_login_logs", current["max_login_logs"]) or 100)
    max_login_logs = max(10, min(max_login_logs, 2000))
    db.execute(
        """UPDATE config SET strategic_pillars = ?, squads = ?, stages = ?,
           max_login_logs = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (json.dumps(strategic_pillars), json.dumps(squads), json.dumps(stages),
         max_login_logs, row["id"]),
    )
    trim_login_logs(db, max_login_logs)


def _update_pod_figures(db, pod, data):
    """A POD's own admin, or super_admin acting on a chosen POD: that POD's
    target/achievement/recurring figures - never the shared taxonomy."""
    row = get_pod_config_row(db, pod)
    current = config_to_dict(row)

    def int_map(src):
        return {k: int(v or 0) for k, v in (src or {}).items()}

    target_amount = int(data.get("target_amount", current["target_amount"]) or 0)
    am_targets = int_map(data.get("am_targets", current["am_targets"]))
    am_achievements = int_map(data.get("am_achievements", current["am_achievements"]))
    am_recurring = int_map(data.get("am_recurring", current["am_recurring"]))
    current_achievement = int(data.get("current_achievement", current["current_achievement"]) or 0)
    recurring_revenue = int(data.get("recurring_revenue", current["recurring_revenue"]) or 0)

    db.execute(
        """UPDATE config SET target_amount = ?, am_targets = ?, am_achievements = ?,
           am_recurring = ?, current_achievement = ?, recurring_revenue = ?,
           updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (target_amount, json.dumps(am_targets), json.dumps(am_achievements),
         json.dumps(am_recurring), current_achievement, recurring_revenue, row["id"]),
    )


CONFIG_MONEY_KEYS = ("target_amount", "am_targets", "am_achievements", "am_recurring",
                     "current_achievement", "recurring_revenue")
CONFIG_TAXONOMY_KEYS = ("strategic_pillars", "stages", "squads", "max_login_logs")


@app.route("/api/config", methods=["PUT"])
@login_required(roles=("admin", "pod_head", "engine1_exec", "super_admin"))
def update_config():
    data = request.get_json(force=True) or {}
    db = get_db()
    user = g.current_user

    if user["role"] == "engine1_exec":
        # Engine1 exec owns only the Engine-1-wide shared taxonomy - never any POD's
        # own target/achievement figures.
        _update_shared_taxonomy(db, data)
        db.commit()
        return jsonify(resolve_config_for(db, user))

    if user["role"] == "super_admin":
        # super_admin can touch both: the shared taxonomy if those fields are present,
        # and/or a chosen POD's own figures if those fields are present (that POD must
        # be selected via ?pod=, the header POD selector, or a `pod` field in the body).
        if any(k in data for k in CONFIG_TAXONOMY_KEYS):
            _update_shared_taxonomy(db, data)
        pod = None
        if any(k in data for k in CONFIG_MONEY_KEYS):
            pod = mutation_pod_for(user, data)
            if not pod:
                return jsonify({"error": "Select a POD (via the header POD selector) to "
                                         "edit its target/achievement figures"}), 400
            _update_pod_figures(db, pod, data)
        db.commit()
        if pod:
            shared = config_to_dict(get_shared_config_row(db))
            return jsonify(merged_pod_config(shared, get_pod_config_row(db, pod), pod))
        return jsonify(resolve_config_for(db, user))

    # admin: only their own POD's target/achievement figures - never the shared taxonomy.
    _update_pod_figures(db, user["pod"], data)
    db.commit()
    return jsonify(resolve_config_for(db, user))


# --------------------------------------------------------------------------
# XLSX Backup: full export / import  (ADMIN only)
# --------------------------------------------------------------------------
DEAL_HEADERS = [
    "ID", "Opportunity", "Customer", "Account Manager", "Squad", "Strategic Pillar",
    "TCV (IDR)", "Rev 2026 (IDR)", "Target Quarter", "Stage", "Progress (%)",
    "Blocked", "Blocker", "Closed Lost", "Closed Lost Reason", "Strategy", "Next Actions",
]
# Config keys stored as JSON (lists/dicts) vs plain integers
CONFIG_JSON_KEYS = ["strategic_pillars", "squads", "stages",
                    "am_targets", "am_achievements", "am_recurring"]
CONFIG_INT_KEYS = ["target_amount", "current_achievement", "recurring_revenue"]


def actions_to_text(actions):
    """[{action,done,due}] (legacy Next Actions) or [{text,date,end,status}] (Timeline
    Items) -> '[x] text @2026-08-15' lines (human readable) for the XLSX backup."""
    lines = []
    for a in actions or []:
        if "text" in a:  # Timeline Items shape
            mark = "[x]" if a.get("status") == "done" else "[ ]"
            label = a.get("text", "")
            when = a.get("date", "")
            if a.get("end"):
                when = f"{when}..{a['end']}" if when else f"..{a['end']}"
        else:  # legacy {action,done,due} shape
            mark = "[x]" if a.get("done") else "[ ]"
            label = a.get("action", "")
            when = a.get("due", "")
        due = f" @{when}" if when else ""
        lines.append(f"{mark} {label}{due}")
    return "\n".join(lines)


def text_to_actions(text):
    actions = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        done = False
        if line.lower().startswith("[x]"):
            done, line = True, line[3:].strip()
        elif line.startswith("[ ]") or line.startswith("[]"):
            line = line.split("]", 1)[1].strip()
        due = ""
        if "@" in line:
            head, _, tail = line.rpartition("@")
            candidate = tail.strip()
            # only treat as a date if it looks like one
            if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
                due, line = candidate, head.strip()
        if line:
            actions.append({"action": line, "done": done, "due": due})
    return actions


@app.route("/api/export/xlsx", methods=["GET"])
@login_required(roles=("admin", "pod_head", "super_admin"))
def export_xlsx():
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        return jsonify({"error": "openpyxl is not installed on the server. "
                                 "Run: pip install --user openpyxl, then reload the web app."}), 500

    db = get_db()
    pod = mutation_pod_for(g.current_user)
    if not pod:
        return jsonify({"error": "Select a POD (via ?pod=) to export its data backup"}), 400
    deals = [deal_to_dict(r) for r in
             db.execute("SELECT * FROM deals WHERE pod = ? ORDER BY id", (pod,)).fetchall()]
    shared = config_to_dict(get_shared_config_row(db))
    config = merged_pod_config(shared, get_pod_config_row(db, pod), pod)
    users = db.execute(
        "SELECT username, full_name, role FROM users WHERE pod = ? ORDER BY id", (pod,)
    ).fetchall()

    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="1A73E8")
    head_font = Font(color="FFFFFF", bold=True)

    def style_header(ws, ncols):
        for c in range(1, ncols + 1):
            cell = ws.cell(row=1, column=c)
            cell.fill, cell.font = head_fill, head_font
        ws.freeze_panes = "A2"

    # --- Opportunities ---
    ws = wb.active
    ws.title = "Opportunities"
    ws.append(DEAL_HEADERS)
    for d in deals:
        ws.append([
            d["id"], d["deal_name"], d["customer"], d["assigned_am"], d["squad"],
            d["strategic_pillar"], d["estimated_value"], d["revenue_2026"],
            d["target_quarter"], d["stage"], d["progress"],
            "Yes" if d["is_blocked"] else "No", d["blocker_description"],
            "Yes" if d["is_closed_lost"] else "No", d["closed_lost_reason"],
            d["strategy"], actions_to_text(d["next_actions"]),
        ])
    style_header(ws, len(DEAL_HEADERS))
    for col, width in zip("ABCDEFGHIJKLMNOPQ",
                          [6, 38, 28, 18, 16, 22, 16, 16, 14, 14, 11, 9, 26, 12, 26, 50, 50]):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2):
        row[15].alignment = Alignment(wrap_text=True, vertical="top")  # Strategy
        row[16].alignment = Alignment(wrap_text=True, vertical="top")  # Next Actions

    # --- Execution Framework (8 Enterprise Proofs), one row per deal per proof ---
    fw = wb.create_sheet("Execution Framework")
    fw.append(["Deal ID", "Opportunity", "Account Manager", "#", "Proof",
               "Status", "Target Date", "Evidence (one per line: YYYY-MM-DD | what was done)"])
    for d in deals:
        p = d["proofs"]
        for idx, key in enumerate(PROOF_KEYS, start=1):
            item = p.get(key, {})
            evidence = "\n".join(
                (f"{e['date']} | {e['text']}" if e.get("date") else e["text"])
                for e in item.get("entries", [])
            )
            fw.append([d["id"], d["deal_name"], d["assigned_am"], idx, PROOF_NAMES[key],
                       item.get("status", "not_started"), item.get("due", ""), evidence])
    style_header(fw, 8)
    for col, width in zip("ABCDEFGH", [8, 34, 18, 5, 24, 14, 14, 52]):
        fw.column_dimensions[col].width = width
    for row in fw.iter_rows(min_row=2):
        row[7].alignment = Alignment(wrap_text=True, vertical="top")

    # --- Config ---
    cfg = wb.create_sheet("Config")
    cfg.append(["Key", "Value"])
    for k in CONFIG_INT_KEYS:
        cfg.append([k, config.get(k, 0)])
    for k in CONFIG_JSON_KEYS:
        cfg.append([k, json.dumps(config.get(k))])
    style_header(cfg, 2)
    cfg.column_dimensions["A"].width = 24
    cfg.column_dimensions["B"].width = 80

    # --- Users (no passwords are ever exported) ---
    us = wb.create_sheet("Users")
    us.append(["Username", "Full Name", "Role"])
    for u in users:
        us.append([u["username"], u["full_name"], u["role"]])
    style_header(us, 3)
    for col, width in zip("ABC", [20, 28, 20]):
        us.column_dimensions[col].width = width

    # --- Readme ---
    rm = wb.create_sheet("READ ME")
    for line in [
        [f"Engine 1 Command Center - data backup for {POD_LABELS.get(pod, pod)}"],
        [f"Exported: {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
        [""],
        ["This file is both a BACKUP and an IMPORT TEMPLATE."],
        ["Re-upload it via Settings > Data Backup > Import to restore or migrate."],
        [""],
        ["Opportunities sheet:"],
        ["  - Leave ID as-is to update an existing opportunity."],
        ["  - Clear the ID to create a NEW opportunity on import."],
        ["  - Next Actions format, one per line:  [x] done action @2026-08-15"],
        ["                                        [ ] pending action"],
        ["    The @YYYY-MM-DD part is the target date and is optional."],
        ["  - Blocked column accepts Yes/No."],
        [""],
        ["Execution Framework sheet (the 8 Enterprise Proofs):"],
        ["  - One row per opportunity per proof. Keep Deal ID and Proof name unchanged."],
        ["  - Status accepts: not_started / in_progress / done / na"],
        ["  - Target Date is YYYY-MM-DD and shows up on the Calendar."],
        ["  - Evidence: one item per line. Optional date prefix, e.g."],
        ["        2026-08-01 | Workshop held with DAOP ops team"],
        ["        Budget letter received"],
        ["  - Stage is auto-derived from how far the framework has progressed"],
        ["    (configurable under Configuration > Stage automation)."],
        [""],
        ["Config sheet: JSON values - keep the JSON syntax valid."],
        ["Users sheet: passwords are never exported. New usernames on import are"],
        ["  created with the temporary password 'changeme123' - reset them right away."],
    ]:
        rm.append(line)
    rm.column_dimensions["A"].width = 95

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"deal_tracker_backup_{date.today().isoformat()}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=filename,
    )


@app.route("/api/import/xlsx", methods=["POST"])
@login_required(roles=("admin", "pod_head", "super_admin"))
def import_xlsx():
    try:
        from openpyxl import load_workbook
    except ImportError:
        return jsonify({"error": "openpyxl is not installed on the server. "
                                 "Run: pip install --user openpyxl, then reload the web app."}), 500

    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "No file uploaded"}), 400
    replace_all = str(request.form.get("replace_all", "")).lower() in ("1", "true", "yes")

    try:
        wb = load_workbook(upload, data_only=True)
    except Exception as exc:
        return jsonify({"error": f"Could not read this file as .xlsx ({exc})"}), 400

    db = get_db()
    pod = mutation_pod_for(g.current_user)
    if not pod:
        return jsonify({"error": "Select a POD (via the header POD selector) to import into"}), 400
    summary = {"updated": 0, "created": 0, "deleted": 0, "users_created": 0,
               "proofs_updated": 0, "config_updated": False}

    # ---------------- Opportunities (always scoped to the importing admin's POD;
    # an ID that belongs to another POD - or doesn't exist - is treated as new so a
    # backup can never overwrite or leak another POD's data) ----------------
    if "Opportunities" in wb.sheetnames:
        ws = wb["Opportunities"]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        seen_ids = set()

        def s(v):
            return "" if v is None else str(v).strip()

        def n(v):
            if v is None or str(v).strip() == "":
                return 0
            try:
                return int(float(str(v).replace(".", "").replace(",", "")
                                 if isinstance(v, str) else v))
            except (TypeError, ValueError):
                return 0

        for r in rows:
            r = list(r) + [None] * (len(DEAL_HEADERS) - len(r))
            name = s(r[1])
            if not name:
                continue  # skip blank lines
            deal_id = r[0]
            payload = (
                name, s(r[2]), s(r[3]), s(r[4]), s(r[5]), n(r[6]), n(r[7]), s(r[8]),
                s(r[9]) or "Prospecting", max(0, min(100, n(r[10]))),
                1 if s(r[11]).lower() in ("yes", "true", "1") else 0,
                s(r[12]),
                1 if s(r[13]).lower() in ("yes", "true", "1") else 0,
                s(r[14]), s(r[15]), json.dumps(text_to_actions(r[16])),
            )
            existing = None
            if deal_id not in (None, ""):
                try:
                    existing = db.execute("SELECT id FROM deals WHERE id = ? AND pod = ?",
                                          (int(deal_id), pod)).fetchone()
                except (TypeError, ValueError):
                    existing = None
            if existing:
                db.execute(
                    """UPDATE deals SET deal_name=?, customer=?, assigned_am=?, squad=?,
                       strategic_pillar=?, estimated_value=?, revenue_2026=?, target_quarter=?,
                       stage=?, progress=?, is_blocked=?, blocker_description=?,
                       is_closed_lost=?, closed_lost_reason=?, strategy=?,
                       next_actions=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND pod=?""",
                    payload + (int(deal_id), pod),
                )
                seen_ids.add(int(deal_id))
                summary["updated"] += 1
            else:
                cur = db.execute(
                    """INSERT INTO deals (pod, deal_name, customer, assigned_am, squad,
                       strategic_pillar, estimated_value, revenue_2026, target_quarter, stage,
                       progress, is_blocked, blocker_description, is_closed_lost,
                       closed_lost_reason, strategy, next_actions,
                       updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (pod,) + payload,
                )
                seen_ids.add(cur.lastrowid)
                summary["created"] += 1

        if replace_all and seen_ids:
            placeholders = ",".join("?" * len(seen_ids))
            cur = db.execute(
                f"DELETE FROM deals WHERE pod = ? AND id NOT IN ({placeholders})",
                (pod,) + tuple(seen_ids))
            summary["deleted"] = cur.rowcount

    # ---------------- Execution Framework (8 Proofs) ----------------
    if "Execution Framework" in wb.sheetnames:
        by_deal = {}
        for r in wb["Execution Framework"].iter_rows(min_row=2, values_only=True):
            r = list(r) + [None] * (8 - len(r))
            deal_id, _, _, _, proof_name, status, due, notes = r[:8]
            if deal_id in (None, "") or not proof_name:
                continue
            try:
                deal_id = int(deal_id)
            except (TypeError, ValueError):
                continue
            # match by proof name (or key), case-insensitive
            label = str(proof_name).strip().lower()
            key = next((k for k in PROOF_KEYS
                        if k == label or PROOF_NAMES[k].lower() == label), None)
            if not key:
                continue
            due_txt = ""
            if due not in (None, ""):
                due_txt = due.strftime("%Y-%m-%d") if hasattr(due, "strftime") else str(due).strip()[:10]
            # Evidence: one entry per line, optional "YYYY-MM-DD | text" prefix
            entries = []
            for line in str(notes or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                date_part = ""
                if "|" in line:
                    head, _, tail = line.partition("|")
                    head = head.strip()
                    if len(head) == 10 and head[4] == "-" and head[7] == "-":
                        date_part, line = head, tail.strip()
                if line:
                    entries.append({"text": line, "date": date_part})
            by_deal.setdefault(deal_id, {})[key] = {
                "status": str(status or "not_started").strip().lower().replace(" ", "_"),
                "due": due_txt,
                "entries": entries,
            }
        for deal_id, proofs in by_deal.items():
            existing = db.execute("SELECT proofs FROM deals WHERE id = ? AND pod = ?",
                                  (deal_id, pod)).fetchone()
            if not existing:
                continue
            merged = normalize_proofs(json.loads(existing["proofs"] or "{}"))
            merged.update(normalize_proofs(proofs))
            db.execute("UPDATE deals SET proofs = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND pod = ?",
                       (json.dumps(merged), deal_id, pod))
        summary["proofs_updated"] = len(by_deal)

    # ---------------- Config (this POD's own target/achievement figures only -
    # the shared Stages/Pillars taxonomy is engine1_exec's to manage, not backed up
    # or restored per-POD here) ----------------
    if "Config" in wb.sheetnames:
        pod_only_int_keys = ("target_amount", "current_achievement", "recurring_revenue")
        pod_only_json_keys = ("am_targets", "am_achievements", "am_recurring")
        incoming = {}
        for k, v in wb["Config"].iter_rows(min_row=2, values_only=True):
            if not k:
                continue
            key = str(k).strip()
            if key in pod_only_int_keys:
                try:
                    incoming[key] = int(float(v or 0))
                except (TypeError, ValueError):
                    pass
            elif key in pod_only_json_keys:
                try:
                    incoming[key] = json.loads(v) if isinstance(v, str) else v
                except (TypeError, ValueError):
                    pass
        if incoming:
            row = get_pod_config_row(db, pod)
            merged = config_to_dict(row)
            merged.update(incoming)
            db.execute(
                """UPDATE config SET target_amount=?, am_targets=?, am_achievements=?,
                   am_recurring=?, current_achievement=?, recurring_revenue=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (int(merged["target_amount"] or 0), json.dumps(merged["am_targets"]),
                 json.dumps(merged["am_achievements"]), json.dumps(merged["am_recurring"]),
                 int(merged["current_achievement"] or 0), int(merged["recurring_revenue"] or 0),
                 row["id"]),
            )
            summary["config_updated"] = True

    # ---------------- Users (this POD only; never overwrites existing passwords,
    # never touches a user from another POD even if the username happens to match,
    # and can never create/promote an engine1_exec account) ----------------
    if "Users" in wb.sheetnames:
        for uname, fname, role in wb["Users"].iter_rows(min_row=2, values_only=True):
            username = str(uname or "").strip()
            role = str(role or "").strip()
            if not username or role not in POD_USER_ROLES:
                continue
            existing = db.execute("SELECT id, pod FROM users WHERE username = ?",
                                  (username,)).fetchone()
            if existing:
                if existing["pod"] != pod:
                    continue  # belongs to a different POD - never touched by this import
                db.execute("UPDATE users SET full_name = ?, role = ? WHERE id = ?",
                           (str(fname or "").strip() or username, role, existing["id"]))
            else:
                db.execute(
                    "INSERT INTO users (username, password, role, pod, full_name) VALUES (?,?,?,?,?)",
                    (username, _hash("changeme123"), role, pod,
                     str(fname or "").strip() or username),
                )
                summary["users_created"] += 1

    db.commit()
    return jsonify({"ok": True, "summary": summary})


# --------------------------------------------------------------------------
# Performance snapshot: import the monthly "ACH" workbook from the performance team
# --------------------------------------------------------------------------
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _num(v):
    """Excel cell -> float, tolerating blanks, text and tiny float noise."""
    if v is None or isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return 0.0 if abs(v) < 1 else float(v)
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def am_key(name):
    """Loose key so 'Ashari Asrar' matches the dashboard's 'Ashari'."""
    return " ".join(str(name or "").lower().replace(".", " ").split())


def am_matches(perf_name, dash_name):
    a, b = am_key(perf_name), am_key(dash_name)
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _parse_pods_sheet(sheet):
    """Per-AM monthly target/actual/forecast + MRC/pipeline/PO + FY summary from a
    single 'PODS (N)' sheet."""
    am_rows = []
    # Monthly triplets start at col D (index 4, 1-based); 3 cols per month.
    # Only the first block (down to its "Total" row) carries the FY summary;
    # the YTD block further down repeats the AM names with a different layout.
    for r in range(4, 40):
        first_col = str(sheet.cell(row=r, column=1).value or "").strip().lower()
        if first_col == "total":
            break
        am = sheet.cell(row=r, column=3).value
        if not am or str(am).strip().lower() in ("total", "am name"):
            continue
        monthly = []
        for mi, mname in enumerate(MONTHS):
            base = 4 + mi * 3
            monthly.append({
                "month": mname,
                "target": _num(sheet.cell(row=r, column=base).value),
                # Jan-Jun are actuals, Jul-Dec are forecast in this template
                "value": _num(sheet.cell(row=r, column=base + 1).value),
                "is_forecast": mi >= 6,
            })
        series = lambda start: [_num(sheet.cell(row=r, column=start + i).value)
                                for i in range(12)]
        am_rows.append({
            "am": str(am).strip(),
            "monthly": monthly,
            "mrc_monthly": series(41),        # AO..AZ
            "pipeline_monthly": series(55),   # BC..BN
            "po_monthly": series(69),         # BQ..CB
            "target_fy": _num(sheet.cell(row=r, column=85).value),   # CG
            "actual_ytd": _num(sheet.cell(row=r, column=86).value),  # CH
            "mrc_rest": _num(sheet.cell(row=r, column=87).value),    # CI
            "po_hand": _num(sheet.cell(row=r, column=88).value),     # CJ
            "forecast_fy": _num(sheet.cell(row=r, column=89).value),  # CK
            "gap": _num(sheet.cell(row=r, column=90).value),          # CL
            "conservative_pipeline": _num(sheet.cell(row=r, column=91).value),  # CM
            "current_pipeline": _num(sheet.cell(row=r, column=93).value),       # CO
        })
    return am_rows


def _parse_account_sheet(acc_sheet):
    """Account-level monthly revenue from the 'byAccount (BP)' sheet."""
    accounts = []
    # header row 3: month columns start at col K (11) and run while dated
    month_cols = []
    for c in range(11, acc_sheet.max_column + 1):
        h = acc_sheet.cell(row=3, column=c).value
        if hasattr(h, "strftime"):
            month_cols.append((c, h.strftime("%Y-%m")))
        elif isinstance(h, str) and h[:4].isdigit() and "-" in h:
            month_cols.append((c, h[:7]))
    for r in range(4, acc_sheet.max_row + 1):
        account = acc_sheet.cell(row=r, column=7).value
        if not account:
            continue
        months = {}
        for c, label in month_cols:
            months[label] = _num(acc_sheet.cell(row=r, column=c).value)
        if not any(months.values()):
            continue
        accounts.append({
            "pillar": str(acc_sheet.cell(row=r, column=1).value or ""),
            "revenue_category": str(acc_sheet.cell(row=r, column=4).value or ""),
            "mrc_type": str(acc_sheet.cell(row=r, column=5).value or ""),
            "account": str(account).strip(),
            "pods": str(acc_sheet.cell(row=r, column=9).value or "").strip(),
            "am": str(acc_sheet.cell(row=r, column=10).value or "").strip(),
            "months": months,
        })
    return accounts


_HEAD_NUM_RE = re.compile(r"head\s*([123])\b", re.I)
_POD_SHEET_RE = re.compile(r"^pods\s*\(?\s*([123])\s*\)?", re.I)


def _head_label_to_pod(label):
    m = _HEAD_NUM_RE.search(str(label or ""))
    return f"pods{m.group(1)}" if m else None


def parse_performance_workbook(wb, pod=None):
    """Read a 'PODS (N)' sheet and the 'byAccount (BP)' sheet into plain dicts -
    the monthly ACH workbook format, for a single-POD import.

    Accepts both a genuine single-POD file (one 'PODS (N)' sheet) and the
    Engine-1-wide file that carries all three PODS' sheets at once: when `pod`
    is given, the sheet matching that POD's number is preferred over just
    taking the first 'PODS'-prefixed sheet, and account rows tagged for a
    different POD (via the 'Business Engine 1 Head N' label) are filtered out
    so a POD's own admin uploading the combined file only ever gets their own
    POD's numbers - the same routing `parse_all_pods_workbook` uses, applied to
    a single POD instead of all three."""
    sheet = None
    if pod:
        for nm in wb.sheetnames:
            m = _POD_SHEET_RE.match(nm.strip())
            if m and f"pods{m.group(1)}" == pod:
                sheet = wb[nm]
                break
    if sheet is None:
        for nm in wb.sheetnames:
            if nm.strip().lower().startswith("pods"):
                sheet = wb[nm]
                break
    am_rows = _parse_pods_sheet(sheet) if sheet is not None else []

    acc_sheet = None
    for nm in wb.sheetnames:
        if "account" in nm.strip().lower():
            acc_sheet = wb[nm]
            break
    accounts = _parse_account_sheet(acc_sheet) if acc_sheet is not None else []
    if pod:
        # Keep rows tagged for this POD, plus any row without a recognisable
        # Head-N label at all (a genuine single-POD file's account sheet may
        # not carry that column) - only drop rows clearly tagged for another POD.
        accounts = [a for a in accounts if _head_label_to_pod(a.get("pods")) in (pod, None)]
    return am_rows, accounts


def parse_all_pods_workbook(wb):
    """Read the Engine-1-wide monthly ACH workbook: one 'PODS (N)' sheet per POD
    (N=1,2,3) plus a single shared 'byAccount (BP)' sheet whose rows are tagged
    'Business Engine 1 Head N' - the same N as the sheet name. Returns
    {pod: {"am_rows": [...], "accounts": [...]}} for whichever PODS are present."""
    acc_sheet = None
    for nm in wb.sheetnames:
        if "account" in nm.strip().lower():
            acc_sheet = wb[nm]
            break
    all_accounts = _parse_account_sheet(acc_sheet) if acc_sheet is not None else []

    result = {}
    for nm in wb.sheetnames:
        m = _POD_SHEET_RE.match(nm.strip())
        if not m:
            continue
        pod = f"pods{m.group(1)}"
        am_rows = _parse_pods_sheet(wb[nm])
        if not am_rows:
            continue
        accounts = [a for a in all_accounts if _head_label_to_pod(a.get("pods")) == pod]
        result[pod] = {"am_rows": am_rows, "accounts": accounts}
    return result


@app.route("/api/performance", methods=["GET"])
@login_required()
def get_performance():
    db = get_db()
    pod = query_pod_for(g.current_user)
    if pod is not None:
        row = db.execute(
            "SELECT * FROM performance WHERE pod = ? ORDER BY id DESC LIMIT 1", (pod,)
        ).fetchone()
        if not row:
            return jsonify({"available": False})
        return jsonify({
            "available": True,
            "label": row["label"],
            "source_file": row["source_file"],
            "uploaded_at": row["uploaded_at"],
            "am_summary": json.loads(row["am_summary"] or "[]"),
            "accounts": json.loads(row["accounts"] or "[]"),
            "pod": pod,
            "pod_label": POD_LABELS.get(pod, ""),
        })

    # Podless with no ?pod= selected: combine the latest snapshot from each of the
    # three PODS into one Engine-1-wide view - concatenate each POD's AM rows and
    # account rows (tagged with which POD they came from) rather than re-summing
    # per AM, since an AM belongs to exactly one POD.
    am_summary, accounts, uploaded_ats, pods_included = [], [], [], []
    for p in PODS:
        row = db.execute(
            "SELECT * FROM performance WHERE pod = ? ORDER BY id DESC LIMIT 1", (p,)
        ).fetchone()
        if not row:
            continue
        pods_included.append(p)
        if row["uploaded_at"]:
            uploaded_ats.append(row["uploaded_at"])
        for a in json.loads(row["am_summary"] or "[]"):
            a = dict(a)
            a["pod"] = p
            a["pod_label"] = POD_LABELS.get(p, "")
            am_summary.append(a)
        for acc in json.loads(row["accounts"] or "[]"):
            acc = dict(acc)
            acc["pod"] = p
            accounts.append(acc)
    if not pods_included:
        return jsonify({"available": False})
    return jsonify({
        "available": True,
        "label": "All PODS (Engine 1)",
        "source_file": "",
        "uploaded_at": max(uploaded_ats) if uploaded_ats else "",
        "am_summary": am_summary,
        "accounts": accounts,
        "pod": None,
        "pod_label": "All PODS (Engine 1)",
        "pods_included": pods_included,
    })


@app.route("/api/config/am_targets/import", methods=["POST"])
@login_required(roles=("admin", "pod_head", "super_admin"))
def import_am_targets():
    """Bulk-update AM Target/YTD Actual/Recurring FY26 from the same monthly
    'PODS (2)' performance workbook already used for Performance import, instead
    of retyping each figure by hand in Settings -> Account Manager Targets."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return jsonify({"error": "openpyxl is not installed on the server. "
                                 "Run: pip install --user openpyxl, then reload."}), 500

    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "No file uploaded"}), 400

    try:
        wb = load_workbook(upload, data_only=True)
    except Exception as exc:
        return jsonify({"error": f"Could not read this file as .xlsx ({exc})"}), 400

    db = get_db()
    pod = mutation_pod_for(g.current_user)
    if not pod:
        return jsonify({"error": "Select a POD (via the header POD selector) to import into"}), 400

    am_rows, _accounts = parse_performance_workbook(wb, pod)
    if not am_rows:
        return jsonify({"error": "No recognisable per-AM figures found. Expected the same "
                                 "workbook used for Performance import, with a sheet named "
                                 "like 'PODS (2)' (or the Engine-1-wide file, matched to your "
                                 "own POD)."}), 400

    row = get_pod_config_row(db, pod)
    cfg = config_to_dict(row)
    am_targets = dict(cfg["am_targets"])
    am_achievements = dict(cfg["am_achievements"])
    am_recurring = dict(cfg["am_recurring"])

    known_ams = [u["full_name"] for u in db.execute(
        "SELECT full_name FROM users WHERE role = 'account_manager' AND pod = ?", (pod,)).fetchall()]

    updated, unmatched = [], []
    for r in am_rows:
        dash_am = next((am for am in known_ams if am_matches(r["am"], am)), None)
        if not dash_am:
            unmatched.append(r["am"])
            continue
        am_targets[dash_am] = int(r["target_fy"] or 0)
        am_achievements[dash_am] = int(r["actual_ytd"] or 0)
        am_recurring[dash_am] = int(r["mrc_rest"] or 0)
        updated.append(dash_am)

    db.execute(
        """UPDATE config SET am_targets = ?, am_achievements = ?, am_recurring = ?,
           updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (json.dumps(am_targets), json.dumps(am_achievements), json.dumps(am_recurring), row["id"]),
    )
    db.commit()
    shared = config_to_dict(get_shared_config_row(db))
    merged = merged_pod_config(shared, get_pod_config_row(db, pod), pod)
    return jsonify({"ok": True, "updated": updated, "unmatched": unmatched, "config": merged})


@app.route("/api/config/am_targets/import_all_pods", methods=["POST"])
@login_required(roles=("super_admin",))
def import_all_pods_targets():
    """One-shot import of the Engine-1-wide monthly ACH workbook: a single upload
    with a 'PODS (1)'/'PODS (2)'/'PODS (3)' sheet each, plus a shared 'byAccount
    (BP)' sheet. For every POD found, this is the authoritative source for that
    POD's Performance snapshot, per-AM Target/YTD Actual/Recurring, and the POD's
    own top-line Full-Year Target/Achieved/Recurring (set to the sum of that
    POD's AM figures) - unlike the single-POD imports above, which only touch
    the per-AM table and leave the top-line rollups untouched."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return jsonify({"error": "openpyxl is not installed on the server. "
                                 "Run: pip install --user openpyxl, then reload."}), 500

    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "No file uploaded"}), 400
    label = (request.form.get("label") or "").strip()

    try:
        wb = load_workbook(upload, data_only=True)
    except Exception as exc:
        return jsonify({"error": f"Could not read this file as .xlsx ({exc})"}), 400

    per_pod = parse_all_pods_workbook(wb)
    if not per_pod:
        return jsonify({"error": "No recognisable data. Expected sheets named like "
                                 "'PODS (1)', 'PODS (2)', 'PODS (3)' and 'byAccount (BP)'."}), 400

    db = get_db()
    results = {}
    for pod, data in per_pod.items():
        am_rows, accounts = data["am_rows"], data["accounts"]

        db.execute(
            "INSERT INTO performance (pod, label, source_file, am_summary, accounts) VALUES (?, ?, ?, ?, ?)",
            (pod, label or datetime.now().strftime("%b %Y"),
             getattr(upload, "filename", "") or "",
             json.dumps(am_rows), json.dumps(accounts)),
        )
        db.execute("""DELETE FROM performance WHERE pod = ? AND id NOT IN
                      (SELECT id FROM performance WHERE pod = ? ORDER BY id DESC LIMIT 12)""",
                   (pod, pod))

        row = get_pod_config_row(db, pod)
        cfg = config_to_dict(row)
        am_targets = dict(cfg["am_targets"])
        am_achievements = dict(cfg["am_achievements"])
        am_recurring = dict(cfg["am_recurring"])

        known_ams = [u["full_name"] for u in db.execute(
            "SELECT full_name FROM users WHERE role = 'account_manager' AND pod = ?", (pod,)).fetchall()]

        updated, unmatched = [], []
        for r in am_rows:
            dash_am = next((am for am in known_ams if am_matches(r["am"], am)), None)
            if not dash_am:
                unmatched.append(r["am"])
                continue
            am_targets[dash_am] = int(r["target_fy"] or 0)
            am_achievements[dash_am] = int(r["actual_ytd"] or 0)
            am_recurring[dash_am] = int(r["mrc_rest"] or 0)
            updated.append(dash_am)

        db.execute(
            """UPDATE config SET target_amount = ?, current_achievement = ?, recurring_revenue = ?,
               am_targets = ?, am_achievements = ?, am_recurring = ?,
               updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (sum(am_targets.values()), sum(am_achievements.values()), sum(am_recurring.values()),
             json.dumps(am_targets), json.dumps(am_achievements), json.dumps(am_recurring), row["id"]),
        )
        results[pod] = {
            "pod_label": POD_LABELS.get(pod, pod),
            "updated": updated, "unmatched": unmatched,
            "am_count": len(am_rows), "account_rows": len(accounts),
        }

    db.commit()
    return jsonify({"ok": True, "pods": results})


# --------------------------------------------------------------------------
# One-off bulk import from a "Sales Activity Tracker" workbook (the same
# layout /api/export/tracker_xlsx produces: one row per activity, with its
# own "PODS " column). Unlike every other import, a single upload can create
# opportunities in more than one POD at once - so this is super_admin only,
# never a POD's own admin.
# --------------------------------------------------------------------------
_POD_LABEL_TO_KEY = {v.strip().lower(): k for k, v in POD_LABELS.items()}
_TRACKER_STATUS_TO_ENTRY = {"not started": "not_started", "in progress": "in_progress", "done": "done"}


def _tracker_cell_date(v):
    if v is None or v == "":
        return ""
    return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v).strip()[:10]


def _tracker_cell_int(v):
    if v is None or v == "":
        return 0
    try:
        return int(float(str(v).replace(",", "")) if isinstance(v, str) else v)
    except (TypeError, ValueError):
        return 0


def _tracker_proof_key(pillar_cell):
    """'1. Proof of Qualification' -> 'qualification' (matches by name, ignoring
    the leading number, so a mislabeled/renumbered prefix still matches)."""
    label = str(pillar_cell or "").strip()
    if "." in label[:3]:
        label = label.split(".", 1)[1].strip()
    label = label.lower()
    return next((k for k in PROOF_KEYS if PROOF_NAMES[k].lower() == label), None)


def _ensure_am_user(db, pod, full_name):
    """Make sure a matching account_manager user exists for this POD so the AM
    shows up in filters/targets right away, without ever touching an existing
    account. Returns True if a new one was created."""
    full_name = full_name.strip()
    if not full_name:
        return False
    existing = db.execute(
        "SELECT id FROM users WHERE pod = ? AND role = 'account_manager' AND full_name = ?",
        (pod, full_name),
    ).fetchone()
    if existing:
        return False
    base = "".join(ch for ch in full_name.lower() if ch.isalnum())[:20] or "am"
    candidate, i = base, 1
    while db.execute("SELECT 1 FROM users WHERE username = ?", (candidate,)).fetchone():
        i += 1
        candidate = f"{base}{i}"
    db.execute(
        "INSERT INTO users (username, password, role, pod, full_name) VALUES (?, ?, 'account_manager', ?, ?)",
        (candidate, _hash("changeme123"), pod, full_name),
    )
    return True


@app.route("/api/import/tracker_xlsx", methods=["POST"])
@login_required(roles=("super_admin",))
def import_tracker_xlsx():
    """Bulk-create opportunities from a Sales Activity Tracker workbook (one
    row per activity, grouped back into deals + 8-proof execution framework
    entries). Each row's own "PODS " column decides which POD it lands in -
    this is the only import that can write into more than one POD at once,
    which is why it's restricted to super_admin."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return jsonify({"error": "openpyxl is not installed on the server. "
                                 "Run: pip install --user openpyxl, then reload."}), 500

    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "No file uploaded"}), 400

    try:
        wb = load_workbook(upload, data_only=True)
    except Exception as exc:
        return jsonify({"error": f"Could not read this file as .xlsx ({exc})"}), 400

    sheet_name = "Tracker" if "Tracker" in wb.sheetnames else wb.sheetnames[0]
    ws = wb[sheet_name]

    db = get_db()
    shared = config_to_dict(get_shared_config_row(db))
    default_pillar = (shared["strategic_pillars"] or [""])[0]

    groups, order = {}, []
    skipped_no_pod = 0
    skipped_pod_labels = set()

    for raw in ws.iter_rows(min_row=2, values_only=True):
        r = list(raw) + [None] * (14 - len(raw))
        (_no, pod_label, opp, customer, am, tcv, rev26, quarter,
         pillar, activity, status, due, completed, notes) = r[:14]
        if not opp:
            continue
        if not pod_label or not str(pod_label).strip():
            skipped_no_pod += 1
            continue
        pod_key = _POD_LABEL_TO_KEY.get(str(pod_label).strip().lower())
        if not pod_key:
            skipped_pod_labels.add(str(pod_label).strip())
            continue

        key = (pod_key, str(opp).strip(), str(customer or "").strip())
        if key not in groups:
            groups[key] = {
                "pod": pod_key,
                "deal_name": str(opp).strip(),
                "customer": str(customer or "").strip(),
                "assigned_am": str(am or "").strip(),
                "estimated_value": _tracker_cell_int(tcv),
                "revenue_2026": _tracker_cell_int(rev26),
                "target_quarter": str(quarter or "").strip(),
                "entries": [],
            }
            order.append(key)

        proof_key = _tracker_proof_key(pillar)
        due_txt = _tracker_cell_date(due)
        completed_txt = _tracker_cell_date(completed)
        status_key = _TRACKER_STATUS_TO_ENTRY.get(str(status or "").strip().lower(), "not_started")
        text = str(activity or "").strip()
        if notes and str(notes).strip():
            text = f"{text} — {str(notes).strip()}" if text else str(notes).strip()
        date_val = completed_txt if (status_key == "done" and completed_txt) else due_txt
        groups[key]["entries"].append((proof_key, {
            "text": text or "(activity to be defined)", "date": date_val, "end": "", "status": status_key,
        }))

    created = 0
    ams_created = 0
    pods_touched = set()
    for key in order:
        g = groups[key]
        proofs = {k: {"status": "not_started", "due": "", "entries": []} for k in PROOF_KEYS}
        for proof_key, entry in g["entries"]:
            if proof_key:
                proofs[proof_key]["entries"].append(entry)
        for pk in PROOF_KEYS:
            entries = proofs[pk]["entries"]
            if not entries:
                status = "not_started"
            elif all(e["status"] == "done" for e in entries):
                status = "done"
            elif any(e["status"] in ("done", "in_progress") for e in entries):
                status = "in_progress"
            else:
                status = "not_started"
            proofs[pk]["status"] = status

        db.execute(
            """INSERT INTO deals
               (pod, deal_name, customer, assigned_am, squad, strategic_pillar, estimated_value,
                revenue_2026, target_quarter, stage, progress, is_blocked, blocker_description,
                is_closed_lost, closed_lost_reason,
                next_actions, strategy, proofs, expected_po_date, expected_revenue_date, updated_at)
               VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, 'Prospecting', 0, 0, '', 0, '', '[]', '', ?, '', '',
                       CURRENT_TIMESTAMP)""",
            (g["pod"], g["deal_name"], g["customer"], g["assigned_am"], default_pillar,
             g["estimated_value"], g["revenue_2026"], g["target_quarter"], json.dumps(proofs)),
        )
        created += 1
        pods_touched.add(g["pod"])
        if _ensure_am_user(db, g["pod"], g["assigned_am"]):
            ams_created += 1

    db.commit()
    return jsonify({
        "ok": True,
        "created": created,
        "ams_created": ams_created,
        "pods_touched": sorted(pods_touched),
        "skipped_no_pod": skipped_no_pod,
        "skipped_pod_labels": sorted(skipped_pod_labels),
        "note": (f"Every imported deal defaulted to Strategic Pillar '{default_pillar}' and Stage "
                 "'Prospecting' - this workbook format doesn't carry either, so review and correct "
                 "them per opportunity.") if created else None,
    })


@app.route("/api/performance/import", methods=["POST"])
@login_required(roles=("admin", "pod_head", "super_admin"))
def import_performance():
    try:
        from openpyxl import load_workbook
    except ImportError:
        return jsonify({"error": "openpyxl is not installed on the server. "
                                 "Run: pip install --user openpyxl, then reload."}), 500

    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "No file uploaded"}), 400
    label = (request.form.get("label") or "").strip()

    try:
        wb = load_workbook(upload, data_only=True)
    except Exception as exc:
        return jsonify({"error": f"Could not read this file as .xlsx ({exc})"}), 400

    db = get_db()
    pod = mutation_pod_for(g.current_user)
    if not pod:
        return jsonify({"error": "Select a POD (via the header POD selector) to import into"}), 400

    am_rows, accounts = parse_performance_workbook(wb, pod)
    if not am_rows and not accounts:
        return jsonify({"error": "No recognisable data. Expected a sheet named like "
                                 "'PODS (2)' and one like 'byAccount (BP)' (or the Engine-1-wide "
                                 "file, matched to your own POD)."}), 400
    db.execute(
        "INSERT INTO performance (pod, label, source_file, am_summary, accounts) VALUES (?, ?, ?, ?, ?)",
        (pod, label or datetime.now().strftime("%b %Y"),
         getattr(upload, "filename", "") or "",
         json.dumps(am_rows), json.dumps(accounts)),
    )
    # keep the last 12 snapshots for this POD
    db.execute("""DELETE FROM performance WHERE pod = ? AND id NOT IN
                  (SELECT id FROM performance WHERE pod = ? ORDER BY id DESC LIMIT 12)""",
               (pod, pod))
    db.commit()
    return jsonify({"ok": True, "am_count": len(am_rows), "account_rows": len(accounts)})


def _perf_am_matches(a, b):
    """Loose match: exact, or one name is a prefix of the other (mirrors the frontend)."""
    an = " ".join(str(a or "").lower().split())
    bn = " ".join(str(b or "").lower().split())
    if not an or not bn:
        return False
    return an == bn or an.startswith(bn) or bn.startswith(an)


def _perf_num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


@app.route("/api/performance/export_pdf", methods=["GET"])
@login_required()
def export_performance_pdf():
    """Performance summary as a PDF: team scorecard, gap coverage, per-AM
    breakdown, churn/growth and product-pillar mix — the same numbers shown
    on the Performance tab, snapshotted for sharing outside the dashboard."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    db = get_db()
    pod = query_pod_for(g.current_user)
    if pod is None:
        return jsonify({"error": "Pick a single POD to export a performance PDF for."}), 400
    row = db.execute(
        "SELECT * FROM performance WHERE pod = ? ORDER BY id DESC LIMIT 1", (pod,)
    ).fetchone()
    if not row:
        return jsonify({"error": "No performance data has been uploaded yet."}), 400

    am_summary = json.loads(row["am_summary"] or "[]")
    accounts = json.loads(row["accounts"] or "[]")
    am_filter = (request.args.get("am") or "").strip()

    am_rows = [a for a in am_summary if not am_filter or _perf_am_matches(a.get("am"), am_filter)]
    am_names = [a.get("am") for a in am_summary]

    def account_in_scope(r):
        if am_filter:
            return _perf_am_matches(r.get("am"), am_filter)
        return any(_perf_am_matches(r.get("am"), n) for n in am_names)

    acc_rows = [r for r in accounts if account_in_scope(r)]

    def s(key):
        return sum(_perf_num(a.get(key)) for a in am_rows)

    target, actual = s("target_fy"), s("actual_ytd")
    mrc, po, forecast, gap = s("mrc_rest"), s("po_hand"), s("forecast_fy"), s("gap")
    need, pipe = s("conservative_pipeline"), s("current_pipeline")
    attain = (actual / target * 100) if target else 0

    # ---- churn / growth: most recent two months across in-scope accounts ----
    month_keys = sorted({m for r in acc_rows for m in (r.get("months") or {}).keys()})
    by_acc = {}
    for r in acc_rows:
        key = r.get("account")
        entry = by_acc.setdefault(key, {"account": key, "am": r.get("am"), "months": {}})
        for m, v in (r.get("months") or {}).items():
            entry["months"][m] = entry["months"].get(m, 0) + _perf_num(v)
    deltas = []
    if len(month_keys) >= 2:
        cur_k, prev_k = month_keys[-1], month_keys[-2]
        for a in by_acc.values():
            cur, prev = a["months"].get(cur_k, 0), a["months"].get(prev_k, 0)
            if prev > 0 or cur > 0:
                deltas.append({"account": a["account"], "am": a["am"] or "",
                               "cur": cur, "prev": prev, "delta": cur - prev})
    down = sorted([d for d in deltas if d["delta"] < 0], key=lambda x: x["delta"])[:10]
    up = sorted([d for d in deltas if d["delta"] > 0], key=lambda x: x["delta"], reverse=True)[:10]

    # ---- revenue by product pillar (sum across all months in scope) ----
    by_pillar = {}
    for r in acc_rows:
        total = sum(_perf_num(v) for v in (r.get("months") or {}).values())
        p = r.get("pillar") or "-"
        by_pillar[p] = by_pillar.get(p, 0) + total
    pillar_entries = sorted(by_pillar.items(), key=lambda x: x[1], reverse=True)
    pillar_total = sum(v for _, v in pillar_entries) or 1

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        leftMargin=1.6 * cm, rightMargin=1.6 * cm,
    )
    styles = getSampleStyleSheet()
    brand = colors.HexColor("#1a73e8")
    title_style = ParagraphStyle("PTitle", parent=styles["Title"], fontSize=18, textColor=brand)
    h2_style = ParagraphStyle("PH2", parent=styles["Heading2"], fontSize=13, textColor=brand,
                              spaceBefore=14, spaceAfter=6)
    body_style = styles["BodyText"]

    def make_table(data, col_widths):
        t = Table(data, colWidths=col_widths)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), brand),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dadce0")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f3f4")]),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        return t

    story = [
        Paragraph(f"Engine 1 Command Center - {POD_LABELS.get(pod, pod)}", title_style),
        Paragraph("Performance Summary Report" + (f" — {am_filter}" if am_filter else ""), styles["Heading3"]),
        Paragraph(f"Snapshot: {row['label'] or 'untitled'} (uploaded {str(row['uploaded_at'])[:10]}) "
                  f"&middot; Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", body_style),
        Spacer(1, 0.4 * cm),
    ]

    # Gap first — the top-priority number for management.
    story.append(Paragraph("Team Scorecard &amp; Gap", h2_style))
    story.append(make_table([
        ["Metric", "Value"],
        ["Target FY26", _fmt_idr(target)],
        ["Actual YTD", _fmt_idr(actual)],
        ["MRC (rest of year)", _fmt_idr(mrc)],
        ["PO on Hand", _fmt_idr(po)],
        ["Total Forecast", _fmt_idr(forecast)],
        ["Gap to Target", _fmt_idr(gap)],
        ["Attainment", f"{attain:.1f}%"],
    ], [8 * cm, 8 * cm]))
    story.append(Spacer(1, 0.3 * cm))

    story.append(Paragraph("Pipeline Cover for the Gap", h2_style))
    cover_note = (f"Gap of {_fmt_idr(gap)} needs {_fmt_idr(need)} of pipeline (conservative 3x rule). "
                  f"Current pipeline is {_fmt_idr(pipe)} — "
                  + ("covered." if pipe >= need else f"short by {_fmt_idr(max(need - pipe, 0))}."))
    story.append(Paragraph(cover_note, body_style))
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Account Manager Performance", h2_style))
    am_table_rows = [["Account Manager", "Target FY26", "Actual YTD", "% Ach", "Forecast", "Gap", "Pipeline"]]
    for a in sorted(am_rows, key=lambda x: _perf_num(x.get("target_fy")), reverse=True):
        t, ac = _perf_num(a.get("target_fy")), _perf_num(a.get("actual_ytd"))
        pct = f"{(ac / t * 100):.1f}%" if t else "-"
        am_table_rows.append([a.get("am") or "", _fmt_idr(t), _fmt_idr(ac), pct,
                              _fmt_idr(_perf_num(a.get("forecast_fy"))), _fmt_idr(_perf_num(a.get("gap"))),
                              _fmt_idr(_perf_num(a.get("current_pipeline")))])
    story.append(make_table(am_table_rows, [3.6 * cm, 2.6 * cm, 2.6 * cm, 1.6 * cm, 2.6 * cm, 2.4 * cm, 2.6 * cm]))
    story.append(Spacer(1, 0.4 * cm))

    if down or up:
        small = ParagraphStyle("psm", parent=body_style, fontSize=8)
        story.append(Paragraph("Churn Watch (vs previous month)", h2_style))
        if down:
            churn_rows = [["Account", "AM", "Previous", "Current", "Change"]]
            for d in down:
                churn_rows.append([Paragraph(d["account"], small), (d["am"] or "").split(" ")[0],
                                   _fmt_idr(d["prev"]), _fmt_idr(d["cur"]), f"-{_fmt_idr(abs(d['delta']))}"])
            story.append(make_table(churn_rows, [5.5 * cm, 2.5 * cm, 2.8 * cm, 2.8 * cm, 2.8 * cm]))
        else:
            story.append(Paragraph("No accounts declined month-over-month.", body_style))
        story.append(Spacer(1, 0.3 * cm))

        story.append(Paragraph("Growing Accounts (vs previous month)", h2_style))
        if up:
            growth_rows = [["Account", "AM", "Previous", "Current", "Change"]]
            for d in up:
                growth_rows.append([Paragraph(d["account"], small), (d["am"] or "").split(" ")[0],
                                    _fmt_idr(d["prev"]), _fmt_idr(d["cur"]), f"+{_fmt_idr(d['delta'])}"])
            story.append(make_table(growth_rows, [5.5 * cm, 2.5 * cm, 2.8 * cm, 2.8 * cm, 2.8 * cm]))
        else:
            story.append(Paragraph("No growth recorded month-over-month.", body_style))
        story.append(Spacer(1, 0.4 * cm))

    if pillar_entries:
        story.append(Paragraph("Revenue by Product Pillar", h2_style))
        pillar_rows = [["Pillar", "Revenue", "Share"]]
        for p, v in pillar_entries:
            pillar_rows.append([p, _fmt_idr(v), f"{v / pillar_total * 100:.0f}%"])
        story.append(make_table(pillar_rows, [8 * cm, 5 * cm, 3 * cm]))
        story.append(Spacer(1, 0.3 * cm))

    story.append(Paragraph(
        "For the full account-level pipeline cross-check (which top accounts have no opportunity "
        "attached in this dashboard), see the Performance tab in the live app.", body_style))

    doc.build(story)
    buf.seek(0)
    filename = f"performance_summary_{date.today().isoformat()}.pdf"
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=filename)


# --------------------------------------------------------------------------
# Execution framework export in the "Sales Activity Tracker" sheet format
# --------------------------------------------------------------------------
TRACKER_HEADERS = ["No.", "PODS ", "Opportunity Name", "Customer", "Account Manager",
                   "TCV (IDR)", "Rev 2026 (IDR)", "Target Quarter",
                   "Pillar (8 Enterprise Proof)", "Activity / Action", "Status",
                   "Due Date", "Completed Date", "Notes"]
TRACKER_STATUS = {"not_started": "Not Started", "planned": "Planned", "in_progress": "In Progress",
                  "done": "Done", "na": "Not Started"}


@app.route("/api/export/tracker_xlsx", methods=["GET"])
@login_required()
def export_tracker_xlsx():
    """One row per activity, ready to paste into the shared Tracker sheet."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        return jsonify({"error": "openpyxl is not installed on the server. "
                                 "Run: pip install --user openpyxl, then reload."}), 500

    db = get_db()
    scope_pod = query_pod_for(g.current_user)
    query = "SELECT * FROM deals WHERE 1=1"
    params = []
    if scope_pod is not None:
        query += " AND pod = ?"
        params.append(scope_pod)
    am = request.args.get("am")
    if am:
        query += " AND assigned_am = ?"
        params.append(am)
    query += " ORDER BY pod, assigned_am, deal_name"
    deals = [deal_to_dict(r) for r in db.execute(query, params).fetchall()]

    wb = Workbook()
    ws = wb.active
    ws.title = "Tracker"
    ws.append(TRACKER_HEADERS)
    for c in range(1, len(TRACKER_HEADERS) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = PatternFill("solid", fgColor="1A73E8")
        cell.font = Font(color="FFFFFF", bold=True)
    ws.freeze_panes = "A2"

    # By default only proofs with something to say are exported (evidence, a target
    # date, or progress). ?include_empty=1 emits a row for every planned proof.
    include_empty = str(request.args.get("include_empty", "")).lower() in ("1", "true", "yes")

    n = 0
    for d in deals:
        proofs = d["proofs"]
        for idx, key in enumerate(PROOF_KEYS, start=1):
            item = proofs.get(key, {})
            status = item.get("status", "not_started")
            if status == "na":
                continue
            entries = item.get("entries", [])
            has_content = bool(entries) or bool(item.get("due")) or status != "not_started"
            if not has_content and not include_empty:
                continue
            pillar = f"{idx}. {PROOF_NAMES[key]}"
            # one row per evidence entry; a proof with none still emits its row
            rows = entries or [{"text": "", "date": "", "status": status}]
            for e in rows:
                n += 1
                e_status = e.get("status") or status
                completed = e.get("date", "") if e_status == "done" else ""
                ws.append([
                    n, d["pod_label"], d["deal_name"], d["customer"], d["assigned_am"],
                    d["estimated_value"] or None, d["revenue_2026"] or None,
                    d["target_quarter"], pillar,
                    e.get("text") or "(activity to be defined)",
                    TRACKER_STATUS.get(e_status, "Not Started"),
                    item.get("due", ""), completed, d["strategy"],
                ])
        # ad-hoc next actions land against Proof of Qualification by default
        for a in d["next_actions"]:
            n += 1
            ws.append([
                n, d["pod_label"], d["deal_name"], d["customer"], d["assigned_am"],
                d["estimated_value"] or None, d["revenue_2026"] or None,
                d["target_quarter"], "1. Proof of Qualification",
                a.get("action", ""), "Done" if a.get("done") else "Not Started",
                a.get("due", ""), a.get("due", "") if a.get("done") else "", d["strategy"],
            ])

    for col, width in zip("ABCDEFGHIJKLMN",
                          [6, 10, 42, 28, 24, 16, 16, 14, 26, 42, 13, 13, 15, 46]):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2):
        row[9].alignment = Alignment(wrap_text=True, vertical="top")
        row[13].alignment = Alignment(wrap_text=True, vertical="top")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"tracker_activities_{date.today().isoformat()}.xlsx"
    return send_file(
        buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=filename)


# --------------------------------------------------------------------------
# PDF Export
# --------------------------------------------------------------------------
def _fmt_idr(value):
    if value >= 1e9:
        return f"IDR {value/1e9:,.2f}B"
    if value >= 1e6:
        return f"IDR {value/1e6:,.1f}M"
    return f"IDR {value:,.0f}"


@app.route("/api/export/pdf", methods=["GET"])
@login_required()
def export_pdf():
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    db = get_db()
    scope_pod = query_pod_for(g.current_user)
    if scope_pod is not None:
        deals = [deal_to_dict(r) for r in
                 db.execute("SELECT * FROM deals WHERE pod = ?", (scope_pod,)).fetchall()]
    else:
        deals = [deal_to_dict(r) for r in db.execute("SELECT * FROM deals").fetchall()]
    config = resolve_config_for(db, g.current_user)

    total_pipeline = sum(d["estimated_value"] for d in deals)
    total_rev_2026 = sum(d["revenue_2026"] for d in deals)
    weighted = sum(d["estimated_value"] * d["progress"] / 100 for d in deals)
    target = config["target_amount"]
    achieved = config.get("current_achievement", 0)
    recurring = config.get("recurring_revenue", 0)
    covered = achieved + recurring + total_rev_2026
    remaining_gap = max(target - covered, 0)
    gap = target - total_pipeline
    avg_deal = total_pipeline / len(deals) if deals else 0
    am_targets = config.get("am_targets", {})

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        leftMargin=1.6 * cm, rightMargin=1.6 * cm,
    )
    styles = getSampleStyleSheet()
    brand = colors.HexColor("#1a73e8")
    title_style = ParagraphStyle("TitleC", parent=styles["Title"], fontSize=18, textColor=brand)
    h2_style = ParagraphStyle("H2C", parent=styles["Heading2"], fontSize=13, textColor=brand,
                              spaceBefore=14, spaceAfter=6)
    body_style = styles["BodyText"]

    def make_table(data, col_widths):
        t = Table(data, colWidths=col_widths)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), brand),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dadce0")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f3f4")]),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        return t

    scope_label = POD_LABELS.get(scope_pod, "Engine 1 - All PODS") if scope_pod else "Engine 1 - All PODS"
    story = []
    story.append(Paragraph(f"Engine 1 Command Center - {scope_label}", title_style))
    story.append(Paragraph("Deal Execution &amp; Strategy Report", styles["Heading3"]))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", body_style))
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Executive Summary", h2_style))
    story.append(Paragraph(
        f"Against the full-year 2026 target of {_fmt_idr(target)}, the team has achieved "
        f"{_fmt_idr(achieved)} to date, expects {_fmt_idr(recurring)} in recurring revenue, and is "
        f"tracking {_fmt_idr(total_rev_2026)} of realizable 2026 pipeline across {len(deals)} "
        f"opportunities. That covers {_fmt_idr(covered)}, leaving a remaining gap of "
        f"{_fmt_idr(remaining_gap)} to close. Execution spans four Account Managers across IoT "
        f"Connectivity, Device Bundling, CCTV &amp; Vision Analytics, Enterprise Solutions and "
        f"Digital Reward.",
        body_style,
    ))
    story.append(Spacer(1, 0.3 * cm))

    story.append(Paragraph("Full-Year 2026 Coverage", h2_style))
    story.append(make_table([
        ["Metric", "Value"],
        ["Full-Year 2026 Target", _fmt_idr(target)],
        ["Achieved (YTD)", _fmt_idr(achieved)],
        ["Recurring (upcoming months)", _fmt_idr(recurring)],
        ["2026 Realizable Pipeline", _fmt_idr(total_rev_2026)],
        ["Total Covered", _fmt_idr(covered)],
        ["Remaining Gap to Close", _fmt_idr(remaining_gap)],
    ], [8 * cm, 8 * cm]))
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Pipeline KPIs", h2_style))
    story.append(make_table([
        ["Metric", "Value"],
        ["Current Pipeline (TCV)", _fmt_idr(total_pipeline)],
        ["Weighted Pipeline", _fmt_idr(weighted)],
        ["Total Opportunities", str(len(deals))],
        ["Average Deal Size", _fmt_idr(avg_deal)],
    ], [8 * cm, 8 * cm]))
    story.append(Spacer(1, 0.4 * cm))

    # By Account Manager (with target attainment on 2026 revenue)
    story.append(Paragraph("Account Manager Target vs Pipeline", h2_style))
    am_val = {}
    am_rev = {}
    am_cnt = {}
    for d in deals:
        am_val[d["assigned_am"]] = am_val.get(d["assigned_am"], 0) + d["estimated_value"]
        am_rev[d["assigned_am"]] = am_rev.get(d["assigned_am"], 0) + d["revenue_2026"]
        am_cnt[d["assigned_am"]] = am_cnt.get(d["assigned_am"], 0) + 1
    am_rows = [["Account Manager", "Opps", "TCV Pipeline", "2026 Rev", "Target", "Attain."]]
    for am in sorted(am_val, key=lambda k: am_val[k], reverse=True):
        tgt = am_targets.get(am, 0)
        attain = f"{(am_rev[am]/tgt*100):.0f}%" if tgt else "-"
        am_rows.append([am, str(am_cnt[am]), _fmt_idr(am_val[am]),
                        _fmt_idr(am_rev[am]), _fmt_idr(tgt) if tgt else "-", attain])
    story.append(make_table(am_rows, [4.5 * cm, 1.3 * cm, 3.2 * cm, 3.2 * cm, 3.2 * cm, 1.6 * cm]))
    story.append(Spacer(1, 0.4 * cm))

    # By stage
    story.append(Paragraph("Deals by Stage", h2_style))
    stage_counts = {}
    for d in deals:
        stage_counts[d["stage"]] = stage_counts.get(d["stage"], 0) + 1
    story.append(make_table(
        [["Stage", "Count"]] + [[k, str(v)] for k, v in stage_counts.items()],
        [8 * cm, 8 * cm],
    ))
    story.append(Spacer(1, 0.4 * cm))

    # By pillar
    story.append(Paragraph("Pipeline by Strategic Pillar", h2_style))
    pillar_values = {}
    for d in deals:
        pillar_values[d["strategic_pillar"]] = pillar_values.get(d["strategic_pillar"], 0) + d["estimated_value"]
    story.append(make_table(
        [["Strategic Pillar", "Pipeline Value"]] +
        [[k, _fmt_idr(v)] for k, v in sorted(pillar_values.items(), key=lambda x: x[1], reverse=True)],
        [10 * cm, 6 * cm],
    ))
    story.append(Spacer(1, 0.4 * cm))

    # Opportunity detail
    story.append(Paragraph("Opportunity Detail", h2_style))
    detail_rows = [["Opportunity", "Customer", "AM", "Value", "Stage", "Prog."]]
    for d in sorted(deals, key=lambda x: x["estimated_value"], reverse=True):
        detail_rows.append([
            Paragraph(d["deal_name"], ParagraphStyle("s", parent=body_style, fontSize=7.5)),
            Paragraph(d["customer"], ParagraphStyle("s", parent=body_style, fontSize=7.5)),
            d["assigned_am"].split(" ")[0],
            _fmt_idr(d["estimated_value"]),
            d["stage"],
            f"{d['progress']}%",
        ])
    story.append(make_table(detail_rows, [5 * cm, 4 * cm, 2.2 * cm, 2.6 * cm, 2.2 * cm, 1.2 * cm]))
    story.append(Spacer(1, 0.4 * cm))

    # Execution framework coverage across the 8 Enterprise Proofs
    if deals:
        story.append(Paragraph("Execution Framework - 8 Enterprise Proofs", h2_style))
        fw_rows = [["#", "Proof", "Done", "In progress", "Not started", "N/A"]]
        for idx, key in enumerate(PROOF_KEYS, start=1):
            counts = {"done": 0, "in_progress": 0, "not_started": 0, "na": 0}
            for d in deals:
                counts[d["proofs"].get(key, {}).get("status", "not_started")] += 1
            fw_rows.append([str(idx), PROOF_NAMES[key], str(counts["done"]),
                            str(counts["in_progress"]), str(counts["not_started"]),
                            str(counts["na"])])
        story.append(make_table(fw_rows, [1 * cm, 6 * cm, 2.2 * cm, 2.6 * cm, 2.6 * cm, 1.6 * cm]))
        avg_fw = round(sum(proof_progress(d["proofs"]) for d in deals) / len(deals))
        story.append(Spacer(1, 0.2 * cm))
        story.append(Paragraph(
            f"Average framework completion across the portfolio: <b>{avg_fw}%</b>.", body_style))
        story.append(Spacer(1, 0.4 * cm))

    # Strategy playbook — the AM's how-to-close per opportunity
    strat_deals = [d for d in deals if (d.get("strategy") or "").strip()]
    if strat_deals:
        story.append(Paragraph("Strategy &amp; How to Close", h2_style))
        small = ParagraphStyle("sm", parent=body_style, fontSize=8)
        strat_rows = [["Opportunity / AM", "Strategy to win &amp; close"]]
        for d in sorted(strat_deals, key=lambda x: x["assigned_am"]):
            label = f"<b>{d['deal_name']}</b><br/>{d['customer']} &middot; {d['assigned_am']}"
            strat_rows.append([Paragraph(label, small), Paragraph(d["strategy"], small)])
        story.append(make_table(strat_rows, [6 * cm, 10 * cm]))
        story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Recommendations for H2 Execution", h2_style))
    blocked = [d for d in deals if d["is_blocked"]]
    recs = [
        "Prioritize Negotiation-stage opportunities with the highest weighted value to accelerate close.",
        "Protect large device-bundling deals by locking fixed partner pricing and delivery timelines.",
        "Assign dedicated, skilled implementation PICs to the largest Enterprise Solutions pursuits.",
        "Maintain frequent customer engagement and manage expectations on CCTV & Vision projects.",
    ]
    if gap > 0:
        recs.insert(0, f"Portfolio is {_fmt_idr(gap)} short of the H2 target - sustain pipeline "
                       f"generation across all four Account Managers.")
    if blocked:
        recs.insert(1, f"{len(blocked)} opportunity(ies) blocked; assign executive sponsors this week.")
    for r in recs:
        story.append(Paragraph(f"&#8226; {r}", body_style))

    doc.build(story)
    buf.seek(0)
    filename = f"deal_tracker_report_{date.today().isoformat()}.pdf"
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=filename)


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------
init_db()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
