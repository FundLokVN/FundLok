"""Contract 3, "Đề nghị giao kết hợp đồng cho vay / Standing Offer to Conclude Loan Contracts", as the SME and its
Personal Guarantor download it after FundLok's assessment, sign it (the SME with its enterprise digital signature,
the Guarantor with a personal one) and upload it back with the SME Service Agreement. Signing publishes the
Listing (app/loans/signing.py).

Copied verbatim from the Contract set (document 3) as it stood on 10 Nov 2026, a DRAFT pending legal review, plus
one section FundLok fills in that the template does not have: the assessment result and repayment estimate
(ASSESSMENT_*), so the investors the Offer is addressed to see what the terms rest on. If the Vietnamese and English
differ, the Vietnamese prevails. VERSION is printed on every page, so a signed copy says which text it was; bump it
whenever a word here changes.
"""

# v0.4: laid out as a Vietnamese contract (national header, date, number); the Guarantor's address the SME enters.
# v0.5: the revenue share s is filled in (the locked run's daily repayment rate) and covered by the check code.
VERSION = "standing-offer-v0.5"

TITLE = ("3. Đề nghị giao kết hợp đồng cho vay", "Standing Offer to Conclude Loan Contracts")

# The number under the title (Đề nghị giao kết), which the Loan Contract cites as the Offer's.
NUMBER = "{ref}/ĐNGK-FL"

PREVAILS = ("Bản tiếng Việt được ưu tiên áp dụng.", "The Vietnamese text prevails.")

SIGNING = (
    "Ký kết: Doanh nghiệp ký bằng chữ ký số doanh nghiệp và Người bảo lãnh ký bằng chữ ký số cá nhân, cùng lúc "
    "với Hợp đồng dịch vụ, sau khi thẩm định xong và Doanh nghiệp đồng ý với điều kiện khoản vay; việc ký sẽ "
    "đăng Khoản gọi vốn. Mỗi Khoản gọi vốn có một Đề nghị này.",
    "Signed: by the SME with its enterprise digital signature and by the Personal Guarantor with a personal "
    "digital signature, at the same time as the SME Service Agreement, once the assessment is done and the SME "
    "agrees to the loan terms; signing publishes the Listing. One Standing Offer per Listing.",
)

# The Offeror's and the Guarantor's paragraphs are built from their own details: see standing_offer.parties().
OFFEROR_VI = (
    "**Bên đề nghị (Doanh nghiệp):** {name}, mã số doanh nghiệp {code}, địa chỉ {address}, đại diện theo pháp "
    "luật {representative}, tài khoản ngân hàng đăng ký {account} tại {bank}."
)
OFFEROR_EN = (
    "Offeror (SME): {name}, enterprise code {code}, address {address}, legal representative {representative}, "
    "registered bank account {account} at {bank}."
)
GUARANTOR_VI = (
    "**Người bảo lãnh cá nhân:** {name}, số định danh cá nhân {id_number}, địa chỉ {address}, vai trò tại Doanh "
    "nghiệp: chủ sở hữu / người đại diện theo pháp luật."
)
GUARANTOR_EN = (
    "Personal Guarantor: {name}, personal ID {id_number}, address {address}, role at the SME: owner / legal "
    "representative."
)

LISTING_TITLE = "Thông tin Khoản gọi vốn / Listing details"

# (label, fixed value) for the rows whose value the template itself states; the rest are filled per Listing.
LISTING_LABELS = {
    "listing_id": "Mã khoản gọi vốn / Listing ID",
    "target": "Số tiền mục tiêu / Target",
    "rate": "Lãi suất / Rate (r)",
    "term": "Thời hạn / Term (M)",
    "instalment_days": "Số ngày trả nợ / Instalment days (N0)",
    "window_opens": "Mở gọi vốn / Window opens",
    "window_closes": "Đóng gọi vốn chậm nhất / Window closes at the latest",
    "acceptance_deadline": "Hạn chấp nhận / Acceptance deadline",
    "disbursement_date": "Ngày giải ngân / Disbursement Date",
    "min_ticket": "Vé tối thiểu / Minimum ticket",
    "revenue_share": "Tỷ lệ doanh thu cho gia hạn / Revenue share for extensions (s)",
}
RATE_VI = "%/năm, tính trên gốc ban đầu (lãi đơn)"
RATE_EN = "a year, flat on original principal"
TARGET_CAP = "(≤ 100.000.000)"
WINDOW_OPENS = "Ngày đăng / Listing date"
WINDOW_CLOSES = "mở + 12 ngày / opening + 12 days"
ACCEPTANCE_DEADLINE = "01 ngày trước Ngày giải ngân / 1 day before the Disbursement Date"
DISBURSEMENT_DATE = "Ngày đăng + 14 ngày / listing date + 14 days"
REVENUE_SHARE_SUFFIX = "% – do hệ thống điền riêng cho từng Khoản gọi vốn / set by the system for each Listing"

