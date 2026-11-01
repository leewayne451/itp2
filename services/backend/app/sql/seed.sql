-- Seed data for the POC. Idempotent.

-- Services (keys must match the SERVICE_KEY env of each service docker; the
-- dockers are placeholders standing in for the real resources during testing).
INSERT INTO services (key, name)
SELECT 'service1', 'Personal Laptop'
WHERE NOT EXISTS (SELECT 1 FROM services WHERE key = 'service1');

INSERT INTO services (key, name)
SELECT 'service2', 'Account'
WHERE NOT EXISTS (SELECT 1 FROM services WHERE key = 'service2');

-- Names + resource types (idempotent; upgrades an existing volume too).
UPDATE services SET name = 'Personal Laptop', resource_type = 'laptop'  WHERE key = 'service1';
UPDATE services SET name = 'Account',         resource_type = 'account' WHERE key = 'service2';

-- Departments
INSERT INTO departments (name)
SELECT d FROM (VALUES ('Engineering'), ('Sales'), ('Human Resources')) AS t(d)
WHERE NOT EXISTS (SELECT 1 FROM departments);

-- Department access templates (now informational hints for requestors).
INSERT INTO department_access (department_id, service_id)
SELECT dp.id, s.id
FROM departments dp
JOIN services s ON (
        (dp.name = 'Engineering'     AND s.key IN ('service1', 'service2')) OR
        (dp.name = 'Sales'           AND s.key IN ('service2')) OR
        (dp.name = 'Human Resources' AND s.key IN ('service1'))
     )
WHERE NOT EXISTS (SELECT 1 FROM department_access);

-- ------------------------------------------------------------------ accounts
-- Workflow role accounts. Their (temporary) demo passwords are set at the end
-- of this file as Argon2id hashes — no plaintext password is stored anywhere.
-- Columns: username, full_name, role
INSERT INTO employees (username, full_name, role)
SELECT v.username, v.full_name, v.role
FROM (VALUES
        ('admin',    'System Administrator', 'admin'),
        ('req',      'Riley Requestor',      'requestor'),
        ('hr',       'Harper HR',            'hr'),
        ('director', 'Dana Director',        'director'),
        ('aa',       'Alex Authority',       'aa'),
        ('itadmin',  'Ivan IT-Admin',        'it_admin'),
        ('euss',     'Erin EUSS',            'euss')
     ) AS v(username, full_name, role)
WHERE NOT EXISTS (SELECT 1 FROM employees e WHERE e.username = v.username);

-- Ensure roles are correct even if the accounts pre-existed with default role.
UPDATE employees SET role = 'admin',     is_admin = TRUE  WHERE username = 'admin';
UPDATE employees SET role = 'requestor'                   WHERE username = 'req';
UPDATE employees SET role = 'hr'                          WHERE username = 'hr';
UPDATE employees SET role = 'director'                    WHERE username = 'director';
UPDATE employees SET role = 'aa'                          WHERE username = 'aa';
UPDATE employees SET role = 'it_admin'                    WHERE username = 'itadmin';
UPDATE employees SET role = 'euss'                        WHERE username = 'euss';

-- Sample employees (access subjects). They start with NO access — access is
-- obtained through the approval workflow.
INSERT INTO employees (username, full_name, department_id, role, status)
SELECT 'alice', 'Alice Employee', (SELECT id FROM departments WHERE name = 'Engineering'), 'employee', 'active'
WHERE NOT EXISTS (SELECT 1 FROM employees WHERE username = 'alice');

INSERT INTO employees (username, full_name, department_id, role, status)
SELECT 'bob', 'Bob Employee', (SELECT id FROM departments WHERE name = 'Sales'), 'employee', 'active'
WHERE NOT EXISTS (SELECT 1 FROM employees WHERE username = 'bob');

INSERT INTO employees (username, full_name, department_id, role, status)
SELECT 'charlie', 'Charlie Employee', (SELECT id FROM departments WHERE name = 'Human Resources'), 'employee', 'active'
WHERE NOT EXISTS (SELECT 1 FROM employees WHERE username = 'charlie');

-- More staff so the quotas can actually be pushed over. Only alice and bob start
-- with resources; everyone else is a candidate for an access request.
INSERT INTO employees (username, full_name, department_id, role, status)
SELECT v.username, v.full_name,
       (SELECT id FROM departments WHERE name = v.dept), 'employee', 'active'
