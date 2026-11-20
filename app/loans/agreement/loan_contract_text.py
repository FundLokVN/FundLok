"""Contract 5, "Hợp đồng cho vay / Loan Contract", as the template the SME and its Personal Guarantor sign after
FundLok's assessment, with the Service Agreement and the Standing Offer (app/loans/signing.py). Each Investor's own
contract is generated from it when they accept the Standing Offer, and the Investor signs that one at commitment
(a later step).

Copied verbatim from the Contract set (document 5) as it stood on 10 Nov 2026, a DRAFT pending legal review. If the
Vietnamese and English differ, the Vietnamese prevails. VERSION is printed on every page, so a signed copy says
which text it was; bump it whenever a word here changes.
"""

# v0.4: laid out as a Vietnamese contract (national header, date, number); filled in with the Standing Offer's
# number and date and, where each Investor's copy fills a value in, the rule it follows (`PER_INVESTOR`).
# v0.5: the revenue share s is filled in, as on the Standing Offer, and covered by the check code.
VERSION = "loan-contract-v0.5"

TITLE = ("5. Hợp đồng cho vay", "Loan Contract")

PREVAILS = ("Bản tiếng Việt được ưu tiên áp dụng.", "The Vietnamese text prevails.")

SIGNING = (
    "Hợp đồng này được tạo cho từng Nhà đầu tư khi Nhà đầu tư chấp nhận Đề nghị giao kết hợp đồng cho vay của "
    "Doanh nghiệp. Nhà đầu tư ký bằng chữ ký số từ xa khi cam kết; Doanh nghiệp và Người bảo lãnh đã ký Đề nghị giao "
    "kết và không ký lại.",
    "This contract is generated for each Investor when the Investor accepts the SME's Standing Offer. The Investor "
    "signs with a remote digital signature at commitment; the SME and the Personal Guarantor have signed the "
    "Standing Offer and do not sign again.",
)

# CONTRACT_NO and LENDER_* are each Investor's own copy's; the template the SME signs prints NUMBER and
# PER_INVESTOR["lender_*"] in their place.
CONTRACT_NO = "Số hợp đồng / Contract no.: {number}"
# The contract number: the application reference, the document (Hợp đồng cho vay) and FundLok, then each Investor's
# own sequence number, issued at commitment.
NUMBER = "{ref}/HĐCV-FL, số thứ tự cấp cho từng Nhà đầu tư khi cam kết / numbered for each Investor at commitment"

ARTICLE_1 = "Điều 1. Các bên / Parties"
LENDER_VI = (
    "**Bên cho vay (Nhà đầu tư):** {name}, số định danh / mã số doanh nghiệp {id_number}, tài khoản ngân hàng đứng "
    "tên Bên cho vay {account} tại {bank}."
)
LENDER_EN = (
    "Lender (Investor): {name}, ID / enterprise code {id_number}, bank account in the Lender's own name {account} at "
    "{bank}."
)
BORROWER_VI = (
    "**Bên vay (Doanh nghiệp):** {name}, mã số doanh nghiệp {code}, đại diện theo pháp luật {representative}, tài "
    "khoản ngân hàng đứng tên Bên vay {account} tại {bank}."
)
BORROWER_EN = (
    "Borrower (SME): {name}, enterprise code {code}, legal representative {representative}, bank account in the "
    "Borrower's own name {account} at {bank}."
)
GUARANTOR_VI = (
    "**Người bảo lãnh cá nhân:** {name}, số định danh cá nhân {id_number}, địa chỉ {address}, vai trò tại Doanh "
    "nghiệp: chủ sở hữu / người đại diện theo pháp luật."
)
GUARANTOR_EN = (
    "Personal Guarantor: {name}, personal ID {id_number}, address {address}, role at the SME: owner / legal "
    "representative."
)

