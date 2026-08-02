/** Shared API-shaped fixtures for the frontend tests. */

export const PRACTITIONER = {
  id: 'p-1',
  firm_id: 'f-1',
  full_name: 'Anita Sharma',
  email: 'anita@sharma-ca.in',
  phone: null,
  membership_number: '123456',
  role: 'owner',
  is_active: true,
  last_login_at: null,
  created_at: '2026-01-01T00:00:00Z',
}

export const FIRM = {
  id: 'f-1',
  name: 'Sharma & Associates',
  icai_registration_number: '012345N',
  email: 'office@sharma-ca.in',
  phone: null,
  pan: null,
  gstin: null,
  city: 'Pune',
  state: 'Maharashtra',
  plan: 'practice',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
}

export const CLIENT = {
  id: 'c-1',
  firm_id: 'f-1',
  assigned_practitioner_id: null,
  name: 'Nimbus Textiles Pvt Ltd',
  entity_type: 'private_limited',
  pan: 'AABCN2345P',
  gstin: '27AABCN2345P1Z5',
  tan: null,
  cin: null,
  contact_person: 'Rohit Nair',
  email: 'accounts@nimbustextiles.in',
  phone: '+919900112233',
  whatsapp: null,
  address: null,
  state: 'Maharashtra',
  gst_registered: true,
  gst_filing_frequency: 'monthly',
  tds_applicable: true,
  income_tax_applicable: true,
  tax_audit_applicable: true,
  roc_applicable: true,
  payroll_applicable: false,
  onboarded_on: '2026-04-01',
  is_active: true,
  notes: null,
  service_fees: {},
  created_at: '2026-04-01T00:00:00Z',
  updated_at: '2026-04-01T00:00:00Z',
}

export function complianceItem(overrides = {}) {
  return {
    id: 'ci-1',
    firm_id: 'f-1',
    client_id: 'c-1',
    compliance_type_id: 'ct-1',
    assigned_practitioner_id: null,
    period_label: '2026-07',
    period_start: '2026-07-01',
    period_end: '2026-07-31',
    due_date: '2026-08-20',
    status: 'pending',
    filed_on: null,
    acknowledgement_number: null,
    fee_paise: 200000,
    is_billed: false,
    notes: null,
    created_at: '2026-08-01T00:00:00Z',
    client_name: 'Nimbus Textiles Pvt Ltd',
    compliance_type_code: 'GSTR3B_MONTHLY',
    compliance_type_name: 'GSTR-3B (Monthly) — Summary return & tax payment',
    category: 'gst',
    form_number: 'GSTR-3B',
    display_status: 'due_soon',
    days_remaining: 5,
    ...overrides,
  }
}

export function calendarResponse(items) {
  const buckets = {}
  for (const item of items) {
    const bucket = (buckets[item.period_label] ??= {
      period_label: item.period_label,
      total: 0,
      upcoming: 0,
      due_soon: 0,
      overdue: 0,
      filed: 0,
    })
    bucket.total += 1
    if (bucket[item.display_status] !== undefined) bucket[item.display_status] += 1
  }
  return {
    from_date: '2026-07-01',
    to_date: '2027-01-31',
    total: items.length,
    limit: 200,
    offset: 0,
    summary: {
      period_label: 'all',
      total: items.length,
      upcoming: items.filter((i) => i.display_status === 'upcoming').length,
      due_soon: items.filter((i) => i.display_status === 'due_soon').length,
      overdue: items.filter((i) => i.display_status === 'overdue').length,
      filed: items.filter((i) => i.display_status === 'filed').length,
    },
    buckets: Object.values(buckets),
    items,
  }
}

export function sharedDocument(overrides = {}) {
  return {
    id: 'd-1',
    original_filename: 'GSTR-3B-July.pdf',
    category: 'gst_return',
    size_bytes: 184320,
    uploaded_via_portal: false,
    created_at: '2026-08-01T09:00:00Z',
    compliance_label: 'GSTR-3B · 2026-07',
    ...overrides,
  }
}