FROM (VALUES
        ('erin',   'Erin Engineer',   'Engineering'),
        ('frank',  'Frank Engineer',  'Engineering'),
        ('grace',  'Grace Sales',     'Sales'),
        ('henry',  'Henry Sales',     'Sales'),
        ('ivy',    'Ivy HR',          'Human Resources'),
        ('jack',   'Jack HR',         'Human Resources')
     ) AS v(username, full_name, dept)
WHERE NOT EXISTS (SELECT 1 FROM employees e WHERE e.username = v.username);

-- Un-onboarded hires (no department) for the onboarding flow.
INSERT INTO employees (username, full_name, department_id, role, status)
SELECT v.username, v.full_name, NULL, 'employee', 'active'
FROM (VALUES ('kate', 'Kate Newhire'), ('liam', 'Liam Newhire')) AS v(username, full_name)
WHERE NOT EXISTS (SELECT 1 FROM employees e WHERE e.username = v.username);

-- ------------------------------------------------------------------- assets
-- Asset kinds mirror the two resources: Laptop and Account.
-- Plenty of stock so the QUOTA is what blocks a request, not a shortage of assets.
-- Guarded per tag, so new tags are added even to an existing database.
INSERT INTO assets (asset_tag, kind)
SELECT v.tag, v.kind
FROM (VALUES
        ('LAP-1001', 'Laptop'), ('LAP-1002', 'Laptop'), ('LAP-1003', 'Laptop'),
        ('LAP-1004', 'Laptop'), ('LAP-1005', 'Laptop'), ('LAP-1006', 'Laptop'),
        ('LAP-1007', 'Laptop'), ('LAP-1008', 'Laptop'), ('LAP-1009', 'Laptop'),
        ('LAP-1010', 'Laptop'), ('LAP-1011', 'Laptop'), ('LAP-1012', 'Laptop'),
        ('ACC-2001', 'Account'), ('ACC-2002', 'Account'), ('ACC-2003', 'Account'),
        ('ACC-2004', 'Account'), ('ACC-2005', 'Account'), ('ACC-2006', 'Account'),
        ('ACC-2007', 'Account'), ('ACC-2008', 'Account'), ('ACC-2009', 'Account'),
        ('ACC-2010', 'Account'), ('ACC-2011', 'Account'), ('ACC-2012', 'Account'),
        ('ACC-2013', 'Account'), ('ACC-2014', 'Account'), ('ACC-2015', 'Account')
     ) AS v(tag, kind)
WHERE NOT EXISTS (SELECT 1 FROM assets a WHERE a.asset_tag = v.tag);

-- --------------------------------------------------------------------- quotas
-- Starting capacity: 3 laptops, 5 accounts (deliberately tight to exercise the
-- AA quota-increase -> IT Admin approval flow).
-- Quotas are deliberately TIGHT relative to headcount so overflow is easy to hit:
--   Engineering / Sales / HR each have 3 staff but only 2 laptops and 2 accounts.
-- Org-wide cap (AA proposes changes, IT Admin approves): 5 laptops / 6 accounts,
-- against 3 departments x 2 = 6 department slots, so the org cap bites too.
INSERT INTO quotas (service_id, allowed)
SELECT s.id, CASE WHEN s.key = 'service1' THEN 5 ELSE 6 END
FROM services s
WHERE NOT EXISTS (SELECT 1 FROM quotas q WHERE q.service_id = s.id);

-- Per-department cap, managed directly by IT Admin.
INSERT INTO department_quotas (department_id, service_id, allowed)
SELECT d.id, s.id, 2
FROM departments d CROSS JOIN services s
WHERE s.resource_type IN ('laptop', 'account')
  AND NOT EXISTS (SELECT 1 FROM department_quotas dq
                  WHERE dq.department_id = d.id AND dq.service_id = s.id);

-- Dave is hired but NOT yet in any department — used to demo the onboard flow.
INSERT INTO employees (username, full_name, department_id, role, status)
SELECT 'dave', 'Dave Newhire', NULL, 'employee', 'active'
WHERE NOT EXISTS (SELECT 1 FROM employees WHERE username = 'dave');