ARTICLE_2 = (
    "Điều 2. Giao kết và bản chất / Formation and nature",
    "Hợp đồng này được giao kết khi chấp nhận của Nhà đầu tư đối với Đề nghị giao kết số {offer_no} ngày {offer_date} "
    "đến Doanh nghiệp qua nền tảng trực tuyến nơi Đề nghị được đăng (\"Nền tảng\") (Điều 400 Bộ luật Dân sự 2015). Đây "
    "là hợp đồng vay tài sản có lãi, có kỳ hạn, riêng giữa Nhà đầu tư này và Doanh nghiệp (Điều 463). Tiền vay và "
    "tiền trả nợ chỉ được chuyển trực tiếp giữa tài khoản ngân hàng của hai bên tại Điều 1.",
    "This contract is concluded when the Investor's acceptance of Standing Offer no. {offer_no} dated {offer_date} "
    "reaches the SME through the online platform where the Offer is published (the \"Platform\") (Civil Code 2015, "
    "Art. 400). It is a separate term loan with interest between this Investor and the SME (Art. 463). Loan money "
    "and repayments move only by direct transfer between the two parties' bank accounts in Article 1.",
)

ARTICLE_3 = (
    "Điều 3. Hiệu lực / Effect",
    [
        (
            "Các quyền và nghĩa vụ về khoản vay theo Hợp đồng này (gốc, lãi, trả nợ và bảo lãnh) chỉ có hiệu lực khi "
            "cả hai điều kiện sau được đáp ứng (Điều 120): (a) Doanh nghiệp chấp nhận, hoặc được coi là chấp nhận, "
            "theo Điều 4 của Đề nghị giao kết; và (b) khoản giải ngân của Nhà đầu tư được xác nhận theo Điều 6. Riêng "
            "nghĩa vụ chuyển tiền tại Điều 5.1 có hiệu lực ngay khi điều kiện (a) được đáp ứng. Nếu một trong hai điều "
            "kiện không được đáp ứng, các quyền và nghĩa vụ về khoản vay không phát sinh hiệu lực.",
            "The loan rights and obligations under this contract (principal, interest, repayment and the guarantee) "
            "take effect only when both conditions are met (Art. 120): (a) the SME accepts, or is deemed to accept, "
            "under Article 4 of the Standing Offer; and (b) the Investor's disbursement is confirmed under Article 6. "
            "The duty to transfer in Article 5.1 alone takes effect as soon as condition (a) is met. If either "
            "condition is not met, the loan rights and obligations do not take effect.",
        )
    ],
)

ARTICLE_4 = "Điều 4. Điều kiện khoản vay / Loan terms"
# The rows of Article 4, label -> how the value is written. {..} are filled; the rest is the template's own text.
TERMS_LABELS = {
    "principal": "Số tiền cho vay / Principal (P)",
    "rate": "Lãi suất / Rate (r)",
    "term": "Thời hạn / Term (M)",
    "interest": "Tiền lãi / Interest (I = P × r × M / 12)",
    "total": "Tổng phải trả / Total repayable (T = P + I)",
    "instalment_days": "Số ngày trả / Instalment days (N0 = 21 × M)",
    "daily": "Khoản trả mỗi ngày / Daily instalment (A)",
    "disbursement_date": "Ngày giải ngân / Disbursement Date",
    "cut_off": "Giờ chốt chuyển tiền / Transfer cut-off",
    "first_instalment": "Ngày trả đầu tiên / First instalment",
    "backstop": "Ngày chặn / Backstop day (N_bs = 4 × N0 / 3, làm tròn lên / rounded up)",
    "revenue_share": "Tỷ lệ doanh thu cho gia hạn / Revenue share for extensions (s)",
    "pit": "Thuế TNCN khấu trừ / PIT withheld",
}
VND_SUFFIX = " VND"
RATE_VI = "%/năm, lãi đơn trên gốc ban đầu"
RATE_EN = "a year, flat on original principal"
TERM = "{months} tháng / months"
INSTALMENT_DAYS = "{days} ngày làm việc / working days"
DAILY = "{daily} VND (kỳ cuối {last} VND / last {last} VND)"
CUT_OFF = "15:00 Ngày giải ngân / 3 pm on the Disbursement Date"
FIRST_INSTALMENT = "Ngày làm việc đầu tiên sau Ngày giải ngân / first working day after the Disbursement Date"
REVENUE_SHARE = "{share}% – theo Thông tin Khoản gọi vốn / as set in the Listing details"
PIT = "5% tiền lãi (Nhà đầu tư cá nhân) / 5% of interest (individual Investors)"

