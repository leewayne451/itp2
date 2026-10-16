-- ServiceJourney POC — database schema
-- NOTE: passwords are stored in PLAINTEXT on purpose for this POC (per requirements).
--       Do NOT reuse this schema in anything real.
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
    password      TEXT NOT NULL,               -- PLAINTEXT (POC only)
    full_name     TEXT NOT NULL,
    department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,  -- legacy; superseded by role
    status        TEXT NOT NULL DEFAULT 'active'   -- 'active' | 'inactive'
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
