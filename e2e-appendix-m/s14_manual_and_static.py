"""Results that come from the manual browser checks (README 'Manual checklist') or from reading code, recorded so the
results file covers every Appendix M step. Edit the outcomes here if your manual run differs."""
from e2e_lib import psql, record

consent = psql("select version || ' ' || consents::text || ' ' || verified_by from user_consents order by created_at limit 1")
record("1.1 sign-up: three consent boxes + OTP, account created", True,
       "browser: three separate boxes, code button disabled until all three ticked, email OTP; wrong code 400, right code 200")
record("1.1 consent record stores who/when/what/purpose", "PARTIAL",
       f"user_consents row: user_id, created_at, {consent}, IP, user agent; purpose only implied by the flag names")
record("1.1 sign-up disclosure: FundLok does not collect debts", False,
       "absent from the T&S disclosures (Art. 3); only in the Investor Service Agreement")
record("2.2 six documents in the wizard", "PARTIAL",
       "code requires five: legal_charter, business_registration, e_invoice_data (zip), tax_filings (zip/xml), cic_report")
record("2.4 offer total consistent with the loans", False,
       "SME dashboard shows the offer total at the business rate (e.g. 102,534,167) next to the repayment plan at the "
       "lender rate capped at 2.5% x 12 / M (102,500,000)")
record("5.8 Day 14 on a Saturday", "PARTIAL",
       "repayment starts on the first working day after confirmation (correct); Day-13 / Day-14 anchors do not exist")
record("6.6 missed cut-off with proof of a bank outage -> no lock", "N/A", "there is no lock to waive (6.1)")
record("M9 Extension (9.1-9.8)", "NOT BUILT", "no revenue filing or re-amortisation; contract Art. 10 and the SME dashboard already promise it")
record("M10 Late, default, recovery (10.1-10.6)", "NOT BUILT", "arrears states defined but never triggered; presume-paid contradicts Late")
record("M11 Fraud (11.1-11.4)", "NOT BUILT", "SUSPENDED blocks all access, repayments included (handbook: except repayments)")
