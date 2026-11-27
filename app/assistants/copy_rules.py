"""FundLok's rules for financial claims (Handbook v3 §4-§5, the fundlok-domain
skill), as prompt text for Claude and as a check on what comes back."""
from __future__ import annotations

import re
import unicodedata

LANGUAGE_NAMES: dict[str, str] = {"vi": "Vietnamese", "en": "English"}

# Text containing any of these is refused, whatever the source says. Kept to
# phrases that are a claim on their own; "đảm bảo" alone is not listed because
# "tài sản đảm bảo" (collateral) is ordinary lending vocabulary.
FORBIDDEN_PHRASES: tuple[str, ...] = (
    "guaranteed",
    "risk-free",
    "risk free",
    "no risk",
    "zero risk",
    "protected capital",
    "capital protection",
    "principal protection",
    "credit scoring",
    "credit rating",
    "đảm bảo lợi nhuận",
    "lợi nhuận đảm bảo",
    "cam kết lợi nhuận",
    "lợi nhuận cam kết",
    "không rủi ro",
    "không có rủi ro",
    "phi rủi ro",
    "bảo toàn vốn",
    "chấm điểm",
    "xếp hạng tín dụng",
)

RULES = """\
FundLok's rules for financial claims apply to everything you write:
- Never promise or imply a return. No "guaranteed", "risk-free", "no risk", \
"safe returns", "fixed profit"; in Vietnamese no "lợi nhuận đảm bảo", "cam \
kết lợi nhuận", "không rủi ro". Returns are targets or ranges, never certain, \
and the investor bears the loss if a business underperforms.
- Never imply that capital is protected, that there is a reserve or \
insurance-like safety net, or that repayment is certain ("bảo toàn vốn").
- FundLok is not a bank, not a lender and not a credit rating agency. Never \
describe FundLok as scoring or rating credit: no "credit scoring", "credit \
rating", "chấm điểm", "xếp hạng tín dụng". Its 0-100 business score is a \
reference for the investor's own decision ("điểm doanh nghiệp và lãi suất \
tham khảo").
- FundLok never holds customer money: investors transfer straight to the \
business's own bank account, and the business repays each investor directly. \
Never mention deposits, withdrawals, wallets, balances, escrow or custodial \
accounts at FundLok.
- FundLok's fees are invoiced separately and never deducted from loan money.
- Never say approval is certain or instant, that no documents are needed, or \
that everyone qualifies.
- Early repayment means no prepayment penalty; never call it a discount or \
say that paying early costs less. If the term stretches, the total repayable \
goes up.
- Do not mention FundLok's licences or the regulations it operates under.
- Be specific, modest and factual. No hype such as "get rich", "instant \
money", "best" or "number one".\
"""


def fold(value: str) -> str:
    """NFC, single spaces, trimmed, case-folded: how text is compared."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip().casefold()


def breaks_rules(value: str) -> bool:
    folded = fold(value)
    return any(fold(phrase) in folded for phrase in FORBIDDEN_PHRASES)