# (heading, [(vi, en), ...]) in order, Articles 5 to 15.
ARTICLES: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Điều 5. Giải ngân / Disbursement",
        [
            (
                "5.1. Khi điều kiện tại Điều 3(a) được đáp ứng, Nhà đầu tư chuyển đúng số tiền cho vay P bằng chuyển "
                "khoản trực tiếp từ tài khoản ngân hàng đứng tên Nhà đầu tư vào tài khoản ngân hàng đứng tên Doanh "
                "nghiệp tại Điều 1, trước giờ chốt của Ngày giải ngân, ghi nội dung chuyển khoản theo mã tham chiếu "
                "của Hợp đồng này. Với khoản bổ sung theo Điều 5 của Đề nghị giao kết, Nhà đầu tư chuyển tiền ngay khi "
                "cam kết và ngày chuyển tiền là Ngày giải ngân.",
                "5.1. Once the condition in Article 3(a) is met, the Investor transfers exactly the principal P by "
                "direct bank transfer from the bank account in the Investor's own name to the bank account in the "
                "SME's own name in Article 1, before the cut-off on the Disbursement Date, quoting this contract's "
                "reference code. For a top-up under Article 5 of the Standing Offer, the Investor transfers as soon "
                "as it commits and the day of transfer is the Disbursement Date.",
            ),
            (
                "5.2. Số tiền chuyển vượt P không phải là tiền vay; Doanh nghiệp hoàn trả lại cho người chuyển.",
                "5.2. Money sent above P is not a loan; the SME returns it to the sender.",
            ),
            (
                "5.3. Nếu Nhà đầu tư không chuyển tiền trước giờ chốt, các quyền và nghĩa vụ về khoản vay theo Hợp "
                "đồng này không phát sinh hiệu lực, trừ khi có sự kiện bất khả kháng hoặc sự cố của hệ thống ngân "
                "hàng và hai bên thống nhất ngày chuyển tiền mới (Điều 156).",
                "5.3. If the Investor does not transfer before the cut-off, the loan rights and obligations under "
                "this contract do not take effect, unless there is force majeure or a banking-system outage and both "
                "parties agree a new transfer date (Art. 156).",
            ),
        ],
    ),
    (
        "Điều 6. Xác nhận giao dịch / Confirmation of transfers",
        [
            (
                "6.1. Mỗi bên ghi nhận chứng từ chuyển tiền (ảnh chụp lệnh chuyển, xác nhận đã nhận) trên Nền tảng "
                "theo thỏa thuận riêng của bên đó với đơn vị vận hành Nền tảng. Khoản giải ngân được xác nhận khi "
                "được ghi nhận là đã nhận theo các thỏa thuận đó.",
                "6.1. Each party records transfer evidence (transfer screenshots, confirmations of receipt) on the "
                "Platform under its own agreement with the Platform operator. A disbursement is confirmed when it is "
                "recorded as received under those agreements.",
            ),
            (
                "6.2. Xác nhận đã nhận trên Nền tảng, kể cả xác nhận mặc định do hết thời hạn phản hồi, là chứng cứ về "
                "việc đã nhận tiền. Bên nhận vẫn có quyền chứng minh điều ngược lại bằng sao kê ngân hàng trong 30 "
                "ngày kể từ ngày xác nhận; khi đó bên chuyển có thể yêu cầu ngân hàng tra soát và kết quả tra soát là "
                "căn cứ.",
                "6.2. A confirmation of receipt on the Platform, including one deemed when the response time runs "
                "out, is evidence that the money was received. The receiving party may still prove otherwise with a "
                "bank statement within 30 days of the confirmation; the paying party may then ask its bank to trace "
                "the transfer, and the trace result decides.",
            ),
        ],
    ),
    (
        "Điều 7. Lãi và trả nợ / Interest and repayment",
        [
            (
                "7.1. Tiền lãi I = P × r × M / 12, tính trên gốc ban đầu cho cả thời hạn. Tổng phải trả T = P + I. "
                "Lãi suất không vượt quá 20%/năm (Điều 468).",
                "7.1. Interest I = P × r × M / 12, flat on the original principal for the whole term. Total repayable "
                "T = P + I. The rate never exceeds 20% a year (Art. 468).",
            ),
            (
                "7.2. Doanh nghiệp trả cho Nhà đầu tư A đồng mỗi ngày làm việc (trừ thứ Bảy, Chủ nhật và ngày lễ, Tết "
                "theo lịch Việt Nam) bằng chuyển khoản trực tiếp từ tài khoản ngân hàng đứng tên Doanh nghiệp vào tài "
                "khoản ngân hàng đứng tên Nhà đầu tư tại Điều 1, bắt đầu từ ngày làm việc đầu tiên sau Ngày giải ngân, "
                "trong N0 ngày làm việc. A = phần nguyên của T / N0; phần dư được cộng vào kỳ cuối. Ngày nghỉ không "
                "phát sinh kỳ trả và không phát sinh thêm lãi. Doanh nghiệp có thể chuyển từng khoản, đặt lệnh chuyển "
                "định kỳ hoặc chuyển theo lô, miễn là Nhà đầu tư nhận đủ và đúng hạn. Mỗi kỳ trả được phân bổ vào lãi "
                "và gốc theo tỷ lệ I : P (phần lãi của mỗi kỳ = A × I / T, phần còn lại là gốc); kỳ cuối nhận phần "
                "gốc và lãi còn lại. I và T được cập nhật khi gia hạn theo Điều 10. Cách phân bổ này dùng để tính thuế "
                "tại Điều 8 và lãi chậm trả tại Điều 11.1.",
                "7.2. The SME pays the Investor A dong every working day (excluding Saturdays, Sundays and Vietnamese "
                "public holidays) by direct bank transfer from the bank account in the SME's own name to the bank "
                "account in the Investor's own name in Article 1, starting on the first working day after the "
                "Disbursement Date, over N0 working days. A = T / N0 rounded down; the remainder is added to the last "
                "instalment. Non-working days carry no instalment and no extra interest. The SME may pay each "
                "transfer separately, by standing order or by batch transfer, as long as the Investor receives the "
                "full amount on time. Each instalment is split between interest and principal in the ratio I : P "
                "(interest in each instalment = A × I / T, the rest is principal); the last instalment takes the "
                "remaining principal and interest. I and T are updated on any extension under Article 10. This split "
                "is used for tax under Article 8 and late interest under Article 11.1.",
            ),
            (
                "7.3. Với khoản bổ sung theo Điều 5 của Đề nghị giao kết: cùng lãi suất, bắt đầu trả từ ngày làm việc "
                "đầu tiên sau khi được xác nhận và kết thúc cùng ngày với các khoản ban đầu. Với N = số ngày làm việc "
                "còn lại đến ngày kết thúc đó: I = P × r × N / 252, T = P + I, A = phần nguyên của T / N.",
                "7.3. For a top-up under Article 5 of the Standing Offer: the same rate, repayment from the first "
                "working day after it is confirmed, and the same end date as the original tranches. With N = the "
                "working days left to that end date: I = P × r × N / 252, T = P + I, A = T / N rounded down.",
            ),
        ],
    ),
    (
        "Điều 8. Thuế / Tax",
        [
            (
                "Đối với Nhà đầu tư cá nhân, Doanh nghiệp khấu trừ 5% thuế thu nhập cá nhân trên tiền lãi của mỗi kỳ, "
                "nộp vào ngân sách nhà nước và cung cấp chứng từ khấu trừ theo quy định.",
                "For individual Investors, the SME withholds 5% personal income tax on the interest in each "
                "instalment, pays it to the State budget and issues withholding documents as required.",
            )
        ],
    ),
    (
        "Điều 9. Trả nợ trước hạn / Early repayment",
        [
            (
                "Doanh nghiệp có thể trả trước hạn bất kỳ lúc nào bằng cách trả toàn bộ số còn lại (T trừ số đã trả), "
                "không bị phạt và không được giảm lãi.",
                "The SME may repay early at any time by paying the full remaining balance (T less amounts paid), with "
                "no penalty and no interest discount.",
            )
        ],
    ),
    (
        "Điều 10. Gia hạn / Extension",
        [
            (
                "10.1. Khi doanh thu tháng của Doanh nghiệp, được chứng minh bằng giấy tờ Doanh nghiệp tự cung cấp, "
                "thấp hơn mức cần để trả đủ trong tháng, Doanh nghiệp có thể đề nghị gia hạn. Phần thiếu hụt U = số "
                "phải trả trong tháng − s × doanh thu tháng được chứng minh.",
                "10.1. When the SME's monthly revenue, evidenced by papers the SME provides itself, falls short of "
                "what it needs to pay that month, the SME may request an extension. The shortfall U = the month's "
                "scheduled amount − s × the month's evidenced revenue.",
            ),
            (
                "10.2. Số ngày gia hạn Δ = (U / số phải trả trong tháng × 21), làm tròn lên bội số gần nhất của 5. "
                "Lãi bổ sung = P × r × Δ / 252. Số còn lại được chia đều cho các ngày còn lại; khoản trả mỗi ngày "
                "không bao giờ tăng.",
                "10.2. Extension days Δ = (U / the month's scheduled amount × 21), rounded up to the next multiple of "
                "5. Extra interest = P × r × Δ / 252. The balance is spread over the remaining days; the daily "
                "instalment never increases.",
            ),
            (
                "10.3. Tổng số ngày gia hạn không vượt quá ngày chặn N_bs và lãi suất quy đổi của khoản vay không vượt "
                "quá 20%/năm. Nhà đầu tư đồng ý trước với công thức này.",
                "10.3. Total extension never goes beyond the backstop day N_bs, and the loan's implied annual rate "
                "never exceeds 20%. The Investor agrees to this formula in advance.",
            ),
        ],
    ),
    (
        "Điều 11. Chậm trả và ngày chặn / Late payment and backstop",
        [
            (
                "11.1. Gốc quá hạn chịu lãi chậm trả bằng 150% lãi suất r; tiền lãi trong hạn đã đến hạn mà chưa trả "
                "chịu lãi 10%/năm, theo Điều 466.5 Bộ luật Dân sự.",
                "11.1. Overdue principal bears late interest at 150% of the rate r; in-term interest that fell due "
                "and was not paid bears interest at 10% a year, under Civil Code Art. 466.5.",
            ),
            (
                "11.2. Đến ngày làm việc thứ N_bs mà khoản vay chưa trả hết, toàn bộ số còn lại đến hạn ngay. Doanh "
                "nghiệp có 07 ngày để thanh toán; việc trả hằng ngày vẫn tiếp tục trong thời gian này. Sau đó, Nhà đầu "
                "tư tự yêu cầu thu hồi khoản nợ của mình, kể cả yêu cầu Người bảo lãnh theo Điều 12.",
                "11.2. If the loan is not fully repaid by working day N_bs, the whole remaining balance falls due at "
                "once. The SME has 7 days to pay; daily payments continue during this time. After that, the Investor "
                "pursues its own claim, including against the Personal Guarantor under Article 12.",
            ),
        ],
    ),
    (
        "Điều 12. Bảo lãnh cá nhân / Personal guarantee",
        [
            (
                "12.1. Người bảo lãnh tại Điều 1 cam kết với Nhà đầu tư sẽ thực hiện thay Doanh nghiệp nghĩa vụ trả nợ "
                "theo Hợp đồng này nếu Doanh nghiệp không thực hiện (Điều 335 Bộ luật Dân sự 2015). Phạm vi: toàn bộ "
                "số còn phải trả (gốc, lãi, lãi chậm trả) và chi phí thu hồi hợp lý.",
                "12.1. The Personal Guarantor in Article 1 undertakes to the Investor to perform the SME's repayment "
                "obligations under this contract if the SME does not (Civil Code 2015, Art. 335). Scope: the full "
                "amount still owed (principal, interest, late interest) and reasonable recovery costs.",
            ),
            (
                "12.2. Nhà đầu tư có quyền yêu cầu Người bảo lãnh thực hiện khi Doanh nghiệp mất khả năng thanh toán, "
                "hoặc khi Doanh nghiệp từ chối trả nợ, tức là tuyên bố không trả hoặc không trả số đến hạn trong 07 "
                "ngày theo Điều 11.2. Trước khi yêu cầu, Nhà đầu tư thông báo bằng văn bản cho Người bảo lãnh, kèm hồ "
                "sơ khoản vay.",
                "12.2. The Investor may call on the Personal Guarantor when the SME is insolvent, or when the SME "
                "refuses to repay, meaning it states that it will not pay or does not pay the balance due within the "
                "7 days under Article 11.2. Before calling, the Investor gives the Guarantor written notice with the "
                "loan records.",
            ),
            (
                "12.3. Bảo lãnh có hiệu lực cùng các quyền và nghĩa vụ về khoản vay, không hủy ngang, và chấm dứt khi "
                "nghĩa vụ được thực hiện xong hoặc khi các quyền và nghĩa vụ đó không phát sinh hiệu lực.",
                "12.3. The guarantee takes effect with the loan rights and obligations, is irrevocable, and ends when "
                "the obligations are fully performed or if those rights and obligations do not take effect.",
            ),
        ],
    ),
    (
        "Điều 13. Cam kết của Doanh nghiệp / SME undertakings",
        [
            (
                "Doanh nghiệp cam kết: thông tin cung cấp là trung thực; người ký có thẩm quyền và việc vay đã được "
                "chấp thuận theo điều lệ; dùng tiền vay cho vốn lưu động của doanh nghiệp; thông báo ngay mọi sự kiện "
                "ảnh hưởng đến khả năng trả nợ.",
                "The SME undertakes that: its information is true; the signatory has authority and the borrowing is "
                "approved as its charter requires; it uses the loan for working capital; it will notify at once any "
                "event affecting its ability to repay.",
            )
        ],
    ),
    (
        "Điều 14. Bên thứ ba / Third parties",
        [
            (
                "Đơn vị vận hành Nền tảng không phải là một bên của Hợp đồng này, không bảo lãnh và không nhận, giữ "
                "hay chuyển tiền vay hoặc tiền trả nợ.",
                "The Platform operator is not a party to this contract, gives no guarantee, and never receives, holds "
                "or transfers loan money or repayments.",
            )
        ],
    ),
    (
        "Điều 15. Luật áp dụng, tranh chấp và điều khoản chung / Law, disputes and general",
        [
            (
                "Pháp luật Việt Nam. Thương lượng trong 30 ngày; sau đó Tòa án có thẩm quyền của Việt Nam, hoặc VIAC "
                "nếu các bên thỏa thuận bằng văn bản. Hợp đồng được giao kết bằng phương tiện điện tử theo Luật Giao "
                "dịch điện tử 2023 và có giá trị như văn bản giấy. Thứ tự ưu tiên: Hợp đồng này, sau đó Đề nghị giao "
                "kết.",
                "Vietnamese law. Negotiation for 30 days; then the competent Vietnamese court, or VIAC if the parties "
                "agree in writing. This contract is made electronically under the Law on Electronic Transactions 2023 "
                "and has the same force as a paper contract. Order of precedence: this contract, then the Standing "
                "Offer.",
            )
        ],
    ),
]

