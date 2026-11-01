# ServiceJourney — Proof of Concept

A container-based system demonstrating a central **ServiceJourney** access-management service
that is the *single source of truth* for which employees may access which
downstream services. Access changes flow through a **multi-role approval
workflow** (Requestor → HR → Director → Approving Authority → IT Admin → EUSS)
rather than a single admin toggling access directly.

Sign-in is **single sign-on (SSO) through Keycloak** (OpenID Connect): one sign-in —
password plus, for staff, a Google Authenticator code — opens the console and both
services, and signing out of one signs out of all.

> ⚠️ **POC only.** Credentials follow industry practice (Keycloak SSO, Argon2id hashes,
> NIST 800-63B password policy, TOTP MFA, lock-out, revocable sessions, signed tokens,
> secrets in a git-ignored `.env`), but this is still a proof of concept — the apps run
> on Flask's development server and it has not been through a security review.

## Containers

| Container | Folder | Port (host) | Purpose |
|-----------|--------|-------------|---------|
| `postgres` | *(official image)* | — | SQL database |
| `keycloak` | *(official image `quay.io/keycloak/keycloak:26.4.2`)* + [`keycloak/`](keycloak) | 8443 | **Single sign-on** identity provider: sign-in pages, passwords, TOTP, lock-out, sessions, forgot password |
| `keycloak-db` | *(official image)* | — | Keycloak's own database |
| `backend` | [`services/backend`](services/backend) | 5000 | REST API + source of truth + workflow engine |
| `accessmanagement` | [`services/accessmanagement`](services/accessmanagement) | 8080 | Role-aware workflow console |
| `service1` | [`services/service1`](services/service1) | 8081 | **Personal Laptop** resource (placeholder service) |
| `service2` | [`services/service2`](services/service2) | 8082 | **Account** resource (placeholder service) |
| `email` | [`services/email`](services/email) | 8090 | Notification service + **mailbox** web page (POC stand-in for real mail); also receives Keycloak's e-mails (SMTP, internal port 1025) |

## Project structure

Both main apps are proper Flask **packages** (blueprints + modules), not single files.
The console is **server-side rendered** — the browser only receives HTML plus a tiny
progressive-enhancement script, so the application logic is **not shipped to / viewable
in the browser**.

```
keycloak/
  import/servicejourney-realm.json   realm: clients, sign-in flow, password policy, lock-out,
                                     session lifetimes, events (imported on first start)
  password-blacklists/               common-password blocklist used by the policy
services/
  backend/                     REST API (source of truth)
    run.py                     entrypoint (bootstrap DB, sync to Keycloak, serve)
    app/
      __init__.py              app factory (registers blueprints)
      config.py  db.py  mailer.py  core.py     (config, DB, email, domain logic)
      tokens.py                validates Keycloak access tokens (RS256 / JWKS, iss, aud, azp)
      auth.py                  require_auth / require_role (token -> employee, revocation, MFA)
      keycloak.py              Keycloak admin REST client (service account)
      sync.py                  employees -> Keycloak users (status, staff group, credential migration)
      kc_events.py             Keycloak sign-in / admin events -> audit trail
      clients.py               service-to-service client auth
      passwords.py             temporary-password generator, legacy plaintext migration
      audit.py                 tamper-evident append-only audit trail (HMAC chain, file copy, verify)
      rotation.py              Mandatory Job Rotation tracker (due check, e-mails, decisions)
      api/       auth.py reference.py admin.py requests.py quotas.py audit.py rotations.py   (route blueprints)
      sql/       schema.sql  seed.sql
  accessmanagement/            server-rendered console (OIDC client "console")
    run.py
    app/
      __init__.py              app factory
      config.py  backend_client.py  labels.py
      oidc.py  oidc_client.py  token_store.py   (SSO sign-in, tokens kept server-side)
      views/     auth.py dashboard.py requests.py admin.py quotas.py assets.py security.py audit.py rotations.py   (blueprints)
      templates/ base.html login.html security.html dashboard/*.html …
      static/    style.css  app.js  (small, generic UI glue only)
  service1/ service2/          demo services (OIDC clients "service1" / "service2", same oidc.py)
```

Actions use **POST → redirect → GET** with flash messages; the console talks to the
backend API with the user's Keycloak access token via `backend_client`.

## Roles & demo accounts

