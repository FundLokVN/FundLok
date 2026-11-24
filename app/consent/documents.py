"""The Terms & Services version every SME and investor must accept, and the
three things they accept (Contract 1, "Terms & Services and data consent").

Changing CURRENT_VERSION asks everyone to accept again: status() looks for
acceptance of THIS version only. Change it whenever the text shown on the
frontend's /consent page changes (lib/legal/terms.ts holds the same string).
"""

# Contract 1 v1.0 FINAL, locked 17 Nov 2026 (the text the public /terms page
# shows). tos-v0.3 was the 5 Nov 2026 draft.
CURRENT_VERSION = "tos-v1.0"

# One tick box each on the sign-up screen (Contract 1, "Tick boxes on the
# sign-up screen"):
TERMS = "TERMS"  # Terms & Services and the Article 3 disclosures
DATA_PROCESSING = "DATA_PROCESSING"  # Art. 10.1-10.2
DATA_SHARING_TPV = "DATA_SHARING_TPV"  # Art. 10.3 + 10.7: TPV Datatrust eKYC/eKYB, overseas cloud storage

DOCUMENTS = (TERMS, DATA_PROCESSING, DATA_SHARING_TPV)

VERIFIED_BY_EMAIL_OTP = "EMAIL_OTP"