/** The `/portal/me` payload, as a client with one outstanding checklist sees it. */
export function portalOverview(overrides = {}) {
  return {
    client_id: 'c-1',
    client_name: 'Nimbus Textiles Pvt Ltd',
    firm_name: 'Sharma & Associates',
    firm_email: 'office@sharma-ca.in',
    firm_phone: '+912041234567',
    contact_person: 'Rohit Nair',
    summary: {
      total: 2,
      overdue: 1,
      due_soon: 1,
      upcoming: 0,
      filed: 0,
      documents_outstanding: 2,
      amount_due_paise: 236000,
      invoices_unpaid: 1,
    },
    filings: [
      {
        id: 'ci-1',
        compliance_type_name: 'GSTR-3B (Monthly) — Summary return & tax payment',
        form_number: 'GSTR-3B',
        period_label: '2026-07',
        due_date: '2026-08-20',
        display_status: 'due_soon',
        status: 'pending',
        filed_on: null,
        acknowledgement_number: null,
        days_remaining: 5,
        missing_documents: ['Sales register', 'Purchase register'],
      },
      {
        id: 'ci-2',
        compliance_type_name: 'TDS Return (Q1)',
        form_number: '24Q',
        period_label: '2026-Q1',
        due_date: '2026-07-31',
        display_status: 'overdue',
        status: 'pending',
        filed_on: null,
        acknowledgement_number: null,
        days_remaining: -5,
        missing_documents: [],
      },
    ],
    checklists: [
      {
        compliance_item_id: 'ci-1',
        compliance_type_name: 'GSTR-3B (Monthly) — Summary return & tax payment',
        period_label: '2026-07',
        due_date: '2026-08-20',
        client_id: 'c-1',
        client_name: 'Nimbus Textiles Pvt Ltd',
        requirements: [
          {
            requirement: 'sales_register',
            label: 'Sales register',
            satisfied: false,
            document_ids: [],
          },
          {
            requirement: 'purchase_register',
            label: 'Purchase register',
            satisfied: false,
            document_ids: [],
          },
          {
            requirement: 'bank_statement',
            label: 'Bank statement',
            satisfied: true,
            document_ids: ['d-2'],
          },
        ],
        missing: ['sales_register', 'purchase_register'],
        is_complete: false,
      },
    ],
    shared_documents: [sharedDocument()],
    my_uploads: [
      sharedDocument({
        id: 'd-2',
        original_filename: 'bank-statement-july.pdf',
        category: 'bank_statement',
        uploaded_via_portal: true,
        compliance_label: null,
      }),
    ],
    invoices: [portalInvoice()],
    ...overrides,
  }
}

/** One issued invoice, as the client sees it on the portal. */
export function portalInvoice(overrides = {}) {
  return {
    id: 'inv-1',
    invoice_number: 'INV/FY2026-27/0007',
    issue_date: '2026-07-15',
    due_date: '2026-08-14',
    total_paise: 236000,
    amount_paid_paise: 0,
    balance_paise: 236000,
    status: 'sent',
    is_overdue: false,
    lines: [
      { description: 'GSTR-3B filing — 2026-07', quantity: 1, amount_paise: 200000 },
    ],
    ...overrides,
  }
}

export function task(overrides = {}) {
  return {
    id: 't-1',
    firm_id: 'f-1',
    client_id: 'c-1',
    compliance_item_id: 'ci-1',
    assignee_id: 'p-1',
    created_by_id: 'p-1',
    title: 'Reconcile GSTR-2B for July',
    description: null,
    status: 'todo',
    priority: 'high',
    due_date: '2026-08-18',
    estimated_minutes: 90,
    completed_at: null,
    created_at: '2026-08-01T00:00:00Z',
    client_name: 'Nimbus Textiles Pvt Ltd',
    assignee_name: 'Anita Sharma',
    compliance_type_name: 'GSTR-3B (Monthly) — Summary return & tax payment',
    period_label: '2026-07',
    days_remaining: 3,
    is_overdue: false,
    ...overrides,
  }
}