-- Demo state: alice and bob already hold a laptop + account, with real asset tags.
-- IMPORTANT: this only applies to a PRISTINE database (no request has ever been
-- raised). Without that guard a backend restart would silently re-grant access
-- that the workflow had deliberately revoked.
INSERT INTO employee_access (employee_id, service_id)
SELECT e.id, s.id
FROM employees e CROSS JOIN services s
WHERE e.username IN ('alice', 'bob') AND s.key IN ('service1', 'service2')
  AND NOT EXISTS (SELECT 1 FROM requests)
  AND NOT EXISTS (SELECT 1 FROM employee_access ea WHERE ea.employee_id = e.id AND ea.service_id = s.id);

UPDATE assets SET status='assigned', assigned_to=(SELECT id FROM employees WHERE username='alice')
WHERE asset_tag='LAP-1001' AND status='available' AND NOT EXISTS (SELECT 1 FROM requests);
UPDATE assets SET status='assigned', assigned_to=(SELECT id FROM employees WHERE username='alice')
WHERE asset_tag='ACC-2001' AND status='available' AND NOT EXISTS (SELECT 1 FROM requests);
UPDATE assets SET status='assigned', assigned_to=(SELECT id FROM employees WHERE username='bob')
WHERE asset_tag='LAP-1002' AND status='available' AND NOT EXISTS (SELECT 1 FROM requests);
UPDATE assets SET status='assigned', assigned_to=(SELECT id FROM employees WHERE username='bob')
WHERE asset_tag='ACC-2002' AND status='available' AND NOT EXISTS (SELECT 1 FROM requests);

-- Seed a 'provisioned' custody event for each pre-assigned asset (once).
INSERT INTO asset_events (asset_id, employee_id, action)
SELECT a.id, a.assigned_to, 'provisioned'
FROM assets a
WHERE a.assigned_to IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM asset_events);

-- Default email for every account that lacks one (admin can edit these later).
UPDATE employees SET email = username || '@servicejourney.local' WHERE email IS NULL;

