-- ServiceJourney POC — database schema
-- Sign-in is handled by Keycloak (single sign-on): passwords and TOTP secrets live
-- ONLY there. The credential columns below are kept solely so older volumes can be
-- migrated into Keycloak by app/sync.py, which then erases them from this database.
--
-- This file is idempotent: CREATE TABLE IF NOT EXISTS + ALTER ... IF NOT EXISTS,
-- so it safely upgrades an existing database volume as the model evolves.

CREATE TABLE IF NOT EXISTS departments (
    id   SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS services (
    id   SERIAL PRIMARY KEY,
    key  TEXT NOT NULL UNIQUE,     -- machine key used by the service dockers (e.g. 'service1')
    name TEXT NOT NULL             -- human friendly name
);

-- What kind of resource a "service" represents. A 'laptop' additionally consumes
-- an 'account' when provisioned (see quota rules in app.py).
ALTER TABLE services ADD COLUMN IF NOT EXISTS resource_type TEXT NOT NULL DEFAULT 'other';  -- laptop | account | other

-- The access "template" for a department (informational: suggests which services
-- a requestor typically needs for that department). Access is NO LONGER granted
-- automatically from this — every grant now flows through the approval workflow.
CREATE TABLE IF NOT EXISTS department_access (
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    service_id    INTEGER NOT NULL REFERENCES services(id)    ON DELETE CASCADE,
    PRIMARY KEY (department_id, service_id)
);

CREATE TABLE IF NOT EXISTS employees (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT,                        -- Argon2id (PHC string); NULL = no password set
    full_name     TEXT NOT NULL,
    department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,  -- legacy; superseded by role
    status        TEXT NOT NULL DEFAULT 'active'   -- 'active' | 'inactive' | 'suspended' (shift)
);

-- Console role. Everyone is an 'employee' (a possible access subject); some
-- accounts additionally hold a workflow role.
--   employee | requestor | hr | director | aa | it_admin | euss | admin
ALTER TABLE employees ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'employee';
-- Contact address used by the email-notification service.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS email TEXT;

-- The EFFECTIVE access of an employee. Source of truth the service dockers check.
-- Now only mutated by the workflow (IT 'process' grants, EUSS 'retrieve' revokes).
CREATE TABLE IF NOT EXISTS employee_access (
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    service_id  INTEGER NOT NULL REFERENCES services(id)  ON DELETE CASCADE,
    PRIMARY KEY (employee_id, service_id)
);
-- There is no per-resource "suspended" state: an employee either holds a usable
-- laptop+account pair or holds nothing. (Dropped from older databases.)
ALTER TABLE employee_access DROP COLUMN IF EXISTS suspended;

-- Physical assets provisioned/retrieved by EUSS ("SN Assets").
CREATE TABLE IF NOT EXISTS assets (
    id          SERIAL PRIMARY KEY,
    asset_tag   TEXT NOT NULL UNIQUE,          -- serial number
    kind        TEXT NOT NULL DEFAULT 'Laptop',
    status      TEXT NOT NULL DEFAULT 'available',  -- 'available' | 'assigned' | 'returned'
    assigned_to INTEGER REFERENCES employees(id) ON DELETE SET NULL
);

-- An access change working its way through the bureaucratic workflow.
CREATE TABLE IF NOT EXISTS requests (
    id                 SERIAL PRIMARY KEY,
    type               TEXT NOT NULL,          -- 'grant' | 'return' | 'shift'
    requestor_id       INTEGER NOT NULL REFERENCES employees(id),
    target_employee_id INTEGER NOT NULL REFERENCES employees(id),
    service_id         INTEGER REFERENCES services(id),   -- null for 'shift'
    target_department_id INTEGER REFERENCES departments(id),  -- destination dept for 'shift'
    justification      TEXT,
    status             TEXT NOT NULL,          -- see workflow states in app.py
    reject_reason      TEXT,
    appointment_at     TIMESTAMPTZ,
    asset_id           INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Upgrade existing volumes to the shift-capable shape.
ALTER TABLE requests ALTER COLUMN service_id DROP NOT NULL;
ALTER TABLE requests ADD COLUMN IF NOT EXISTS target_department_id INTEGER REFERENCES departments(id);
-- Origin department for a shift (so history can show "from -> to").
ALTER TABLE requests ADD COLUMN IF NOT EXISTS source_department_id INTEGER REFERENCES departments(id);
-- Whether the requestor is acting for themselves or on behalf of someone else.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS on_behalf BOOLEAN NOT NULL DEFAULT FALSE;

-- Supporting documents a requestor may attach to a request (stored inline as
-- base64 for this POC — no separate file store).
CREATE TABLE IF NOT EXISTS request_documents (
    id             SERIAL PRIMARY KEY,
    request_id     INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    filename       TEXT NOT NULL,
    content_base64 TEXT NOT NULL,
    uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Chain-of-custody: every time an asset is provisioned to or retrieved from an
-- employee. Lets EUSS see where a laptop has been and who the last holder was.
CREATE TABLE IF NOT EXISTS asset_events (
    id          SERIAL PRIMARY KEY,
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    action      TEXT NOT NULL,                 -- 'provisioned' | 'retrieved'
    request_id  INTEGER REFERENCES requests(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Immutable audit trail: every action taken on a request, by whom and when.
CREATE TABLE IF NOT EXISTS request_events (
    id         SERIAL PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    actor_id   INTEGER REFERENCES employees(id),
    action     TEXT NOT NULL,                  -- 'submitted','approved','rejected','processed',...
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Organisation-wide capacity ("allowed") per resource. 'used' is derived live
-- from employee_access. Changes are proposed by AA and approved by IT Admin.
CREATE TABLE IF NOT EXISTS quotas (
    service_id INTEGER PRIMARY KEY REFERENCES services(id) ON DELETE CASCADE,
    allowed    INTEGER NOT NULL DEFAULT 0
);

-- Per-department capacity for each resource, managed directly by IT Admin.
-- 'used' is the number of employees IN THAT DEPARTMENT holding the resource.
CREATE TABLE IF NOT EXISTS department_quotas (
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    service_id    INTEGER NOT NULL REFERENCES services(id)    ON DELETE CASCADE,
    allowed       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (department_id, service_id)
);

-- AA proposes a new quota; IT Admin approves or rejects it.
CREATE TABLE IF NOT EXISTS quota_requests (
    id              SERIAL PRIMARY KEY,
    service_id      INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    requested_by    INTEGER NOT NULL REFERENCES employees(id),
    current_allowed INTEGER NOT NULL,
    new_allowed     INTEGER NOT NULL,
    reason          TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    decided_by      INTEGER REFERENCES employees(id),
    decide_note     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------- --
-- LEGACY credential columns (CHANGE 001/002). Keycloak now owns passwords and
-- TOTP; these only carry existing hashes / encrypted TOTP secrets until sync.py
-- has moved them into Keycloak, after which they are set to NULL.
-- --------------------------------------------------------------------------- --
ALTER TABLE employees ADD COLUMN IF NOT EXISTS mfa_enabled        BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS mfa_secret         TEXT;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS mfa_pending_secret TEXT;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS mfa_last_step      BIGINT;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS mfa_failed_count   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS mfa_locked_until   TIMESTAMPTZ;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS mfa_enrolled_at    TIMESTAMPTZ;

-- (Authentication events now live in the tamper-evident audit_log below; a legacy
-- auth_events table from an older volume is migrated into it and dropped at start-up.)

ALTER TABLE employees ADD COLUMN IF NOT EXISTS password_hash        TEXT;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS password_changed_at  TIMESTAMPTZ;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS failed_login_count   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS locked_until         TIMESTAMPTZ;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS last_login_at        TIMESTAMPTZ;
-- Old volumes: let rows exist without the legacy plaintext column being filled
-- (the start-up migration hashes it and then drops it).
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'employees' AND column_name = 'password') THEN
        ALTER TABLE employees ALTER COLUMN password DROP NOT NULL;
    END IF;
END $$;

-- --------------------------------------------------------------------------- --
-- Audit trail: tamper-evident and append-only
-- --------------------------------------------------------------------------- --
-- One row per security-relevant event (sign-ins, admin changes, quota changes,
-- access checks, entitlement changes, workflow steps, asset custody, exports).
--   * `payload` is the canonical JSON of the event and the only thing hashed;
--     the other columns are copies for filtering (verification checks they match).
--   * hash = HMAC-SHA256(AUDIT_HMAC_KEY, prev_hash || payload): editing, deleting
--     or re-ordering any row breaks the chain from that point on.
--   * No foreign keys on purpose: deleting an employee must never touch the log.
CREATE TABLE IF NOT EXISTS audit_log (
    seq            BIGINT PRIMARY KEY,           -- gap-free, assigned under a lock
    ts             TIMESTAMPTZ NOT NULL,
    category       TEXT NOT NULL,                -- auth | admin | config | quota | access | workflow | asset | audit
    action         TEXT NOT NULL,
    outcome        TEXT NOT NULL,                -- success | failure | denied
    actor_id       INTEGER,
    actor_username TEXT,
    actor_role     TEXT,
    target_type    TEXT,
    target_id      TEXT,
    target_label   TEXT,
    ip             TEXT,
    client         TEXT,
    payload        TEXT NOT NULL,
    prev_hash      TEXT NOT NULL,
    hash           TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS audit_log_ts       ON audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS audit_log_category ON audit_log (category, ts DESC);
CREATE INDEX IF NOT EXISTS audit_log_actor    ON audit_log (actor_username);

-- Append-only, enforced by the database itself (applies to every role, including
-- the table owner): any UPDATE, DELETE or TRUNCATE is rejected.
CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END $$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
CREATE OR REPLACE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only();

-- --------------------------------------------------------------------------- --
-- Single sign-on (Keycloak)
-- --------------------------------------------------------------------------- --
-- The employee's Keycloak account (the token `sub`), whether a change still has to
-- be pushed to Keycloak (retried in the background if Keycloak is down), and the
-- moment from which earlier access tokens are refused (immediate revocation).
ALTER TABLE employees ADD COLUMN IF NOT EXISTS keycloak_id          TEXT UNIQUE;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS kc_sync_pending      BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS tokens_revoked_after TIMESTAMPTZ;
-- A "sign out everywhere" that could not reach Keycloak yet (retried in the background).
ALTER TABLE employees ADD COLUMN IF NOT EXISTS kc_logout_pending    BOOLEAN NOT NULL DEFAULT FALSE;

-- Keycloak login/admin events already copied into audit_log (each exactly once).
CREATE TABLE IF NOT EXISTS keycloak_ingested_events (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,              -- user | admin
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Superseded by Keycloak: its own sessions, reset e-mails and credentials.
DROP TABLE IF EXISTS user_sessions;
DROP TABLE IF EXISTS password_reset_tokens;
DROP TABLE IF EXISTS mfa_recovery_codes;

-- --------------------------------------------------------------------------- --
-- Mandatory Job Rotation (MJR) tracker  (CHANGE 006)
-- --------------------------------------------------------------------------- --
-- When the officer joined their CURRENT department. Set when an onboard or shift
-- request is processed (and when an administrator moves someone directly), so it
-- is what the 4-year rotation clock runs from. rotation_alerted_at = when the
-- Directors were last e-mailed that this officer is due (no repeat e-mails).
ALTER TABLE employees ADD COLUMN IF NOT EXISTS department_since    DATE;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS rotation_alerted_at TIMESTAMPTZ;

-- A Director's decision for an officer who reached the rotation mark.
--   keep   — stays, with a reason; reviewed again on review_after
--   rotate — to be moved to target_department_id; a Requestor then raises the shift
--            request (request_id), which goes through the normal approval workflow
-- department_since copies the tenure the decision was about, so a completed shift
-- (new department_since) automatically starts a fresh rotation clock.
CREATE TABLE IF NOT EXISTS rotation_decisions (
    id                   SERIAL PRIMARY KEY,
    employee_id          INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    department_id        INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    department_since     DATE,
    decision             TEXT NOT NULL CHECK (decision IN ('keep', 'rotate')),
    reason               TEXT,
    target_department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    review_after         DATE,
    request_id           INTEGER REFERENCES requests(id) ON DELETE SET NULL,
    decided_by           INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    decided_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rotation_decisions_emp ON rotation_decisions (employee_id, decided_at DESC);

-- --------------------------------------------------------------------------- --
-- HR-system webhook  (CHANGE 008)
-- --------------------------------------------------------------------------- --
-- One row per event the HR system (HRMS / ServiceNow HRSD) has sent. event_id is the
-- HR system's own id: it makes delivery idempotent (a retry or replay is answered from
-- here instead of raising the request twice). status: received | processed | rejected | failed.
CREATE TABLE IF NOT EXISTS hr_events (
    id                SERIAL PRIMARY KEY,
    event_id          TEXT NOT NULL UNIQUE,
    event_type        TEXT NOT NULL,              -- hire | transfer | resign
    employee_username TEXT,
    payload           TEXT NOT NULL,              -- the exact body received
    status            TEXT NOT NULL DEFAULT 'received',
    result            TEXT,                       -- JSON: what was done, or why it was refused
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS hr_events_received ON hr_events (received_at DESC);