export const WORKLOAD = {
  as_of: '2026-08-01',
  rows: [
    {
      practitioner_id: 'p-1',
      practitioner_name: 'Anita Sharma',
      role: 'owner',
      open_tasks: 4,
      overdue: 1,
      due_this_week: 2,
      in_progress: 1,
      blocked: 0,
      completed_this_month: 6,
      estimated_minutes: 300,
      by_status: { todo: 3, in_progress: 1 },
    },
    {
      practitioner_id: null,
      practitioner_name: 'Unassigned',
      role: null,
      open_tasks: 2,
      overdue: 0,
      due_this_week: 1,
      in_progress: 0,
      blocked: 0,
      completed_this_month: 0,
      estimated_minutes: 60,
      by_status: { todo: 2 },
    },
  ],
  unassigned_open: 2,
  total_open: 6,
}

export function invoice(overrides = {}) {
  return {
    id: 'inv-1',
    firm_id: 'f-1',
    client_id: 'c-1',
    invoice_number: 'INV-2026-0001',
    issue_date: '2026-07-05',
    due_date: '2026-07-20',
    subtotal_paise: 500000,
    tax_paise: 90000,
    total_paise: 590000,
    amount_paid_paise: 0,
    balance_paise: 590000,
    gst_rate_bps: 1800,
    status: 'sent',
    payment_date: null,
    payment_reference: null,
    notes: null,
    created_at: '2026-07-05T00:00:00Z',
    client_name: 'Nimbus Textiles Pvt Ltd',
    days_overdue: null,
    ...overrides,
  }
}

/** An invoice as `GET /invoices/{id}` returns it — with its lines. */
export function invoiceDetail(overrides = {}) {
  const { lines, ...rest } = overrides
  return {
    ...invoice(rest),
    lines: lines ?? [
      {
        id: 'line-1',
        compliance_item_id: 'ci-1',
        description: 'GSTR-3B (Monthly) — 2026-06',
        quantity: 1,
        unit_price_paise: 300000,
        amount_paise: 300000,
        sac_code: '998222',
      },
      {
        id: 'line-2',
        compliance_item_id: null,
        description: 'Advisory on the new TDS rates',
        quantity: 2,
        unit_price_paise: 100000,
        amount_paise: 200000,
        sac_code: '998311',
      },
    ],
  }
}

export const REVENUE = {
  from_date: '2026-04-01',
  to_date: '2027-03-31',
  invoiced_paise: 2500000,
  collected_paise: 1500000,
  outstanding_paise: 1000000,
  overdue_paise: 400000,
  draft_paise: 200000,
  invoice_count: 8,
  unbilled_paise: 750000,
  by_client: { 'Nimbus Textiles Pvt Ltd': 1200000 },
  by_category: { gst: 900000, tds: 400000 },
}

export const BILLABLE_WORK = {
  clients: [
    {
      client_id: 'c-1',
      client_name: 'Nimbus Textiles Pvt Ltd',
      item_count: 2,
      total_paise: 400000,
      items: [
        {
          compliance_item_id: 'ci-1',
          description: 'GSTR-3B (Monthly)',
          period_label: '2026-06',
          filed_on: '2026-07-18',
          fee_paise: 200000,
        },
        {
          compliance_item_id: 'ci-2',
          description: 'TDS Return (Q1)',
          period_label: '2026-Q1',
          filed_on: '2026-07-28',
          fee_paise: 200000,
        },
      ],
    },
  ],
  total_paise: 400000,
  total_items: 2,
}

