# ServiceJourney — Proof of Concept

A container-based system demonstrating a central **ServiceJourney** access-management service
that is the *single source of truth* for which employees may access which
downstream services. Access changes flow through a **multi-role approval
workflow** (Requestor → HR → Director → Approving Authority → IT Admin → EUSS)
rather than a single admin toggling access directly.

> ⚠️ **POC only.** Passwords are stored in **plaintext** by design (per the
> requirements) and secrets are hard-coded. Do not use any of this as-is.

## Containers

| Container | Folder | Port (host) | Purpose |
|-----------|--------|-------------|---------|
| `postgres` | *(official image)* | — | SQL database |
| `backend` | [`services/backend`](services/backend) | 5000 | REST API + source of truth + workflow engine |
| `accessmanagement` | [`services/accessmanagement`](services/accessmanagement) | 8080 | Role-aware workflow console |
| `service1` | [`services/service1`](services/service1) | 8081 | **Personal Laptop** resource (placeholder service) |
| `service2` | [`services/service2`](services/service2) | 8082 | **Account** resource (placeholder service) |
| `email` | [`services/email`](services/email) | 8090 | Notification service + **mailbox** web page (POC stand-in for real mail) |

## Project structure

Both main apps are proper Flask **packages** (blueprints + modules), not single files.
The console is **server-side rendered** — the browser only receives HTML plus a tiny
progressive-enhancement script, so the application logic is **not shipped to / viewable
in the browser**.

```
services/
  backend/                     REST API (source of truth)
    run.py                     entrypoint (bootstrap DB, serve)
    app/
      __init__.py              app factory (registers blueprints)
      config.py  db.py  auth.py  mailer.py  core.py   (config, DB, tokens, email, domain logic)
      api/       auth.py reference.py admin.py requests.py quotas.py   (route blueprints)
      sql/       schema.sql  seed.sql
  accessmanagement/            server-rendered console
    run.py
    app/
      __init__.py              app factory
      config.py  backend_client.py  labels.py
      views/     auth.py dashboard.py requests.py admin.py quotas.py assets.py   (blueprints)
      templates/ base.html login.html dashboard/*.html request_detail.html …
      static/    style.css  app.js  (small, generic UI glue only)
```

Actions use **POST → redirect → GET** with flash messages; the console talks to the
backend API with the session token via `backend_client`.

## Roles & demo accounts

All console accounts use **username = password**. Sign in at <http://localhost:8080>.

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

## Notifications, documents & request options

* **Email notifications.** Every account has an email address (admin-editable). On
  every request event the backend sends three kinds of message via the `email` docker:
  1. **ACTION REQUIRED** → to whoever the request has just landed on (the next
     approver, or the requestor when it is time to book the appointment);
  2. **Progress** → to the requestor and the employee the request concerns;
  3. **Confirmation** → to the person who just approved / rejected / actioned it.

  **Real delivery is off by default.** The `email` service records every message in a
  local mailbox at <http://localhost:8090> and only *sends* if SMTP is configured —
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
 Browser ──▶ accessmanagement (8080) ─┐   (role-aware BFF; token kept server-side)
 Browser ──▶ service1        (8081) ─┼─▶ backend (5000) ──▶ postgres
 Browser ──▶ service2        (8082) ─┘        (source of truth + workflow)
```

Frontends never touch the database — they call the backend. The console proxies
dashboard calls with the session's token; service logins are validated by the
backend against live access.

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
DNS: localhost, backend, email, accessmanagement, service1, service2, postgres
IP : 127.0.0.1, ::1
```

`certs/` is mounted read-only into all five app containers, and internal URLs are now
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
docker compose up --build
```

Open the console at <http://localhost:8080> and the services at
<http://localhost:8081> / <http://localhost:8082>. The backend applies the
schema + seed automatically on first start.

## Try the full journey

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

## Live session termination

The service dockers re-validate against the backend on every `/success` load and
poll `/session/status` every 5 seconds, so revoking access (via a completed
return, deactivation, or offboarding) ends any live session automatically.

## API (backend) — highlights

| Method | Path | Role |
|--------|------|------|
| POST | `/api/login` | any active account |
| POST | `/api/service/login`, `/api/service/authorized` | services |
| GET | `/api/services`, `/api/departments`, `/api/employees` | any auth |
| POST/PUT/DELETE | `/api/employees[/id]` | admin |
| GET | `/api/assets` | admin, euss |
| POST | `/api/requests` | requestor |
| GET | `/api/requests/mine`, `/api/requests/queue` | any auth (role-scoped) |
| GET | `/api/requests`, `/api/requests/{id}` | admin / owner+staff |
| POST | `/api/requests/{id}/approve` | hr, director, aa (their stage) |
| POST | `/api/requests/{id}/reject` | approver/it at their stage |
| POST | `/api/requests/{id}/process` | it_admin (grants access) |
| POST | `/api/requests/{id}/appointment` | requestor (owner) |
| POST | `/api/requests/{id}/provision` | euss (grant) |
| POST | `/api/requests/{id}/retrieve` | euss (return → revokes access) |
| GET | `/api/quotas` | aa, it_admin, admin |
| POST | `/api/quota-requests` | aa (propose new limit) |
| GET | `/api/quota-requests` | aa, it_admin, admin |
| POST | `/api/quota-requests/{id}/approve` \| `/reject` | it_admin |

## Resetting

```bash
docker compose down -v   # drops the Postgres volume; fresh seed on next 'up'
```