-- ------------------------------------------------------------- demo credentials
-- Every seeded account starts with a TEMPORARY password equal to its username,
-- given here only as an Argon2id hash. On first start the backend moves these
-- hashes into Keycloak (which does the sign-in) and erases them from this table (argon2-cffi defaults = RFC 9106 low-memory
-- profile: t=3, m=64 MiB, p=4). must_change_password forces the user to choose
-- a strong password of their own at first sign-in. Only applied to accounts that
-- have no password yet, so it never overwrites a password someone has set.
UPDATE employees e SET password_hash = d.hash, must_change_password = TRUE
FROM (VALUES
        ('admin', '$argon2id$v=19$m=65536,t=3,p=4$IzxqXIX2emVomePWJOLB0g$KfAroyjN4eKq22IZ1BVb/HJNOi0yPXFTDwlea27m8AE'),
        ('req', '$argon2id$v=19$m=65536,t=3,p=4$HPJZBKOVsloEu2+T6jxyRg$MYPg41VryfR9/4jvoBhy2pNO+Zznt4iydVxYHTLqHf4'),
        ('hr', '$argon2id$v=19$m=65536,t=3,p=4$GcPfMh8/QHQpu2LXRfBXYQ$yH/qMoujnczrJYt0zZ8ccZrZoxVyhN2YsvCs5A6YOOg'),
        ('director', '$argon2id$v=19$m=65536,t=3,p=4$CKCM4vEMqTa8CwRwYplH5g$HgzpDrRFptYOXNyYLLK5VfMWmesNiBbb9UfoC1aagfs'),
        ('aa', '$argon2id$v=19$m=65536,t=3,p=4$3VQQyinr34g/Z6Wa7LqAGw$JknLzofkAkwMgu4jDdv4/2qYhQo1fjAkKo0iVg8GuzQ'),
        ('itadmin', '$argon2id$v=19$m=65536,t=3,p=4$e3TRwZnf5UAnn81rHaucmA$r1rCpPMMcPOd+TSEJpqcmE6/yqzA5eOAS+xNGshez0I'),
        ('euss', '$argon2id$v=19$m=65536,t=3,p=4$T1D2ZHGSrLVjTD7CVlW5Xw$p8cu6smTuvrPJXqYc47lHwModoSZC/w4XaxcnNlrQlI'),
        ('alice', '$argon2id$v=19$m=65536,t=3,p=4$JJvhF3JjJ0lATjgF6TBAjw$0Lb7DYOVZwNo3HlmRppUj9k6BYpuk21eMKAe3QdD0Q4'),
        ('bob', '$argon2id$v=19$m=65536,t=3,p=4$KCjK1o59hT01MFeAx3oEhQ$JoTGfZo+TsYRuQ0j3QZBVVldXxGOpcSCk7wmN0r10Gw'),
        ('charlie', '$argon2id$v=19$m=65536,t=3,p=4$7oI7WKp5XtNXQPxTBbD1eQ$IQvmaZC+HoRTS1fe4yp0oYyL5m5+bpBLochsnGv3rHg'),
        ('erin', '$argon2id$v=19$m=65536,t=3,p=4$AwJi03wdRFNJ65g8h9xeKg$5eHYTFzis82EVqNYdnQoLXC6BLED4fc+F01B6FHwn3c'),
        ('frank', '$argon2id$v=19$m=65536,t=3,p=4$uMhzjEfUHlba0OdqbT2RMg$2eYlN11PvVjawQcZi6Zx7g5idL9VPxl+Indmmu2jDT0'),
        ('grace', '$argon2id$v=19$m=65536,t=3,p=4$A7U9PjMwAF5XTj++8vEhrw$1rUMqDSSGK2wtwoAy87s6rsvl0wtukWtthz8cATiY/U'),
        ('henry', '$argon2id$v=19$m=65536,t=3,p=4$ejB+QidnGm7TVeJ6rIG8eQ$X0k7lyiPkyjMe4mgNeRO/y8BDwE7hnHWR3v0nI8eSnc'),
        ('ivy', '$argon2id$v=19$m=65536,t=3,p=4$RCMntBa8Xoyu+VGY83MPhQ$sRP26FuaIUe7cJQG3k/F3fcHYTqZNYuAWWylqZ+1ENc'),
        ('jack', '$argon2id$v=19$m=65536,t=3,p=4$c6Ze9MNAK9DKr3QCGXbbfA$vGSPnDkMSPo9eB8rTdi+ky6s8hydSXhro7dBe1FNuew'),
        ('kate', '$argon2id$v=19$m=65536,t=3,p=4$lD6Wsft3Z68A6xf4hk+VzA$pXZok4XI5rWK/cTw+QXMoSzutbt/KLBz8QFP5XH/qZg'),
        ('liam', '$argon2id$v=19$m=65536,t=3,p=4$dvs4kebqnKXdVICxcgi+6A$KFeMx2Tuq0HQZj0ANCGJVppOwAYbCg4xDH0Iu9WolAM'),
        ('dave', '$argon2id$v=19$m=65536,t=3,p=4$ypy46Ua1prRzNAYbgMRJlQ$5ESwgJZeq5DOPfkRAPjNh/eKQ++SJ/7v5KHZn2LHWwQ')
     ) AS d(username, hash)
WHERE e.username = d.username AND e.password_hash IS NULL AND e.keycloak_id IS NULL;

-- ------------------------------------------------- MJR rotation clock (CHANGE 006)
-- Demo tenures, RELATIVE to today so the demo works whenever the stack is started:
--   alice + frank are past the 4-year mark (due), grace reaches it within ~2 months
--   (due soon), everyone else is well inside. Only fills empty values, so it never
--   overwrites a date set by a real onboard / shift.
UPDATE employees e SET department_since = (CURRENT_DATE - v.tenure::interval)::date
FROM (VALUES
        ('alice',   '5 years 3 months'),
        ('frank',   '4 years 2 months'),
        ('grace',   '3 years 10 months'),
        ('erin',    '3 years'),
        ('ivy',     '2 years 6 months'),
        ('bob',     '2 years'),
        ('charlie', '1 year 6 months'),
        ('henry',   '1 year'),
        ('jack',    '8 months')
     ) AS v(username, tenure)
WHERE e.username = v.username AND e.department_id IS NOT NULL AND e.department_since IS NULL;

-- Anyone else already in a department (e.g. an upgraded volume): start the clock
-- from the last completed onboard / shift into that department, else from today.
UPDATE employees e SET department_since = COALESCE(
        (SELECT r.updated_at::date FROM requests r
          WHERE r.target_employee_id = e.id AND r.type IN ('onboard', 'shift')
            AND r.status = 'completed' AND r.target_department_id = e.department_id
          ORDER BY r.updated_at DESC LIMIT 1),
        CURRENT_DATE)
WHERE e.department_id IS NOT NULL AND e.department_since IS NULL;