export function document(overrides = {}) {
  return {
    id: 'd-1',
    firm_id: 'f-1',
    client_id: 'c-1',
    compliance_item_id: 'ci-1',
    original_filename: 'bank-statement-july.pdf',
    content_type: 'application/pdf',
    size_bytes: 184320,
    checksum_sha256: null,
    category: 'bank_statement',
    category_confidence: 0.92,
    is_category_confirmed: false,
    status: 'processed',
    extracted_data: {},
    satisfies_requirements: ['bank_statement'],
    uploaded_via_portal: true,
    is_shared_with_client: false,
    processing_error: null,
    processed_at: '2026-08-01T09:05:00Z',
    created_at: '2026-08-01T09:00:00Z',
    client_name: 'Nimbus Textiles Pvt Ltd',
    compliance_label: 'GSTR-3B · 2026-07',
    ...overrides,
  }
}

export const OUTSTANDING = {
  from_date: '2026-08-01',
  to_date: '2026-08-31',
  total_items: 1,
  total_missing: 2,
  checklists: [
    {
      compliance_item_id: 'ci-1',
      compliance_type_name: 'GSTR-3B (Monthly)',
      period_label: '2026-07',
      due_date: '2026-08-20',
      client_id: 'c-1',
      client_name: 'Nimbus Textiles Pvt Ltd',
      requirements: [
        { requirement: 'sales_register', label: 'Sales register', satisfied: false, document_ids: [] },
        {
          requirement: 'purchase_register',
          label: 'Purchase register',
          satisfied: false,
          document_ids: [],
        },
        { requirement: 'bank_statement', label: 'Bank statement', satisfied: true, document_ids: ['d-1'] },
      ],
      missing: ['sales_register', 'purchase_register'],
      is_complete: false,
    },
  ],
}

export function reminder(overrides = {}) {
  return {
    id: 'r-1',
    firm_id: 'f-1',
    client_id: 'c-1',
    compliance_item_id: 'ci-1',
    invoice_id: null,
    reminder_type: 'document',
    channel: 'email',
    status: 'scheduled',
    subject: 'GSTR-3B (Monthly) — 2026-07',
    body: 'Dear Nimbus Textiles Pvt Ltd,\n\nWe need a few documents…',
    recipient: 'accounts@nimbustextiles.in',
    scheduled_for: '2026-08-05T09:00:00Z',
    sent_at: null,
    attempt_count: 0,
    error_message: null,
    extra: {},
    created_at: '2026-08-01T00:00:00Z',
    client_name: 'Nimbus Textiles Pvt Ltd',
    ...overrides,
  }
}

/** A `Page[...]` envelope around whatever items a test cares about. */
export function pageOf(items, overrides = {}) {
  return { items, total: items.length, limit: 50, offset: 0, ...overrides }
}

export const DASHBOARD_STATS = {
  total_clients: 3,
  active_clients: 3,
  total_items: 42,
  overdue: 2,
  due_soon: 5,
  upcoming: 30,
  filed_this_month: 5,
  unbilled_fee_paise: 1250000,
  by_category: { gst: 20, tds: 12, income_tax: 5 },
}

export function auditEntry(overrides = {}) {
  return {
    id: 'a-1',
    firm_id: 'f-1',
    actor_practitioner_id: 'p-1',
    actor_label: 'Anita Sharma <anita@sharma-ca.in>',
    action: 'client.update',
    entity_type: 'client',
    entity_id: 'c-1',
    summary: 'Updated Nimbus Textiles Pvt Ltd',
    changes: {
      before: { contact_person: 'Rohit Nair', gst_registered: false },
      after: { contact_person: 'Priya Nair', gst_registered: true },
    },
    ip_address: '203.0.113.4',
    user_agent: 'Mozilla/5.0',
    created_at: '2026-08-01T09:30:00Z',
    ...overrides,
  }
}

export const AUDIT_ACTIONS = {
  actions: [
    { action: 'client.update', count: 12 },
    { action: 'client.create', count: 3 },
  ],
  entity_types: ['client', 'invoice'],
}
