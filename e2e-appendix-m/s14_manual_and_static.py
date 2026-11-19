"""Results that come from the manual browser checks (README 'Manual checklist') or from reading code, recorded so the
results file covers every Appendix M step. Edit the outcomes here if your manual run differs."""
from e2e_lib import psql, record

consent = psql("select version || ' ' || consents::text || ' ' || verified_by from user_consents order by created_at limit 1")
record("1.1 sign-up: three consent boxes + OTP, account created", True,
       "driven through the API in every onboarding (three flags + email OTP); the 14 Nov browser check is not repeated")
record("1.1 consent record stores who/when/what/purpose", "PARTIAL",
       f"user_consents row: user_id, created_at, {consent}, IP, user agent; purpose only implied by the flag names")
record("1.1 sign-up disclosure: FundLok does not collect debts", False,
       f"the consent version is still {consent.split()[0] if consent else '?'}: the FINAL v1.0 T&S (Art. 3.2 disclosure) is not "
       "loaded yet (SCRUM-638 / SCRUM-639)")
record("2.2 five documents in the wizard", True,
       "five uploads: legal_charter, business_registration, e_invoice_data (zip), tax_filings (zip/xml), cic_report "
       "(handbook v3.22 6.3)")
record("2.4 offer total consistent with the loans", "NOT RECHECKED",
       "14 Nov browser finding (offer total at the business rate vs plan at a capped lender rate); lenders now earn the "
       "full graded rate, so recheck on the SME dashboard")
record("5.8 Day 14 on a Saturday", "N/A",
       "not driven by this run: Day 13 / Day 14 are now fixed calendar instants (14:45 / 15:00 / 15:30 ICT) and "
       "the backend tests cover weekend dates")
record("M9 Extension (9.1-9.8)", "NOT RUN",
       "built on 18 Nov (automatic extension on the monthly revenue filing, PR #132); not yet covered by this run")
record("M10.1 Late and late interest", "PARTIAL",
       "Late status and late interest are built (PRs #130, #131); s12 checks Late; late-interest amounts not yet checked")
record("M10.2-10.6 default, guarantor call, loan sale, backstop", "NOT BUILT", "manual procedure at launch (accepted debt D-11)")
record("M11 Fraud (11.1-11.4)", "NOT BUILT", "SUSPENDED blocks all access, repayments included (handbook: except repayments)")
