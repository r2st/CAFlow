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
    ...overrides,
  }
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