# Added by FundLok after the Listing details: what the assessment found and what the loan costs on its term, by
# the repayment schedule's own rules (agreement/terms.repayment_estimate).
ASSESSMENT_TITLE = "Kết quả thẩm định và ước tính trả nợ / Assessment result and repayment estimate"
ASSESSMENT_LABELS = {
    "score": "Điểm doanh nghiệp / Business score",
    "assessed_on": "Ngày thẩm định / Assessed on",
    "principal": "Số tiền gốc (P) / Principal (P)",
    "interest": "Tiền lãi (P × r × M / 12) / Interest",
    "total": "Tổng số tiền phải trả (T0 = P + lãi) / Total repayable (T0)",
    "daily": "Số tiền trả mỗi ngày làm việc / Each working day",
    "backstop": "Hạn chót trả hết / Backstop",
}
ASSESSMENT_DAILY = "{daily} VND × {days} ngày; ngày cuối {last} VND / {daily} VND × {days} days; last day {last} VND"
ASSESSMENT_BACKSTOP = "ngày trả nợ thứ {day} của lịch trả nợ / repayment day {day} of the schedule"
ASSESSMENT_NOTE = (
    "Điểm doanh nghiệp và lãi suất tham khảo là đánh giá nội bộ của FundLok dựa trên hồ sơ Doanh nghiệp tự cung "
    "cấp, không phải xếp hạng tín dụng và không phải cam kết rằng khoản vay sẽ được trả. Tổng số tiền phải trả tính "
    "theo lãi đơn trên gốc ban đầu cho thời hạn M; số tiền mỗi ngày làm việc tính theo lịch trả nợ. Nếu thời hạn "
    "được gia hạn theo Hợp đồng cho vay, số tiền mỗi ngày có thể giảm nhưng tổng số tiền phải trả tăng thêm tiền lãi "
    "của thời gian gia hạn, và không quá hạn chót. Nhà đầu tư chịu rủi ro khoản vay không được trả. Hợp đồng cho vay "
    "là văn bản điều chỉnh.",
    "The business score and reference rate are FundLok's internal assessment of the papers the SME provided itself; "
    "they are not a credit rating and not a promise that the loan will be repaid. The total repayable is simple "
    "interest on the original principal for term M; the amount per working day follows the repayment schedule. If "
    "the term is extended under the Loan Contract, the daily amount can fall but the total rises by the interest "
    "for the extra time, and never past the backstop. Investors bear the risk that the loan is not repaid. The Loan "
    "Contract governs.",
)