Every demo account starts with a **temporary password equal to its username**
(stored only as an Argon2id hash, now inside Keycloak). Open <https://localhost:8080> —
you are taken to the ServiceJourney sign-in page (Keycloak). At first sign-in you must
choose your own password (12+ characters). Every **staff account** (everything except
`Employee`) must also set up **two-factor authentication** the first time — have Google
Authenticator (or any TOTP app) ready (see [Single sign-on](#single-sign-on-keycloak)).

> **Current demo passwords (2026-11-01):** `scripts/test_sso.py` re-randomises the accounts it
> uses, so `admin`, `req`, `hr`, `director`, `aa`, `itadmin`, `euss`, `alice`, `bob` and `charlie`
> were reset to the temporary password **`Demo-Reset-2026`** (authenticators cleared). Keycloak's
> password policy does not allow an admin-set password equal to the username. The other employees
> still use username = password. See the OPS NOTE in `ITP2_changes.txt`.

| Account | Role | Does |
|---------|------|------|
| `req` | Requestor | Raises **onboard** (new hire → dept), **grant** (existing employee needs a service), **return** (offboard) and **shift** (dept transfer) requests; attaches documents; books the appointment for every request |
| `hr` | HR | 1st approval |
| `director` | Director (DD/ASO) | 2nd approval |
| `aa` | Approving Authority | 3rd approval; **views every quota** — per-department (read-only) and organisation-wide — and **proposes org-wide changes** (needs IT approval) |
| `itadmin` | IT Admin | Processes the request → **grants access**; manages each **department's laptop/account quota**; **approves/rejects** AA's org-wide quota changes; oversees the **department → service mapping**; may override an over-quota grant with a recorded justification |
| `euss` | EUSS | Provisions the SN asset (grant) / retrieves it → **revokes access** (return); asset kinds are **Laptop** and **Account** (provisioned asset must match the requested kind), each tagged with the holder's department |
| `admin` | Administrator | Manages accounts/roles/departments and **email addresses**; read-only oversight of all requests. **No direct access control.** |
| `alice`, `bob` | Employee | Seeded **with a laptop + account** (real asset tags) |
| `charlie`, `erin`, `frank`, `grace`, `henry`, `ivy`, `jack` | Employee | In a department, **no resources yet** — targets for access requests |
| `dave`, `kate`, `liam` | Employee | **No department** — used to demo the **onboard** flow |

## Single sign-on (Keycloak)

**Keycloak** (container `keycloak`, <https://localhost:8443>) is the identity provider for
the whole project. It owns **authentication** — sign-in pages, passwords, the
authenticator app, lock-out, sessions, forgot password. The backend keeps
**authorization** — roles, departments, which services each person may use, the
approval workflow and the audit trail. The console and the two services are OpenID
Connect clients (`console`, `service1`, `service2`).

```
 Browser ──▶ console / service ──(redirect)──▶ Keycloak sign-in: password [+ TOTP code]
         ◀── back to the app with a one-time code (authorization code + PKCE + state + nonce)
 app ──(code + client secret, internal https://keycloak:8443)──▶ Keycloak: ID + access token
 console ──(Bearer access token)──▶ backend: checks signature (JWKS), issuer, audience,
                                    app (azp), expiry, account active, staff used TOTP
 service ──(client credentials)──▶ backend /api/service/access: is this person allowed here?
```

* **One sign-in for everything.** Signed in to the console? Opening the Personal Laptop
  or Account service lets you straight in (and vice versa) — the service still asks the
  backend whether *you* are granted that service.
* **Single sign-out.** *Sign out* in any app ends the Keycloak session; Keycloak then calls
  every app's `/backchannel-logout` (signed logout token) so all of them end their session.
* **Accounts stay in sync automatically.** The backend is still where accounts are
  created and edited (Administrator). Its service account (`servicejourney-backend`) mirrors
  each employee into Keycloak: create / delete, enabled ⇄ status (deactivated, offboarded or
  **suspended during a department shift** = cannot sign in), staff role ⇄ Keycloak group
  `staff` (TOTP required). If Keycloak is briefly down, changes are retried every 10 s.
* **Changes apply at once.** Deactivation, role change, password / 2FA reset and *Sign out*
  end the user's Keycloak sessions **and** make the backend refuse access tokens (and
  service sessions) issued before that moment — no waiting for a token to expire (access
  tokens live 5 minutes). This holds even while Keycloak is down: the backend-side refusal
  is applied first and the Keycloak sign-out is retried in the background.
* **Changes made directly in Keycloak count too.** Disabling, deleting or signing out a user,
  or resetting / removing their credentials, in the Keycloak admin console is picked up from
  Keycloak's admin events within ~10 s and has the same effect (older tokens refused).
* **Existing accounts move over by themselves.** On first start against an older database
  the backend copies each Argon2id password hash and each TOTP secret (CHANGE 001–003) into
  Keycloak — users keep their password **and their existing authenticator app** — then
  erases them from the app database (audited as `credentials_migrated_to_keycloak`).

### Two-factor authentication (TOTP)

| Who | Policy |
|-----|--------|
| `admin`, `requestor`, `hr`, `director`, `aa`, `it_admin`, `euss` | **Required.** Keycloak makes them set up an authenticator app (QR code) at first sign-in and asks for a code at every sign-in. The backend refuses any staff token without `otp` in its `amr` claim. |
| `employee` | **Optional.** Set one up from **Security → Change password & two-factor authentication**; once set up, Keycloak asks for it. |

Codes are standard RFC 6238 TOTP (6 digits, 30 s, SHA-1) — **Google Authenticator**,
Microsoft Authenticator, Authy etc. The required roles come from `MFA_REQUIRED_ROLES`.
Right after first-time set-up the console asks for one more sign-in with a code, so
the session is genuinely two-factor. **Lost phone:** the administrator clicks **Reset**
in the *2FA* column; the user sets up a new app at next sign-in.

> Recovery codes from CHANGE 001 no longer exist (Keycloak's TOTP has none); use the
> administrator reset instead.

### Keycloak administration

* Admin console: <https://localhost:8443/admin> — user `kcadmin`, password
  `KEYCLOAK_ADMIN_PASSWORD` in `.env` (Keycloak marks this bootstrap admin as temporary;
  create a permanent admin there for anything beyond the POC).
* Realm `servicejourney` comes from `keycloak/import/servicejourney-realm.json`; client
  secrets are filled in from `.env`. **It is imported only when Keycloak starts on an
  empty database** — after editing the file, run `docker compose down -v`.
* Make account changes in the **console**, not in Keycloak: the backend re-applies its
  view (enabled, staff group) on every change.

## Credential security

| Area | What the project does | Standard followed |
|------|----------------------|-------------------|
| **Where credentials live** | Only in **Keycloak** — the app database holds no password hash or TOTP secret. Keycloak hashes passwords with **Argon2id** (migrated hashes are kept as they are). | OWASP Password Storage Cheat Sheet, RFC 9106 |
| **Password policy** (Keycloak realm) | 12–128 characters, any characters incl. spaces; no composition rules or forced expiry. Rejects the username or e-mail (or containing the username), the last 3 passwords and ~180 common/expected passwords (`keycloak/password-blacklists/`). | NIST SP 800-63B §3.1.1.2, OWASP ASVS 2.1 |
| **Temporary passwords** | Seeded, migrated, admin-created and admin-reset passwords are temporary: Keycloak makes the user choose their own before the sign-in completes. Admin-generated ones are random and shown **once**; an admin-chosen one must pass the same policy. | ASVS 2.1.6 / 2.3.1 |
| **Brute force** | Keycloak **lock-out**: 5 wrong passwords lock the account for 15 min (an admin password reset lifts it). | NIST 800-63B §5.2.2 |
| **Forgot password** | *Forgot Password?* on the sign-in page: Keycloak e-mails a signed, single-use, time-limited link (via the `email` service). TOTP is still required at the next sign-in. | OWASP Forgot Password Cheat Sheet |
| **Change password / authenticator / sessions** | Self-service in the Keycloak account page, linked from **Security**: change password, add / replace the authenticator, *Device activity* (sign out other sessions). | ASVS 2.1.5, 3.3 |
| **Sessions & tokens** | SSO session idle 30 min, maximum 8 h; access tokens 5 min (refreshed server-side — tokens never reach the browser). Authorization code flow with **PKCE**, `state` and `nonce`; ID, access and logout tokens are **signature-checked** (RS256) with issuer / audience / expiry. The backend accepts only access tokens meant for it (`aud=servicejourney-api`) and issued to the console (`azp`). | OAuth 2.0 Security BCP, OIDC Core, OIDC Back-Channel Logout |
| **Service-to-service auth** | The console and both services authenticate to the backend with **client credentials** (`X-Client-Id`/`X-Client-Secret`). `/api/service/access` refuses anonymous callers, and a service may only ask about **itself**. | Zero-trust / least privilege |
| **Secrets** | Nothing secret is in `docker-compose.yml`: DB passwords, signing keys, client secrets, OIDC client secrets and the Keycloak admin password live in the git-ignored **`.env`**, created by `python scripts/generate_secrets.py` (CSPRNG). Apps **refuse to start** without them. | 12-factor, OWASP Secrets Management |
| **Console hardening** | CSRF check of `Origin`/`Referer` on every POST (on top of `SameSite=Lax`), `Secure; HttpOnly` cookies, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `Cache-Control: no-store`. | OWASP CSRF Prevention Cheat Sheet |
| **Audit** | Keycloak's sign-in and admin events (sign-ins, failed sign-ins, lock-outs, password and authenticator changes, reset e-mails, sign-outs) are pulled into the tamper-evident [audit trail](#audit-trail) with the backend's own events. | ASVS 7.x |

**Administrator tools** (Accounts table): **Reset password** (temporary password, shown
once, lock-out lifted, user signed out everywhere), **Sign out** (ends all the user's
sessions in every app), and — for 2FA — **Reset**.

> **E-mails in the POC:** Keycloak's reset e-mails go to the `email` container, which keeps
> a local mailbox at <https://localhost:8090>. The port is published on **127.0.0.1 only**
> (the mailbox has no sign-in and shows reset links, so it must not be reachable from other
> machines). Configure SMTP in `.env` to deliver e-mails for real.

**Verify it:** `python scripts/test_sso.py` — 41 automated checks: accounts in Keycloak,
staff TOTP set-up and sign-in, employees on password only, SSO across all three apps,
single sign-out, the API's token checks (missing, tampered, wrong app, ID token), password
policy, lock-out, every admin action, suspension on shift, the full grant workflow and the
audit trail. Set `MAILBOX_URL` to include the forgot-password e-mail.

## The workflow

The requestor raises one of **four** request types, and **every** one follows the
**same lifecycle** — it always ends with the requestor booking an appointment and
then EUSS, never earlier:

```
Requestor submits
   → HR → Director → Approving Authority        (any may reject → back to requestor)
   → IT Admin "Process"                         (executes the access change)
   → Requestor books an APPOINTMENT             (always — for every type)
   → EUSS provisions / retrieves the asset      → COMPLETED
```

| Type | For whom | IT "Process" does | EUSS does |
|------|----------|-------------------|-----------|
| **onboard** | an employee **with no department** | places them in the dept + grants its services | provisions their asset(s) |
| **grant** | an **existing** employee (already in a dept) needing one more service | grants the service (+ account if laptop) | provisions the asset(s) |
| **shift** | an existing employee moving department | revokes old services, moves them, grants new dept's services | provisions new / releases old assets |
| **return** | offboarding — taking access back | approves only | revokes the **laptop+account pair** and retrieves **both assets** |

* **Onboard vs grant:** onboarding is only for department-less employees; a grant
  (raise access) is only for employees who already have a department.
* **Shift suspension:** while a shift is in flight the employee is **`suspended`** —
  the service dockers block all their logins (and end live sessions) until the
  transfer completes, so someone moving teams can never keep access they shouldn't.
  A rejected shift lifts the suspension and leaves them in place.
* **Assets follow access:** EUSS uses `sync_assets`, giving an employee exactly one
  asset per resource kind they hold (Laptop, Account) and returning any they no
  longer need. Every hand-over/return is recorded as a **custody event**, so EUSS
  can open an asset's **chain-of-custody history** (where a laptop has been, who the
  last holder was).
* Every action is recorded in an immutable **timeline** (`request_events`), and the
  **history** view shows the requestor and whether they acted for themselves or on
  behalf of someone.
* As a request travels **up the hierarchy**, every approving account (HR → Director →
  AA → IT Admin, plus EUSS and admin oversight) sees the **reason given** by the
  requestor directly in their queue, alongside who raised it and whether it was raised
  on someone's behalf.
* The admin can no longer grant/remove access directly — it only happens through
  the workflow. Every user carries a **department**, shown throughout the console.

## Mandatory Job Rotation (MJR) tracker

MND rotates officers who have spent **4 years** in the same department. Before this
tracker, HR followed each officer's timeline by hand and e-mailed every Director
(HR process 10, "Mandatory Job Rotation"). Now the console does it:

```
officer joins a department (onboard / shift processed)  -> department_since = that day
   ... 4 years later: hourly check flags the officer as DUE
   -> Directors e-mailed (copy to HR), once per officer
   -> Director, "Due for rotation" panel:
        Keep   + reason      -> recorded; back for review in 12 months
        Rotate + department  -> Requestors e-mailed; "Rotations to raise" panel shows a
                                PRE-FILLED department shift request
   -> Requestor submits it   -> normal workflow HR -> Director -> AA -> IT -> EUSS
   -> IT processes the shift -> new department + its access; rotation clock restarts
```

| Who | Sees / does |
|-----|-------------|
| **Director** | *Due for rotation*: officers at or past the mark with their department start date, time in department and due date. **Rotate** (pick the department, optional note) or **Keep** (reason required). *Rotation tracker*: officers due within 90 days, decisions made, transfers in progress (with a link to the shift request). |
| **HR** | Same lists, read-only, plus **Check now & e-mail Directors**. Receives a copy of every alert and every decision. |
| **Requestor** | *Rotations to raise*: one pre-filled shift request per Rotate decision (justification filled in from the decision); add documents and submit. |
| **Administrator** | Can correct an officer's *in department since* date (e.g. from HR records) in the employee row; moving someone to another department directly also restarts the clock. |

* **States:** *Within term* → *Due soon* (90 days before) → *Due — decision needed* →
  *Kept* (until the review date, then due again) or *Rotate — shift to be raised* →
  *Shift in progress* → back to *Within term* in the new department. A rejected shift
  puts the officer back to *Due* so the Director can decide again.
* **Access follows the move:** the shift is an ordinary department shift, so the officer
  is suspended while it is in flight and gets the new department's services when IT
  processes it — no separate access request.
* **Audited:** `rotation_due_notified`, `rotation_keep`, `rotation_rotate` (category
  *workflow*) in the tamper-evident trail; the shift request's history says it is an MJR
  rotation; date corrections appear in `account_updated` (old → new).
* **Demo data:** tenures are seeded *relative to today*: **alice** (5 yr 3 mo) and
  **frank** (4 yr 2 mo) are due, **grace** (3 yr 10 mo) is due soon, the rest are within
  term. On an existing database, employees already in a department start their clock
  from their last completed onboard / shift into it (or today if there is none).
* **Tunables** (backend environment, optional): `MJR_ROTATION_YEARS` (4),
  `MJR_DUE_SOON_DAYS` (90), `MJR_KEEP_REVIEW_MONTHS` (12), `MJR_CHECK_INTERVAL_SECONDS`
  (3600 — the check also runs at start-up).

## Resources & quota

The two placeholder services now represent two **resources**: **Personal Laptop**
and **Account**. Each has a quota (`allowed`), with `used` derived live from
actual grants and `remaining = allowed − used`. Seeded at **3 laptops / 5 accounts**.

* **Coupling:** provisioning a Personal Laptop consumes **1 laptop + 1 account** —
  IT processing a laptop grant auto-grants the Account resource too. An Account on
  its own consumes only 1 account.

### The laptop / account binding

A laptop and its account are a **bound pair**, because the account *is* the login
identity — a laptop is useless without one:

The **laptop is the higher privilege and depends on the account** (the lower one):

| Event | Effect |
|-------|--------|
| **Laptop revoked** (higher) | only the laptop goes — the **account remains**, and only the laptop asset is returned |
| **Account revoked** (lower) | the **laptop goes too** — a laptop cannot exist without an account, and **both assets** are returned |
| An account **on its own** | valid — an account never implies a laptop |

There is **no "blocked" or "suspended resource" state**: an employee either holds a
usable laptop+account pair, or holds nothing. Taking access away is always done with a
**return/offboard request**, which brings the assets back.

The one hold in the system is at the *employee* level during a **department shift** —
while the transfer is in flight the employee cannot sign in anywhere, and access is
re-established from the new department's template when IT processes it.

Every access change is one of the **four request types** below — there is no separate
suspend/resume workflow, and no way to alter access outside the chain.
There are **two levels of quota**, and a grant is checked against **both**:

| Level | Scope | Managed by |
|-------|-------|------------|
| **Department quota** | how many laptops / accounts *one department* may hold | **IT Admin**, directly from the console |
| **Organisation-wide quota** | the overall cap across every department | **AA proposes** → **IT Admin approves** |

* **Department quotas** — each department has its own laptop and account capacity.
  `used` counts the employees *in that department* holding the resource. **IT Admin**
  edits the numbers inline on their dashboard ("Department quotas" panel). **AA sees
  the same figures read-only** — used / allowed / remaining per department — to inform
  their approvals, but cannot change them. No other role can even view them.
* **AA** sees the organisation-wide allowed / used / remaining and can **propose** a
  new limit; it applies only after **IT Admin approves** (IT can reject with a reason).
* **Every path that hands out a resource is quota-checked** — `grant`, `onboard` and
  `shift` alike. For onboard/shift the check runs against the **destination
  department** *before anything is changed*, so a blocked request leaves the employee
  completely untouched.
* Exceeding **either** cap does not hard-block: IT may proceed but must record an
  override justification, which names the cap that was exceeded — e.g.
  *"Personal Laptop in Engineering (2/2)"* vs *"Account org-wide (6/6)"*.
* A **shift is net-zero org-wide** for a resource the employee already holds (they
  keep one laptop, they just move), so only the destination department's cap applies
  in that case.

### Seeded figures

Deliberately tight so overflow is easy to demonstrate:

| | Laptop | Account |
|---|---|---|
| **Per department** (Engineering / Sales / HR) | 2 | 2 |
| **Organisation-wide** | 5 | 6 |

with **12 employees**: 3 in each department (only `alice` and `bob` start with
resources), plus `dave`, `kate` and `liam` with no department for onboarding.
Assets are plentiful (12 laptops, 15 accounts) so the **quota** is what blocks, not
a shortage of stock.
* **Over-quota override:** if a grant would exceed a quota, IT is not hard-blocked —
  it may proceed but **must enter a justification**, which is recorded on the
  request's timeline (visible to IT) for the audit trail.

## Audit trail

A single, **append-only, tamper-evident** record of every security-relevant event, kept
by the backend and readable only by **IT Admin** (top bar → **Audit trail**).

**What is recorded** (the gaps listed in `remarks.txt`, plus the workflow):

| Category | Events |
|----------|--------|
| `auth` | sign-ins and **failed sign-ins** (from Keycloak, with IP and app), lock-outs, authenticator set-up / removal, password changes, reset e-mails and completed resets, sign-outs, session revocations, migration of credentials to Keycloak |
| `admin` | **edits to users and roles** — account created / updated (each field old → new) / deleted, password reset, MFA reset, forced sign-out; plus Keycloak admin-API changes (`keycloak_<operation>_<resource>`) |
| `quota` | **IT's department-quota changes** (old → new), org-wide quota proposals / approvals / rejections, over-quota overrides with the justification |
| `config` | departments created, department → service mapping changes |
| `access` | **access checks** by the services (every denial; grants at sign-in), role-based permission denials in the API, every entitlement granted / revoked |
| `workflow` | every request step: submitted, approved, rejected, processed, appointment booked, provisioned, retrieved |
| `asset` | asset provisioned / retrieved (chain of custody) |
| `audit` | downloads of the trail and integrity checks |

Each entry has a sequence number, UTC timestamp (shown in Singapore time on the page), actor (user + role, or `service:…`),
target, outcome, client IP and structured details. Entries never reference other tables,
so deleting an account does not remove its history. Keycloak events are copied in
every 10 seconds (and whenever the trail is opened), each exactly once.

**Why it cannot be changed**

1. **No API changes it** — there are no update/delete endpoints (PUT/PATCH/DELETE → 405).
2. **Least-privilege database role.** API requests run as `sj_app`, which holds only
   `SELECT, INSERT` on `audit_log`; schema work at start-up uses the owner account.
3. **Database trigger** rejects `UPDATE`, `DELETE` and `TRUNCATE` on `audit_log` for every
   role, including the owner.
4. **HMAC hash chain.** `hash = HMAC-SHA256(AUDIT_HMAC_KEY, prev_hash ‖ payload)`. Editing,
   deleting or re-ordering any entry — even by a database superuser who disables the
   trigger — breaks the chain, and without the key (kept in `.env`, not the database) a
   valid chain cannot be forged.
5. **Second copy on the backend.** Every entry is also appended to
   `/var/log/servicejourney/audit-YYYY-MM-DD.jsonl` on the backend-only `auditlog` volume;
   **Verify integrity** compares it with the table, which also catches entries cut off the
   end.

**Download:** *Download CSV* (spreadsheet-safe: formula injection neutralised) or
*Download JSONL (signed)*, both honouring the current filters. Each download returns a
SHA-256 checksum (`X-Audit-SHA256`) and is itself audited. The JSONL file contains each
signed payload with its hashes, so it can be re-verified independently with
`AUDIT_HMAC_KEY` (see `scripts/test_audit.py`) and fed to a SIEM.

**Verify it:** `python scripts/test_audit.py` — event coverage, IT-only access, no write
API, chain verification, independent re-verification of a download and, if the `docker`
CLI is available, that the database refuses `UPDATE`/`DELETE`/`TRUNCATE`.

> Keep `AUDIT_HMAC_KEY` safe and unchanged — with a new key, earlier entries no longer
> verify. Older volumes' `auth_events` rows are migrated into the chain automatically.

## Notifications, documents & request options

* **Email notifications.** Every account has an email address (admin-editable). On
  every request event the backend sends three kinds of message via the `email` docker:
  1. **ACTION REQUIRED** → to whoever the request has just landed on (the next
     approver, or the requestor when it is time to book the appointment);
  2. **Progress** → to the requestor and the employee the request concerns;
  3. **Confirmation** → to the person who just approved / rejected / actioned it.

  **Real delivery is off by default.** The `email` service records every message in a
  local mailbox at <https://localhost:8090> and only *sends* if SMTP is configured —
  the mailbox page and each message's status say plainly which mode you are in.
  To send for real, copy [`.env.example`](.env.example) to `.env`, fill in your own
  SMTP details, then `docker compose up -d --build email`.
  For Gmail you need 2-Step Verification and a 16-character **App Password**
  (your normal password will be rejected).
* **Supporting documents.** The requestor can attach files to any request; they are
  stored (base64) with the request and are downloadable from its detail view.
* **Self / on behalf.** Each request records whether the requestor is acting for
  themselves or on behalf of someone — shown in the request detail and emails.
* **Asset provisioning requires valid tags.** EUSS must pick a **specific asset tag
  for each resource kind** the employee needs; every tag is validated to be an
  existing, **available** asset of **exactly that kind** (a Laptop slot cannot take an
  Account tag, an already-assigned tag, or a made-up tag). The EUSS page only offers
  matching available tags, and the backend re-checks on submit.

## Architecture

```
                         ┌──────────── keycloak (8443) ── keycloak-db
                         │   sign-in, TOTP, sessions (OIDC)
 Browser ──▶ accessmanagement (8080) ─┐   (role-aware BFF; tokens kept server-side)
 Browser ──▶ service1        (8081) ─┼─▶ backend (5000) ──▶ postgres
 Browser ──▶ service2        (8082) ─┘        (source of truth + workflow + audit)
```

Frontends never touch the database — they call the backend. Browsers are sent to
Keycloak to sign in; the console calls the backend with the user's access token,
and the services ask the backend whether the signed-in user is granted them.

## HTTPS

Everything speaks **HTTPS** — the pages you open in a browser *and* the calls the
containers make to each other. Traffic is both encrypted and **authenticated**: no
service is configured with `verify=False`.

The repo ships a small private Certificate Authority. Regenerate or rotate it with:

```bash
bash scripts/generate-certs.sh
```

That produces `certs/`:

| File | Purpose |
|------|---------|
| `ca.crt` | the CA every container trusts (`CA_BUNDLE`) — import it into your browser to remove the warning |
| `ca.key` | CA private key — **git-ignored**, never leaves your machine |
| `server.crt` / `server.key` | the certificate each service presents (`TLS_CERT` / `TLS_KEY`) |

One certificate covers every name in play, so the same file works for the browser and
for container-to-container calls:

```
DNS: localhost, backend, email, accessmanagement, service1, service2, postgres, keycloak
IP : 127.0.0.1, ::1
```

`certs/` is mounted read-only into all five app containers and Keycloak, and internal URLs are now
`https://backend:5000` and `https://email:5000`. The console's session cookie is issued
`Secure; HttpOnly; SameSite=Lax`.

**Your browser will warn on first visit** — the CA is self-signed, so it isn't in any
trust store. Click through, or import `certs/ca.crt` as a trusted root to silence it.

**Calling the API from the command line** needs the CA:

```bash
curl --cacert certs/ca.crt https://localhost:5000/health
```

On **Windows**, curl uses Schannel, which insists on a revocation endpoint a private CA
does not have (`CERT_TRUST_REVOCATION_STATUS_UNKNOWN`). Add `--ssl-no-revoke`:

```bash
curl --ssl-no-revoke --cacert certs/ca.crt https://localhost:5000/health
```

> **Not covered:** the backend↔Postgres link. Postgres does not speak HTTPS — it has its
> own TLS on the wire — so it is still an unencrypted connection on the internal network.
> Enabling it means mounting the key into the Postgres container with `0600`/`postgres`
> ownership and setting `sslmode=require`.

## Running it

```bash
python scripts/generate_secrets.py   # writes strong random secrets into .env (re-run after
                                     # updates: it only adds keys that are missing)
bash scripts/generate-certs.sh       # only if certs/ predates Keycloak (needs the 'keycloak' name)
docker compose up -d --build
```

Keycloak takes about 30 seconds to start (the apps show *sign-in service not reachable*
until then). Open the console at <https://localhost:8080> and the services at
<https://localhost:8081> / <https://localhost:8082>. The backend applies the schema,
seeds, and creates / updates every account in Keycloak automatically on start.

> **Upgrading from CHANGE 003 or earlier:** run `python scripts/generate_secrets.py` (adds
> the Keycloak secrets) and `bash scripts/generate-certs.sh` (adds `keycloak` to the
> certificate — re-import `certs/ca.crt` in your browser). Existing accounts, passwords
> and authenticator apps are migrated into Keycloak on start. For a clean demo state use
> `docker compose down -v` first.

## Try the full journey

The first time you sign in as each account you will set up TOTP (staff) and replace the
temporary password (`username`) with your own. To switch account, **Sign out** first
(single sign-on would otherwise sign you straight back in as the same person).

1. **`req`** → *Raise access request*: Alice → Personal Laptop → Submit.
2. **`hr`**, then **`director`**, then **`aa`** → each sees the request in their
   queue → **Approve**. (`aa` also sees the quota panel.)
3. **`itadmin`** → **Process (grant access)**. Alice can now sign into both the
   **Personal Laptop** (8081) and **Account** (8082) resources — a laptop grant
   auto-provisions the account. If quota is exhausted, IT is prompted for an
   override justification.
4. **`req`** → *My requests* → **Book** an appointment on that request.
5. **`euss`** → *Provision SN assets* → **Provision** (assigns e.g. `SN-1001`).
   Request is **Completed**.
6. Try a **rejection**: raise another request and have `hr` **Reject** it — it
   returns to the requestor as *Rejected* with the reason.
7. **Return / offboarding**: **`req`** raises a *return* for Alice → Service One,
   then **`euss`** → *Retrieve* → access is revoked (and if Alice is signed into
   Service One, she is bounced within ~5 seconds).
8. **Mandatory Job Rotation**: open the **mailbox** (<https://localhost:8090>) — `director` has an automatic
   *"MJR — 2 officer(s) due for rotation"* e-mail (alice, frank). Sign in as **`director`**
   → *Due for rotation* → **Keep** frank with a reason, **Rotate** alice to Sales. Sign in as
   **`req`** → *Rotations to raise* → **Raise shift request** (pre-filled). Take it through
   `hr` → `director` → `aa` → `itadmin` as usual: alice moves to Sales with Sales' access, and
   the *Rotation tracker* shows her clock restarted. (If you already ran step 7 on alice, use
   frank for the rotation instead.)

## Live session termination

The service dockers re-validate against the backend on every `/success` load and
poll `/session/status` every 5 seconds, so revoking access (via a completed
return, deactivation, or offboarding) ends any live session automatically. Signing out
anywhere (or an administrator's *Sign out*) ends the session in every app through
Keycloak's back-channel logout.

Service sessions are kept on the server (the cookie holds only a random handle) and are
limited the same way as the Keycloak SSO session:

| Limit | Rule |
|---|---|
| Idle | 30 min without a page view (the 5-second status poll does not count) |
| Absolute | 8 h after sign-in, whatever the activity |
| Keycloak check | every 60 s the session's refresh token is presented to Keycloak; if Keycloak refuses it (user disabled / deleted, SSO session ended or expired) the session ends. If Keycloak is merely unreachable the session carries on, so an outage does not sign everyone out |
| Forced sign-out | the backend refuses sessions that began before the employee's last forced sign-out, password or 2FA reset — this works even while Keycloak is down |
| Restart | a service restart ends its sessions (SSO signs users straight back in) |

Tunable per service with `IDLE_TIMEOUT_MINUTES`, `MAX_SESSION_HOURS`, `KC_CHECK_SECONDS`.

**Trade-off during a Keycloak outage.** The backend validates access tokens locally (JWKS
keys cached in memory), so it keeps working while Keycloak is down, but the console needs
Keycloak to refresh its 5-minute token, so console users are signed out within ~5 minutes;
signed-in service users carry on (subject to the limits above). A backend restart during an
outage cannot fetch the signing keys and refuses all tokens until Keycloak is back.

## API (backend) — highlights

Every call needs the caller's client credentials; user calls also need
`Authorization: Bearer <Keycloak access token>`. Sign-in, password change, forgot
password, TOTP set-up and sessions are Keycloak's (no backend endpoints any more).

| Method | Path | Role |
|--------|------|------|
| GET | `/api/me` | any auth — who am I, role, 2FA used, sign-in time |
| POST | `/api/employees/{id}/mfa/reset` | admin (removes the authenticator in Keycloak) |
| GET | `/api/audit` | **it_admin** — search the audit trail (filters, paging) |
| GET | `/api/audit/verify` | **it_admin** — re-check the whole hash chain |
| GET | `/api/audit/export?format=csv\|jsonl` | **it_admin** — download (filtered) |
| POST | `/api/employees/{id}/password/reset`, `/api/employees/{id}/sessions/revoke` | admin (via Keycloak) |
| POST | `/api/service/access` | **services only** (client credentials), about themselves |
| GET | `/api/services`, `/api/departments`, `/api/employees` | any auth |
| POST/PUT/DELETE | `/api/employees[/id]` | admin |
| GET | `/api/assets` | admin, euss |
| POST | `/api/requests` | requestor (a `shift` may carry `rotation_decision_id`) |
| GET | `/api/requests/mine`, `/api/requests/queue` | any auth (role-scoped) |
| GET | `/api/requests`, `/api/requests/{id}` | admin / owner+staff |
| POST | `/api/requests/{id}/approve` | hr, director, aa (their stage) |
| POST | `/api/requests/{id}/reject` | approver/it at their stage |
| POST | `/api/requests/{id}/process` | it_admin (grants access) |
| POST | `/api/requests/{id}/appointment` | requestor (owner) |
| POST | `/api/requests/{id}/provision` | euss (grant) |
| POST | `/api/requests/{id}/retrieve` | euss (return → revokes access) |
| GET | `/api/rotations` | director, hr — everyone on the MJR clock, with state |
| POST | `/api/rotations/{employee_id}/decision` | director — `{"decision":"keep","reason":…}` or `{"decision":"rotate","department_id":…}` |
| GET | `/api/rotations/planned` | requestor — rotate decisions waiting for a shift request |
| POST | `/api/rotations/check` | hr — run the due check now (e-mails Directors) |
| GET | `/api/quotas` | aa, it_admin, admin |
| POST | `/api/quota-requests` | aa (propose new limit) |
| GET | `/api/quota-requests` | aa, it_admin, admin |
| POST | `/api/quota-requests/{id}/approve` \| `/reject` | it_admin |

## Resetting

```bash
docker compose down -v   # drops the app AND Keycloak databases; fresh seed + realm import on next 'up'
```
