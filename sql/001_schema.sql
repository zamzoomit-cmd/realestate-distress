-- ============================================================
-- Real Estate Distress Lead System - Database Schema
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ============================================================
-- ENUMS
-- ============================================================

CREATE TYPE lead_tier AS ENUM ('hot', 'warm', 'watch', 'ignore');
CREATE TYPE equity_tier AS ENUM ('gold', 'silver', 'bronze', 'below_bronze');
CREATE TYPE scrape_status AS ENUM ('pending', 'running', 'success', 'failed', 'partial');
CREATE TYPE signal_type AS ENUM (
  'foreclosure_notice',
  'auction_within_45_days',
  'probate',
  'open_code_violation',
  'tax_delinquency',
  'absentee_owner',
  'out_of_state_owner',
  'high_equity',
  'stacked_signals'
);

-- ============================================================
-- SOURCE REGISTRY
-- ============================================================

CREATE TABLE data_sources (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  source_key      TEXT UNIQUE NOT NULL,
  county          TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'AZ',
  source_name     TEXT NOT NULL,
  source_type     TEXT NOT NULL, -- foreclosure | probate | code_violation | tax | assessor
  base_url        TEXT NOT NULL,
  scraper_class   TEXT NOT NULL,
  scrape_method   TEXT NOT NULL DEFAULT 'requests', -- requests | playwright | csv | json | api
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  rate_limit_rps  NUMERIC(4,2) NOT NULL DEFAULT 1.0,
  requires_auth   BOOLEAN NOT NULL DEFAULT FALSE,
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- SCRAPE RUNS
-- ============================================================

CREATE TABLE scrape_runs (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  source_id       UUID NOT NULL REFERENCES data_sources(id),
  started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at    TIMESTAMPTZ,
  status          scrape_status NOT NULL DEFAULT 'pending',
  records_found   INT NOT NULL DEFAULT 0,
  records_new     INT NOT NULL DEFAULT 0,
  records_updated INT NOT NULL DEFAULT 0,
  records_skipped INT NOT NULL DEFAULT 0,
  error_message   TEXT,
  error_details   JSONB,
  run_params      JSONB
);

CREATE INDEX idx_scrape_runs_source ON scrape_runs(source_id);
CREATE INDEX idx_scrape_runs_started ON scrape_runs(started_at DESC);
CREATE INDEX idx_scrape_runs_status ON scrape_runs(status);

-- ============================================================
-- RAW SOURCE DOCUMENTS
-- ============================================================

CREATE TABLE source_documents (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  scrape_run_id   UUID REFERENCES scrape_runs(id),
  source_id       UUID NOT NULL REFERENCES data_sources(id),
  source_url      TEXT NOT NULL,
  doc_type        TEXT NOT NULL,
  doc_key         TEXT NOT NULL, -- document number, case number, or APN
  raw_content     TEXT,
  raw_headers     JSONB,
  http_status     INT,
  content_type    TEXT,
  fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  parse_status    TEXT NOT NULL DEFAULT 'pending',
  parse_error     TEXT,
  UNIQUE(source_id, doc_key)
);

CREATE INDEX idx_source_docs_source ON source_documents(source_id);
CREATE INDEX idx_source_docs_run ON source_documents(scrape_run_id);
CREATE INDEX idx_source_docs_key ON source_documents(doc_key);

-- ============================================================
-- PROPERTIES (canonical property records)
-- ============================================================

CREATE TABLE properties (
  id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  apn                 TEXT,                    -- Assessor Parcel Number (normalized)
  apn_raw             TEXT,                    -- original format from source
  county              TEXT NOT NULL,
  state               TEXT NOT NULL DEFAULT 'AZ',

  -- Address (normalized)
  address_full        TEXT,
  address_street      TEXT,
  address_city        TEXT,
  address_zip         TEXT,

  -- Owner info
  owner_name          TEXT,
  owner_mailing_address TEXT,
  owner_mailing_city  TEXT,
  owner_mailing_state TEXT,
  owner_mailing_zip   TEXT,
  is_absentee_owner   BOOLEAN,
  is_out_of_state     BOOLEAN,

  -- Valuation
  assessed_value      NUMERIC(14,2),
  market_value_est    NUMERIC(14,2),
  land_value          NUMERIC(14,2),
  improvement_value   NUMERIC(14,2),
  last_sale_price     NUMERIC(14,2),
  last_sale_date      DATE,
  mortgage_balance_est NUMERIC(14,2),
  equity_est          NUMERIC(14,2),
  equity_tier         equity_tier,

  -- Property details
  property_type       TEXT,
  bedrooms            INT,
  bathrooms           NUMERIC(4,1),
  sqft                INT,
  lot_sqft            INT,
  year_built          INT,
  zoning              TEXT,

  -- Lead scoring
  lead_score          INT NOT NULL DEFAULT 0,
  lead_tier           lead_tier,
  signal_count        INT NOT NULL DEFAULT 0,
  has_stacked_signals BOOLEAN NOT NULL DEFAULT FALSE,

  -- Metadata
  first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  enriched_at         TIMESTAMPTZ,
  source_ids          UUID[],
  source_urls         TEXT[]
);

CREATE INDEX idx_properties_apn ON properties(apn);
CREATE INDEX idx_properties_county ON properties(county);
CREATE INDEX idx_properties_lead_tier ON properties(lead_tier);
CREATE INDEX idx_properties_lead_score ON properties(lead_score DESC);
CREATE INDEX idx_properties_address ON properties USING gin(address_full gin_trgm_ops);
CREATE INDEX idx_properties_equity_tier ON properties(equity_tier);

-- ============================================================
-- FORECLOSURE EVENTS
-- ============================================================

CREATE TABLE foreclosure_events (
  id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  property_id         UUID REFERENCES properties(id),
  source_id           UUID NOT NULL REFERENCES data_sources(id),
  source_doc_id       UUID REFERENCES source_documents(id),
  source_url          TEXT,
  county              TEXT NOT NULL,
  apn                 TEXT,
  document_number     TEXT,
  case_number         TEXT,
  trustee_name        TEXT,
  beneficiary_name    TEXT,
  borrower_name       TEXT,
  property_address    TEXT,
  notice_type         TEXT,   -- NOD, NTS, NOTICE_OF_SALE, LIS_PENDENS
  notice_date         DATE,
  recording_date      DATE,
  auction_date        DATE,
  auction_time        TEXT,
  auction_location    TEXT,
  loan_amount         NUMERIC(14,2),
  default_amount      NUMERIC(14,2),
  opening_bid         NUMERIC(14,2),
  trustee_sale_number TEXT,
  raw_data            JSONB,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(county, document_number)
);

CREATE INDEX idx_foreclosure_property ON foreclosure_events(property_id);
CREATE INDEX idx_foreclosure_apn ON foreclosure_events(apn);
CREATE INDEX idx_foreclosure_auction_date ON foreclosure_events(auction_date);
CREATE INDEX idx_foreclosure_county ON foreclosure_events(county);

-- ============================================================
-- PROBATE CASES
-- ============================================================

CREATE TABLE probate_cases (
  id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  property_id         UUID REFERENCES properties(id),
  source_id           UUID NOT NULL REFERENCES data_sources(id),
  source_doc_id       UUID REFERENCES source_documents(id),
  source_url          TEXT,
  county              TEXT NOT NULL,
  case_number         TEXT NOT NULL,
  case_type           TEXT,
  decedent_name       TEXT,
  personal_rep_name   TEXT,
  attorney_name       TEXT,
  filing_date         DATE,
  property_address    TEXT,
  apn                 TEXT,
  estate_value_est    NUMERIC(14,2),
  case_status         TEXT,
  raw_data            JSONB,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(county, case_number)
);

CREATE INDEX idx_probate_property ON probate_cases(property_id);
CREATE INDEX idx_probate_county ON probate_cases(county);
CREATE INDEX idx_probate_apn ON probate_cases(apn);

-- ============================================================
-- CODE VIOLATIONS
-- ============================================================

CREATE TABLE code_violations (
  id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  property_id         UUID REFERENCES properties(id),
  source_id           UUID NOT NULL REFERENCES data_sources(id),
  source_doc_id       UUID REFERENCES source_documents(id),
  source_url          TEXT,
  county              TEXT NOT NULL,
  case_number         TEXT,
  violation_type      TEXT,
  violation_description TEXT,
  property_address    TEXT,
  apn                 TEXT,
  complaint_date      DATE,
  inspection_date     DATE,
  compliance_date     DATE,
  is_open             BOOLEAN NOT NULL DEFAULT TRUE,
  penalty_amount      NUMERIC(10,2),
  raw_data            JSONB,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(county, case_number)
);

CREATE INDEX idx_violations_property ON code_violations(property_id);
CREATE INDEX idx_violations_apn ON code_violations(apn);
CREATE INDEX idx_violations_open ON code_violations(is_open);

-- ============================================================
-- TAX DISTRESS
-- ============================================================

CREATE TABLE tax_distress (
  id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  property_id         UUID REFERENCES properties(id),
  source_id           UUID NOT NULL REFERENCES data_sources(id),
  source_doc_id       UUID REFERENCES source_documents(id),
  source_url          TEXT,
  county              TEXT NOT NULL,
  apn                 TEXT NOT NULL,
  parcel_number       TEXT,
  owner_name          TEXT,
  property_address    TEXT,
  tax_year            INT,
  amount_delinquent   NUMERIC(12,2),
  amount_total_due    NUMERIC(12,2),
  lien_date           DATE,
  tax_deed_date       DATE,
  is_tax_deed         BOOLEAN NOT NULL DEFAULT FALSE,
  years_delinquent    INT,
  raw_data            JSONB,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(county, apn, tax_year)
);

CREATE INDEX idx_tax_property ON tax_distress(property_id);
CREATE INDEX idx_tax_apn ON tax_distress(apn);
CREATE INDEX idx_tax_county ON tax_distress(county);

-- ============================================================
-- LEAD SIGNALS
-- ============================================================

CREATE TABLE lead_signals (
  id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  property_id         UUID NOT NULL REFERENCES properties(id),
  signal_type         signal_type NOT NULL,
  signal_score        INT NOT NULL,
  signal_detail       TEXT,
  signal_date         DATE,
  source_table        TEXT,   -- foreclosure_events | probate_cases | etc
  source_record_id    UUID,
  is_active           BOOLEAN NOT NULL DEFAULT TRUE,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_signals_property ON lead_signals(property_id);
CREATE INDEX idx_signals_type ON lead_signals(signal_type);
CREATE INDEX idx_signals_active ON lead_signals(is_active);

-- ============================================================
-- GHL WEBHOOK LOG
-- ============================================================

CREATE TABLE ghl_webhook_log (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  property_id     UUID NOT NULL REFERENCES properties(id),
  sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  http_status     INT,
  response_body   TEXT,
  payload         JSONB,
  success         BOOLEAN NOT NULL DEFAULT FALSE,
  error_message   TEXT
);

CREATE INDEX idx_ghl_property ON ghl_webhook_log(property_id);
CREATE INDEX idx_ghl_sent ON ghl_webhook_log(sent_at DESC);

-- ============================================================
-- VIEWS
-- ============================================================

CREATE OR REPLACE VIEW v_lead_dashboard AS
SELECT
  p.id,
  p.apn,
  p.county,
  p.state,
  p.address_full,
  p.address_city,
  p.address_zip,
  p.owner_name,
  p.owner_mailing_state,
  p.is_absentee_owner,
  p.is_out_of_state,
  p.assessed_value,
  p.market_value_est,
  p.equity_est,
  p.equity_tier,
  p.lead_score,
  p.lead_tier,
  p.signal_count,
  p.has_stacked_signals,
  p.last_updated_at,

  -- Foreclosure
  fe.notice_type          AS fc_notice_type,
  fe.auction_date         AS fc_auction_date,
  fe.auction_location     AS fc_auction_location,
  fe.default_amount       AS fc_default_amount,
  fe.opening_bid          AS fc_opening_bid,
  fe.source_url           AS fc_source_url,

  -- Probate
  pc.case_number          AS prob_case_number,
  pc.case_status          AS prob_case_status,
  pc.decedent_name        AS prob_decedent_name,
  pc.filing_date          AS prob_filing_date,
  pc.source_url           AS prob_source_url,

  -- Code violations
  cv.case_number          AS cv_case_number,
  cv.violation_type       AS cv_violation_type,
  cv.is_open              AS cv_is_open,
  cv.complaint_date       AS cv_complaint_date,
  cv.source_url           AS cv_source_url,

  -- Tax
  td.amount_delinquent    AS tax_amount_delinquent,
  td.years_delinquent     AS tax_years_delinquent,
  td.is_tax_deed          AS tax_is_deed,
  td.source_url           AS tax_source_url,

  -- Signal array
  ARRAY(
    SELECT signal_type::TEXT FROM lead_signals
    WHERE property_id = p.id AND is_active = TRUE
  ) AS signals

FROM properties p
LEFT JOIN foreclosure_events fe ON fe.property_id = p.id
LEFT JOIN probate_cases pc ON pc.property_id = p.id
LEFT JOIN code_violations cv ON cv.property_id = p.id AND cv.is_open = TRUE
LEFT JOIN tax_distress td ON td.property_id = p.id
ORDER BY p.lead_score DESC;

-- ============================================================
-- UPDATED_AT TRIGGER
-- ============================================================

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_properties_updated BEFORE UPDATE ON properties
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_foreclosure_updated BEFORE UPDATE ON foreclosure_events
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_probate_updated BEFORE UPDATE ON probate_cases
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_violations_updated BEFORE UPDATE ON code_violations
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_tax_updated BEFORE UPDATE ON tax_distress
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_signals_updated BEFORE UPDATE ON lead_signals
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
