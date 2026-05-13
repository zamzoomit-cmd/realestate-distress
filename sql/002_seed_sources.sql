-- ============================================================
-- Source Registry Seed Data - Arizona Counties
-- ============================================================

INSERT INTO data_sources (source_key, county, source_name, source_type, base_url, scraper_class, scrape_method, rate_limit_rps, notes) VALUES

-- ============================================================
-- MARICOPA COUNTY
-- ============================================================

('maricopa_recorder_foreclosure', 'Maricopa', 'Maricopa County Recorder - Foreclosure/Trustee Docs',
 'foreclosure', 'https://recorder.maricopa.gov/recdocdata/', 'MaricopaRecorderScraper', 'playwright', 0.5,
 'Search for NOD, NTS, Trustee Sale notices via document type filter'),

('maricopa_sheriff_foreclosure', 'Maricopa', 'Maricopa County Sheriff - Civil Sales',
 'foreclosure', 'https://www.maricopa.gov/5574/Civil-Sale-Information', 'MaricopaSheriffScraper', 'requests', 1.0,
 'Sheriff civil foreclosure sale listings - public auction data'),

('maricopa_superior_court_probate', 'Maricopa', 'Maricopa Superior Court - Probate Cases',
 'probate', 'https://www.superiorcourt.maricopa.gov/docket/', 'MaricopaProbateScraper', 'playwright', 0.5,
 'Probate case search - estate cases with real property'),

('maricopa_code_violations', 'Maricopa', 'Maricopa County Code Compliance - Open Violations',
 'code_violation', 'https://www.maricopa.gov/DocumentCenter/View/', 'MaricopaCodeViolationScraper', 'requests', 1.0,
 'Code compliance open cases - publicly listed violations'),

('maricopa_treasurer_tax', 'Maricopa', 'Maricopa County Treasurer - Tax Delinquency',
 'tax', 'https://mctreasurer.maricopa.gov/TreasurersPortal/', 'MaricopaTaxScraper', 'playwright', 0.5,
 'Property tax delinquency search by parcel'),

('maricopa_assessor', 'Maricopa', 'Maricopa County Assessor - Property Data',
 'assessor', 'https://mcassessor.maricopa.gov/mcs.php', 'MaricopaAssessorScraper', 'requests', 1.0,
 'Property assessment, owner info, valuation - public data portal'),

('maricopa_assessor_csv', 'Maricopa', 'Maricopa County Assessor - Open Data CSV',
 'assessor', 'https://www.maricopa.gov/OpenData', 'MaricopaAssessorCSVScraper', 'csv', 2.0,
 'Annual bulk CSV export of assessed values and owner data'),

-- ============================================================
-- PIMA COUNTY
-- ============================================================

('pima_recorder_foreclosure', 'Pima', 'Pima County Recorder - Foreclosure Documents',
 'foreclosure', 'https://recorder.pima.gov/RecorderWeb/', 'PimaRecorderScraper', 'playwright', 0.5,
 'Document search for NOD, NTS, Trustee Sale filings'),

('pima_superior_court_probate', 'Pima', 'Pima County Superior Court - Probate',
 'probate', 'https://www.sc.pima.gov/', 'PimaProbateScraper', 'playwright', 0.5,
 'Probate court case search for estate proceedings'),

('pima_dev_services_violations', 'Pima', 'Pima County Development Services - Code Violations',
 'code_violation', 'https://webcms.pima.gov/cms/One.aspx?portalId=169&pageId=55367', 'PimaCodeViolationScraper', 'requests', 1.0,
 'Code enforcement and building violations - public case list'),

('pima_treasurer_tax', 'Pima', 'Pima County Treasurer - Delinquent Tax List',
 'tax', 'https://www.to.pima.gov/', 'PimaTaxScraper', 'playwright', 0.5,
 'Annual delinquent tax list and parcel search'),

('pima_assessor', 'Pima', 'Pima County Assessor - Property Records',
 'assessor', 'https://www.assessor.pima.gov/', 'PimaAssessorScraper', 'requests', 1.0,
 'Property assessment data and owner information - public portal'),

('pima_assessor_open_data', 'Pima', 'Pima County GIS Open Data - Parcels',
 'assessor', 'https://gisdata.pima.gov/datasets/', 'PimaGISDataScraper', 'json', 2.0,
 'GIS open data parcel layer with ownership and valuation fields');