ANNEX = (
    "Phụ lục – Lịch trả nợ / Annex – Repayment schedule",
    "Do hệ thống tạo sau khi khoản giải ngân được xác nhận: ngày, gốc, lãi, thuế khấu trừ, số thực nhận của từng kỳ.",
    "Generated by the system once the disbursement is confirmed: date, principal, interest, tax withheld and net "
    "amount for each instalment.",
)

SIGNATURES_TITLE = "Ký kết / Signatures"
SIGNATURE_LINES = [
    (
        "Nhà đầu tư / Investor: chữ ký số từ xa, thời điểm {investor_time_vi} / remote digital signature, time "
        "{investor_time_en}."
    ),
    (
        "Doanh nghiệp / SME: đã ký Đề nghị giao kết số {offer_no} bằng chữ ký số doanh nghiệp ngày {offer_date} / "
        "signed Standing Offer no. {offer_no} with its enterprise digital signature on {offer_date}."
    ),
    (
        "Người bảo lãnh / Personal Guarantor: đã ký Đề nghị giao kết số {offer_no} bằng chữ ký số cá nhân ngày "
        "{offer_date} / signed Standing Offer no. {offer_no} with a personal digital signature on {offer_date}."
    ),
]

# The boxes this template is signed in now, by the SME and the Guarantor (the Investor signs their own contract,
# generated from this template, at commitment).
# What this copy says where each Investor's own copy fills a value in at commitment: the rule the value follows, and
# for the amounts a worked example at the minimum ticket, so the SME and the Guarantor sign knowing what every copy
# will say. ({..} are filled from the approved terms.)
PER_INVESTOR = {
    "lender_vi": (
        "**Bên cho vay (Nhà đầu tư):** Nhà đầu tư chấp nhận Đề nghị giao kết số {offer_no}. Họ tên hoặc tên doanh "
        "nghiệp, số định danh hoặc mã số doanh nghiệp, và tài khoản ngân hàng đứng tên Bên cho vay được ghi vào bản "
        "Hợp đồng của từng Nhà đầu tư khi cam kết."
    ),
    "lender_en": (
        "Lender (Investor): the Investor who accepts Standing Offer no. {offer_no}. The name, the ID or enterprise "
        "code, and the bank account in the Lender's own name are written into each Investor's copy at commitment."
    ),
    "principal": (
        "Số tiền Nhà đầu tư cam kết: từ {min} VND, tổng các hợp đồng không vượt {target} VND / the amount the "
        "Investor commits: from {min} VND, all contracts together at most {target} VND"
    ),
    "interest": "P × {rate}% × {months} / 12; ví dụ / e.g. P = {min}: {interest} VND",
    "total": "P + I; ví dụ / e.g. P = {min}: {total} VND",
    "daily": (
        "Phần nguyên của T / {days} / integer part of T / {days}; ví dụ / e.g. P = {min}: {daily} VND (kỳ cuối / "
        "last {last} VND)"
    ),
    "investor_time_vi": "cam kết, ghi trên bản Hợp đồng của từng Nhà đầu tư",
    "investor_time_en": "of commitment, stated on each Investor's copy",
}

