# CAFlow — AI Practice Management for Chartered Accountants

## Problem

India has 400K+ practicing Chartered Accountants managing hundreds of clients each. Most rely on Excel spreadsheets, WhatsApp groups, and manual tracking for:

- **Compliance deadlines**: GST (monthly/quarterly), TDS (monthly), IT returns (annual), ROC filings, audit deadlines — each client has different due dates
- **Document collection**: Chasing clients for invoices, bank statements, Form 16s via WhatsApp — no structured intake
- **Client communication**: Status updates scattered across WhatsApp, email, phone calls — no audit trail
- **Task delegation**: Senior CAs manually assign work to juniors with no visibility into progress
- **Revenue leakage**: No systematic billing — many CAs undercharge or forget to invoice for additional work

## Solution

AI-powered practice management platform purpose-built for Indian CAs, automating compliance tracking, document collection, client communication, and billing.

## Core Features (MVP)

### 1. Compliance Calendar
- Pre-loaded Indian compliance calendar (GST, TDS, IT, ROC, audit dates)
- Per-client deadline tracking with automatic due date calculation
- Color-coded status: upcoming (green), due soon (amber), overdue (red), filed (blue)
- Bulk filing status update
- Smart reminders: escalating notifications as deadlines approach

### 2. Client Portal
- Each client gets a simple portal (no login required — magic link)
- Document upload with AI categorization (bank statement, invoice, Form 16, etc.)
- Filing status visibility for the client
- Secure document sharing (reports, computation sheets)

### 3. AI Document Processing
- Auto-categorize uploaded documents using free LLMs
- Extract key data from uploaded documents (PAN, GSTIN, financial figures)
- Flag missing documents per compliance requirement
- Generate document checklists per filing type

### 4. Task Management
- Create tasks from compliance deadlines automatically
- Assign to team members with due dates
- Progress tracking with status updates
- Workload view across team

### 5. Smart Reminders & Communication
- Automated client reminders for pending documents (WhatsApp/Email/SMS)
- Template-based communication (filing confirmation, fee reminder, document request)
- AI-drafted messages based on context

### 6. Billing & Invoicing
- Service-wise fee configuration per client
- Auto-generate invoices on filing completion
- Payment tracking with reminders
- Revenue dashboard per client, per service, per period

### 7. Dashboard & Analytics
- Practice-wide compliance status at a glance
- Revenue metrics and projections
- Team productivity analytics
- Client profitability analysis

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python/FastAPI |
| Frontend | React + Vite |
| Database | PostgreSQL |
| Cache/Queue | Redis + Celery |
| AI | OpenRouter free models (document categorization, data extraction, message drafting) |
| Notifications | WhatsApp Business API (future), Email (SMTP), SMS (MSG91) |
| Auth | JWT + magic links for clients |

## Data Model (Core Tables)

- `firms` — CA firm details, ICAI registration, plan
- `practitioners` — individual CAs/staff within firm, role (partner/manager/junior)
- `clients` — PAN, GSTIN, entity type, contact details, assigned practitioner
- `compliance_types` — GST monthly, TDS quarterly, IT return, ROC annual, etc.
- `compliance_items` — client + compliance_type + period + status + due_date + filed_date
- `documents` — uploaded file, category, client, compliance_item link, extracted_data JSON
- `tasks` — title, assignee, compliance_item link, status, due_date
- `invoices` — client, items, amount (paise), status, payment_date
- `reminders` — type (document/payment/filing), channel, sent_at, status
- `audit_log` — all actions tracked

## Pricing

- **Solo**: ₹999/month — 1 CA, up to 50 clients, basic compliance tracking
- **Practice**: ₹2,499/month — up to 5 users, 200 clients, full features
- **Firm**: ₹4,999/month — unlimited users, unlimited clients, white-label client portal, API access

## Deployment

- Hetzner VPS
- Domain: caflow.aiknol.com
- Systemd services: caflow-api, caflow-worker, caflow-beat, caflow-web

## Competition

| Competitor | Gap |
|---|---|
| Saral TDS/ClearTax | Filing tools, not practice management |
| Zoho Practice | Generic, not India CA-specific |
| TDSMAN | Desktop-only, outdated UI |
| Excel/WhatsApp | No automation, no audit trail |
| **CAFlow** | India CA-specific, AI-powered, affordable, modern |

## MVP Timeline

Week 1: Project setup, auth, firm/client/practitioner models, compliance calendar
Week 2: Client portal, document upload + AI categorization
Week 3: Task management, automated reminders
Week 4: Billing, dashboard, deploy