# (heading, [(vi, en), ...]) in order.
ARTICLES: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Điều 1. Bản chất của Đề nghị / Nature of the Offer",
        [
            (
                "Đây là đề nghị giao kết hợp đồng cho vay gửi tới công chúng là mọi Nhà đầu tư đã được xác minh trên "
                "nền tảng trực tuyến nơi Đề nghị này được đăng (\"Nền tảng\") (Điều 386 Bộ luật Dân sự 2015). Doanh "
                "nghiệp đề nghị giao kết một hợp đồng cho vay riêng với từng Nhà đầu tư chấp nhận, theo đúng các "
                "điều khoản của Mẫu Hợp đồng cho vay đính kèm (Phụ lục) và Thông tin Khoản gọi vốn nêu trên. Việc "
                "một Nhà đầu tư chấp nhận không chấm dứt Đề nghị đối với các Nhà đầu tư khác cho đến khi đạt số tiền "
                "mục tiêu. Đơn vị vận hành Nền tảng không phải là một bên của Đề nghị hay của bất kỳ hợp đồng cho vay "
                "nào.",
                "This is an offer to conclude loan contracts addressed to the public, being every verified Investor "
                "on the online platform where this Offer is published (the \"Platform\") (Civil Code 2015, Art. "
                "386). The SME offers to conclude a separate loan contract with each Investor who accepts, on the "
                "exact terms of the attached Loan Contract template (Annex) and the Listing details above. One "
                "Investor's acceptance does not end the Offer for the others until the target is reached. The "
                "Platform operator is not a party to this Offer or to any loan contract.",
            )
        ],
    ),
    (
        "Điều 2. Thời hạn hiệu lực / Validity",
        [
            (
                "Đề nghị có hiệu lực từ khi đăng đến thời điểm sớm nhất trong hai thời điểm: tổng cam kết đạt số "
                "tiền mục tiêu, hoặc kết thúc ngày thứ 12 kể từ ngày đăng, để Doanh nghiệp có ngày thứ 13 để quyết "
                "định trước Ngày giải ngân vào ngày thứ 14 (Điều 388, 394). Cam kết sau thời điểm này không hình "
                "thành hợp đồng, trừ khoản bổ sung theo Điều 5.",
                "The Offer is valid from publication until the earlier of: commitments reaching the target, or the "
                "end of day 12 after publication, so the SME has day 13 to decide before the Disbursement Date on "
                "day 14 (Arts. 388 and 394). A commitment after that forms no contract, except a top-up under "
                "Article 5.",
            )
        ],
    ),
    (
        "Điều 3. Chấp nhận Đề nghị (Cam kết) / Acceptance (Commitment)",
        [
            (
                "3.1. Nhà đầu tư chấp nhận bằng cách cam kết một số tiền từ 10.000.000 đồng trở lên, không vượt quá "
                "số còn lại của mục tiêu, và ký Hợp đồng cho vay bằng chữ ký số trên nền tảng. Mỗi Nhà đầu tư chỉ có "
                "một cam kết cho mỗi Khoản gọi vốn. Tại thời điểm cam kết, chưa có tiền nào được chuyển.",
                "3.1. An Investor accepts by committing VND 10,000,000 or more, no more than the amount of the target "
                "still open, and signing the Loan Contract with a digital signature on the platform. Each Investor "
                "has one commitment per Listing. No money moves at commitment.",
            ),
            (
                "3.2. Hợp đồng cho vay được giao kết khi chấp nhận của Nhà đầu tư đến Doanh nghiệp qua nền tảng "
                "(Điều 400), và có hiệu lực theo Điều 3 của Hợp đồng cho vay (Điều 120).",
                "3.2. The Loan Contract is concluded when the Investor's acceptance reaches the SME through the "
                "platform (Art. 400), and takes effect under Article 3 of the Loan Contract (Art. 120).",
            ),
        ],
    ),
    (
        "Điều 4. Quyền chấp nhận của Doanh nghiệp / SME's right to accept or decline",
        [
            (
                "4.1. Khi Đề nghị hết hiệu lực, Doanh nghiệp được xem tổng số tiền cam kết. Đến Hạn chấp nhận (01 "
                "ngày trước Ngày giải ngân), Doanh nghiệp có quyền: (a) chấp nhận tổng số tiền cam kết; hoặc (b) "
                "không chấp nhận; khi đó điều kiện tại Điều 3.2 không xảy ra, mọi hợp đồng cho vay đã giao kết theo "
                "Đề nghị không phát sinh hiệu lực và Doanh nghiệp không phải chịu phạt hay phí.",
                "4.1. When the Offer ends, the SME sees the total committed. Until the acceptance deadline (1 day "
                "before the Disbursement Date), the SME may: (a) accept the total committed; or (b) decline; the "
                "condition in Article 3.2 then fails, no loan contract concluded under this Offer takes effect, and "
                "the SME owes no penalty or fee.",
            ),
            (
                "4.2. Nếu Doanh nghiệp không phản hồi trước Hạn chấp nhận: tổng cam kết đạt từ 50% số tiền mục tiêu "
                "trở lên được coi là Doanh nghiệp chấp nhận; dưới 50% được coi là Doanh nghiệp không chấp nhận. Các "
                "bên thỏa thuận trước hệ quả này của sự im lặng (Điều 393.2).",
                "4.2. If the SME does not respond by the acceptance deadline: commitments of 50% of the target or "
                "more count as accepted by the SME; below 50% count as declined by the SME. The parties agree to "
                "this effect of silence in advance (Art. 393.2).",
            ),
            (
                "4.3. Khi Doanh nghiệp không chấp nhận, Nền tảng thông báo cho mọi Nhà đầu tư đã cam kết; các hợp "
                "đồng cho vay không phát sinh hiệu lực và không ai phải hoàn trả gì vì chưa có tiền được chuyển.",
                "4.3. When the SME declines, the Platform notifies every committed Investor; the loan contracts do "
                "not take effect, and nothing needs to be refunded because no money has moved.",
            ),
        ],
    ),
    (
        "Điều 5. Khoản bổ sung / Top-ups",
        [
            (
                "Nếu một Nhà đầu tư không chuyển tiền trước giờ chốt của Ngày giải ngân, hợp đồng của Nhà đầu tư đó "
                "không phát sinh hiệu lực theo Điều 5.3 của Hợp đồng cho vay; khoản vay với các Nhà đầu tư đã chuyển "
                "tiền vẫn tiếp tục. Phần thiếu hụt được mở lại ngay trên Nền tảng trong 07 ngày cho Nhà đầu tư mới "
                "và Đề nghị này được gia hạn tương ứng. Nhà đầu tư mới cam kết, ký và chuyển tiền ngay theo Điều 5.1 "
                "của Hợp đồng cho vay. Doanh nghiệp đồng ý trước việc nhận các khoản bổ sung trong phạm vi tổng số "
                "tiền đã chấp nhận; điều kiện của mỗi khoản bổ sung theo Điều 7.3 của Hợp đồng cho vay.",
                "If an Investor does not transfer before the cut-off on the Disbursement Date, that Investor's "
                "contract does not take effect under Article 5.3 of the Loan Contract; the loan with the Investors "
                "who did transfer goes ahead. The shortfall reopens on the Platform at once for 7 days for new "
                "Investors, and this Offer is extended accordingly. New Investors commit, sign and transfer straight "
                "away under Article 5.1 of the Loan Contract. The SME agrees in advance to receive these top-ups up "
                "to the total it accepted; each top-up's terms follow Article 7.3 of the Loan Contract.",
            )
        ],
    ),
    (
        "Điều 6. Đề nghị bảo lãnh / Offer of guarantee",
        [
            (
                "Người bảo lãnh nêu tại phần đầu Đề nghị này đề nghị bảo lãnh cho từng Nhà đầu tư giao kết hợp đồng "
                "cho vay theo Đề nghị này, với phạm vi, điều kiện và thời hạn tại Điều 12 của Hợp đồng cho vay (Điều "
                "335 Bộ luật Dân sự 2015). Mỗi chấp nhận của Nhà đầu tư hình thành bảo lãnh cho hợp đồng cho vay của "
                "Nhà đầu tư đó. Người bảo lãnh đã hoàn thành xác minh danh tính (eKYC), đồng ý xử lý dữ liệu cá nhân "
                "và ký Đề nghị này bằng chữ ký số cá nhân.",
                "The Personal Guarantor named at the start of this Offer offers a guarantee to each Investor who "
                "concludes a loan contract under this Offer, with the scope, trigger and term in Article 12 of the "
                "Loan Contract (Civil Code 2015, Art. 335). Each Investor's acceptance forms a guarantee for that "
                "Investor's loan contract. The Guarantor has completed identity verification (eKYC), consented to "
                "personal data processing and signs this Offer with a personal digital signature.",
            )
        ],
    ),
    (
        "Điều 7. Cam kết của Doanh nghiệp / SME undertakings",
        [
            (
                "Doanh nghiệp cam kết: thông tin cung cấp là trung thực; người ký có thẩm quyền và việc vay đã được "
                "chấp thuận theo điều lệ; dùng tiền vay cho vốn lưu động của doanh nghiệp; thông báo ngay mọi sự "
                "kiện ảnh hưởng đến khả năng trả nợ; không đăng khoản gọi vốn mới trên Nền tảng khi khoản vay này "
                "chưa trả hết.",
                "The SME undertakes that: its information is true; the signatory has authority and the borrowing is "
                "approved as its charter requires; it uses the loan for working capital; it will notify at once any "
                "event affecting its ability to repay; it will not post a new listing on the Platform until this "
                "loan is repaid in full.",
            )
        ],
    ),
    (
        "Điều 8. Luật áp dụng, tranh chấp và điều khoản chung / Law, disputes and general",
        [
            (
                "Pháp luật Việt Nam. Thương lượng trong 30 ngày; sau đó Tòa án có thẩm quyền của Việt Nam, hoặc VIAC "
                "nếu các bên thỏa thuận bằng văn bản. Đề nghị và các hợp đồng được giao kết bằng phương tiện điện tử "
                "theo Luật Giao dịch điện tử 2023 và có giá trị như văn bản giấy.",
                "Vietnamese law. Negotiation for 30 days; then the competent Vietnamese court, or VIAC if the parties "
                "agree in writing. The Offer and the contracts are made electronically under the Law on Electronic "
                "Transactions 2023 and have the same force as paper documents.",
            )
        ],
    ),
]

ANNEX_TITLE = "Phụ lục – Mẫu Hợp đồng cho vay / Annex – Loan Contract template"
ANNEX = (
    "Mẫu Hợp đồng cho vay (văn bản số 5) là một phần không tách rời của Đề nghị này.",
    "The Loan Contract template (document 5) forms an integral part of this Offer.",
)

SIGNATURES = (
    ("BÊN ĐỀ NGHỊ – DOANH NGHIỆP / OFFEROR – SME", "Ký số doanh nghiệp / Enterprise digital signature"),
    ("NGƯỜI BẢO LÃNH CÁ NHÂN / PERSONAL GUARANTOR", "Ký số cá nhân / Personal digital signature"),
)