SIGNATURE_BOXES = (
    ("DOANH NGHIỆP / SME", "Ký số doanh nghiệp / Enterprise digital signature"),
    ("NGƯỜI BẢO LÃNH CÁ NHÂN / PERSONAL GUARANTOR", "Ký số cá nhân / Personal digital signature"),
)


# --------------------------------------------------------------------------- #
# Each Investor's own copy (Money flow FINAL v1.0, Day 13 and Day 14)
# --------------------------------------------------------------------------- #
# Generated for one Investor at 00:00 on Day 13 from the final pledge list. The SME signs it with its enterprise
# digital signature and the Personal Guarantor by a Zalo one-time code on Day 13; the Investor signs it by a Zalo
# one-time code on the Disbursement Date (Day 14). To be checked against the Contract 5 FINAL v1.0 wording.
INVESTOR_COPY_SIGNING = (
    "Bản Hợp đồng này được lập cho Nhà đầu tư nêu tại Điều 1 từ danh sách cam kết cuối cùng lúc 00:00 Ngày 13 của "
    "Khoản gọi vốn. Doanh nghiệp ký bằng chữ ký số doanh nghiệp và Người bảo lãnh ký bằng mã xác thực một lần gửi qua "
    "Zalo vào Ngày 13; Nhà đầu tư ký bằng mã xác thực một lần gửi qua Zalo vào Ngày giải ngân.",
    "This copy is made for the Investor named in Article 1 from the final pledge list at 00:00 on Day 13 of the "
    "Listing. The SME signs it with its enterprise digital signature and the Personal Guarantor by a one-time code "
    "sent by Zalo on Day 13; the Investor signs it by a one-time code sent by Zalo on the Disbursement Date.",
)
INVESTOR_COPY_SIGNATURE_LINES = [
    (
        "Nhà đầu tư / Investor: ký bằng mã xác thực một lần gửi qua Zalo vào Ngày giải ngân {disbursement_date} / "
        "signs by a one-time code sent by Zalo on the Disbursement Date {disbursement_date}."
    ),
    (
        "Doanh nghiệp / SME: chữ ký số doanh nghiệp trên bản này, Ngày 13 / enterprise digital signature on this copy, "
        "Day 13."
    ),
    (
        "Người bảo lãnh cá nhân / Personal Guarantor: mã xác thực một lần gửi qua Zalo, Ngày 13 / one-time code sent by "
        "Zalo, Day 13."
    ),
]
INVESTOR_COPY_SIGNATURE_BOXES = (
    ("DOANH NGHIỆP / SME", "Ký số doanh nghiệp / Enterprise digital signature"),
    ("NGƯỜI BẢO LÃNH CÁ NHÂN / PERSONAL GUARANTOR", "Ký bằng mã OTP qua Zalo / Signs by Zalo one-time code"),
)
# The contract number of an Investor's copy: the application reference, the Investor's sequence in the final pledge
# list, the document and FundLok.
INVESTOR_NUMBER = "{ref}-{seq:02d}/HĐCV-FL"
