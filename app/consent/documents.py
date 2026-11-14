"""The Terms & Services version every SME and investor must accept, and the
three things they accept (Contract 1, "Terms & Services and data consent").

Changing CURRENT_VERSION asks everyone to accept again: status() looks for
acceptance of THIS version only. Change it whenever the text shown on the
frontend's /consent page changes (lib/legal/terms.ts holds the same string).
"""

# Contract set v0.3, drafted 5 Nov 2026, pending IBL review.
CURRENT_VERSION = "tos-v0.3"

# One tick box each on the sign-up screen (Contract 1, "Tick boxes on the
# sign-up screen"):
TERMS = "TERMS"  # Terms & Services and the Article 3 disclosures
DATA_PROCESSING = "DATA_PROCESSING"  # Art. 10.1-10.2
DATA_SHARING_TPV = "DATA_SHARING_TPV"  # Art. 10.3, sharing with TPV Datatrust for eKYC

DOCUMENTS = (TERMS, DATA_PROCESSING, DATA_SHARING_TPV)

VERIFIED_BY_EMAIL_OTP = "EMAIL_OTP"
