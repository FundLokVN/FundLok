"""Contract 2, "Hợp đồng dịch vụ dành cho Doanh nghiệp / SME Service
Agreement", as the SME downloads it after FundLok's assessment, signs it with
its enterprise digital signature and uploads it back with the Standing Offer
(app/loans/signing.py).

Copied verbatim from Contract set v0.3 (5 Nov 2026), a DRAFT pending IBL
review. If the Vietnamese and English differ, the Vietnamese prevails.
VERSION is printed on every page, so a signed copy says which text it was;
bump it whenever a word here changes.
"""

VERSION = "sme-agreement-v0.3"

TITLE = ("2. Hợp đồng dịch vụ dành cho Doanh nghiệp", "SME Service Agreement")

PREVAILS = ("Bản tiếng Việt được ưu tiên áp dụng.", "The Vietnamese text prevails.")

SIGNING = (
    "Ký kết: khi Doanh nghiệp chấp nhận điều kiện gọi vốn sau khi FundLok thẩm định, bằng chữ ký số "
    "doanh nghiệp. Mỗi khoản gọi vốn có một Hợp đồng này.",
    "Signed: when the SME accepts its listing terms after FundLok's assessment, with the SME's enterprise "
    "digital signature. One agreement per listing.",
)

PARTY_A = (
    "**Bên A – FundLok:** Công ty Cổ phần FundLok, mã số doanh nghiệp 1501167629, trụ sở chính: Thửa đất "
    "số 31, Tờ bản đồ số 121, khóm Thuận Tiến B, phường Bình Minh, tỉnh Vĩnh Long; đại diện: Ông Vương "
    "Vĩnh Lộc, chức vụ: Tổng Giám đốc.",
    "Party A – FundLok: FundLok Joint-Stock Company, enterprise code 1501167629, head office: Land Parcel "
    "No. 31, Cadastral Map Sheet No. 121, Thuan Tien B Quarter, Binh Minh Ward, Vinh Long Province; "
    "represented by Mr Vuong Vinh Loc, General Director.",
)

# Party B's paragraph is built from the SME's own details: see pdf.party_b().
PARTY_B_VI = (
    "**Bên B – Doanh nghiệp:** {name}, mã số doanh nghiệp {code}, địa chỉ {address}, đại diện theo pháp "
    "luật {representative}, tài khoản ngân hàng đăng ký {account} tại {bank}."
)
PARTY_B_EN = (
    "Party B – SME: {name}, enterprise code {code}, address {address}, legal representative "
    "{representative}, registered bank account {account} at {bank}."
)

# (heading, [(vi, en), ...]) in order.
ARTICLES: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Điều 1. Định nghĩa / Definitions",
        [
            (
                "\"Khoản gọi vốn\": khoản vay Doanh nghiệp đề nghị trên nền tảng theo Điều kiện gọi vốn. "
                "\"Điều kiện gọi vốn\": số tiền, lãi suất, thời hạn do FundLok đề xuất sau thẩm định và Doanh "
                "nghiệp chấp nhận. \"Đề nghị giao kết\": đề nghị giao kết hợp đồng cho vay mà Doanh nghiệp gửi "
                "tới mọi Nhà đầu tư đã xác minh. \"Khoản giải ngân được xác nhận\": khoản tiền Nhà đầu tư chuyển "
                "cho Doanh nghiệp đã được xác nhận theo Hợp đồng cho vay.",
                "\"Listing\": the loan the SME offers on the platform on the Listing Terms. \"Listing Terms\": the "
                "amount, rate and term FundLok proposes after its assessment and the SME accepts. \"Standing "
                "Offer\": the SME's offer to conclude loan contracts, addressed to all verified Investors. "
                "\"Confirmed Disbursement\": money an Investor transfers to the SME that is confirmed under the "
                "Loan Contract.",
            )
        ],
    ),
    (
        "Điều 2. Dịch vụ của FundLok / FundLok's services",
        [
            (
                "FundLok: (a) thẩm định hồ sơ do Doanh nghiệp tự cung cấp và đề xuất Điều kiện gọi vốn; (b) đăng "
                "Khoản gọi vốn và Đề nghị giao kết lên nền tảng; (c) tạo mã VietQR cho giải ngân và trả nợ; (d) "
                "tính lịch trả nợ, tiền lãi, thuế khấu trừ và các trường hợp gia hạn theo công thức trong Hợp "
                "đồng cho vay; (e) lưu trữ hợp đồng, ảnh chụp giao dịch và nhật ký thao tác; (f) gửi thông báo "
                "và nhắc hạn cho các bên.",
                "FundLok: (a) assesses the papers the SME provides itself and proposes the Listing Terms; (b) "
                "publishes the Listing and the Standing Offer on the platform; (c) generates VietQR codes for "
                "disbursement and repayment; (d) computes the repayment schedule, interest, withheld tax and any "
                "extension using the formulas in the Loan Contract; (e) keeps contracts, transfer screenshots "
                "and activity logs; (f) sends notices and reminders to the parties.",
            )
        ],
    ),
    (
        "Điều 3. Những việc FundLok không làm / What FundLok does not do",
        [
            (
                "FundLok không phải là bên cho vay và không bảo lãnh việc trả nợ hay việc gọi đủ vốn. Hợp đồng "
                "này không ủy quyền cho FundLok vay, ký kết hoặc nhận tiền thay Doanh nghiệp. FundLok không nhận, "
                "không giữ và không chuyển tiền vay hoặc tiền trả nợ. Dịch vụ giúp Doanh nghiệp chia sẻ dữ liệu "
                "với Nhà đầu tư và quản lý khoản vay; FundLok không phải là một bên của bất kỳ giao dịch chuyển "
                "tiền nào.",
                "FundLok is not a lender and does not guarantee repayment or full funding. This Agreement gives "
                "FundLok no authority to borrow, sign or receive money for the SME. FundLok never receives, holds "
                "or transfers loan money or repayments. The service lets the SME share data with Investors and "
                "manage its loan; FundLok is not a party to any transfer.",
            )
        ],
    ),
    (
        "Điều 4. Nghĩa vụ của Doanh nghiệp / SME obligations",
        [
            (
                "4.1. Cung cấp hồ sơ trung thực, đầy đủ; thông báo ngay mọi thay đổi ảnh hưởng đến khả năng trả "
                "nợ. Người ký là người đại diện theo pháp luật hoặc được ủy quyền hợp lệ, và việc vay đã được cấp "
                "có thẩm quyền theo điều lệ chấp thuận.",
                "4.1. Provide true and complete papers; notify at once any change affecting its ability to "
                "repay. The signatory is the legal representative or duly authorised, and the borrowing has been "
                "approved as the SME's charter requires.",
            ),
            (
                "4.2. Ký Đề nghị giao kết theo đúng Điều kiện gọi vốn; nhận tiền vào tài khoản ngân hàng đăng ký; "
                "tải ảnh chụp tiền đến trong 24 giờ (quá hạn mà không tải, khoản giải ngân được coi là đã nhận); "
                "trả nợ hằng ngày trực tiếp cho từng Nhà đầu tư và tải ảnh chụp lệnh chuyển; khấu trừ và nộp thuế "
                "theo quy định.",
                "4.2. Sign the Standing Offer exactly on the Listing Terms; receive money only into its "
                "registered bank account; upload a screenshot of money arriving within 24 hours (if it does not, "
                "the disbursement counts as received); repay each Investor directly every day and upload the "
                "transfer screenshot; withhold and pay tax as the law requires.",
            ),
            (
                "4.3. Chỉ có một Khoản gọi vốn tại một thời điểm; không đăng Khoản gọi vốn mới khi khoản vay trước "
                "chưa trả hết. Tổng dư nợ trên nền tảng không vượt quá 100.000.000 đồng.",
                "4.3. Have only one Listing at a time; no new Listing until the previous loan is repaid in full. "
                "Total outstanding on the platform must not exceed VND 100,000,000.",
            ),
            (
                "4.4. Bố trí một người bảo lãnh cá nhân (chủ sở hữu hoặc người đại diện theo pháp luật của Doanh "
                "nghiệp) hoàn thành xác minh danh tính và ký bảo lãnh theo Điều 6 của Đề nghị giao kết và Điều 12 "
                "của Hợp đồng cho vay. Không có bảo lãnh thì Khoản gọi vốn không được đăng.",
                "4.4. Provide a personal guarantor (the SME's owner or legal representative) who completes "
                "identity verification and signs the guarantee in Article 6 of the Standing Offer and Article 12 "
                "of the Loan Contract. Without the guarantee, the Listing is not published.",
            ),
            (
                "4.5. Đến Hạn chấp nhận, Doanh nghiệp chấp nhận hoặc không chấp nhận tổng số tiền được cam kết "
                "theo Điều 4 của Đề nghị giao kết. Doanh nghiệp đồng ý trước rằng nếu không phản hồi trước Hạn "
                "chấp nhận thì áp dụng Điều 4.2 của Đề nghị giao kết (Điều 393.2 Bộ luật Dân sự 2015).",
                "4.5. By the acceptance deadline, the SME accepts or declines the total committed under Article 4 "
                "of the Standing Offer. The SME agrees in advance that if it does not respond by the deadline, "
                "Article 4.2 of the Standing Offer applies (Civil Code 2015, Art. 393.2).",
            ),
        ],
    ),
    (
        "Điều 5. Phí kích hoạt / Activation fee",
        [
            (
                "5.1. Phí kích hoạt bằng 3% tổng Khoản giải ngân được xác nhận, chưa gồm VAT. Không phát sinh phí "
                "nếu Doanh nghiệp không chấp nhận Khoản gọi vốn hoặc không có khoản giải ngân nào được xác nhận.",
                "5.1. The activation fee is 3% of the total Confirmed Disbursements, excluding VAT. No fee is due "
                "if the SME declines the Listing or no disbursement is confirmed.",
            ),
            (
                "5.2. FundLok xuất hóa đơn điện tử sau khi các khoản giải ngân được xác nhận. Doanh nghiệp thanh "
                "toán trong 07 ngày vào tài khoản của FundLok bằng mã QR riêng, tách biệt với mọi khoản trả nợ.",
                "5.2. FundLok issues an e-invoice once the disbursements are confirmed. The SME pays within 7 days "
                "into FundLok's account using a separate QR code, apart from any repayment.",
            ),
            (
                "5.3. Phí kích hoạt là phí dịch vụ của FundLok, không phải tiền lãi, không chia cho Nhà đầu tư và "
                "không bao giờ được khấu trừ vào tiền gốc hoặc khoản trả nợ hằng ngày. Phí không phụ thuộc vào "
                "việc khoản vay được trả hay không.",
                "5.3. The activation fee is FundLok's service fee, not interest; it is never shared with "
                "Investors and never deducted from principal or the daily instalment. The fee does not depend on "
                "whether the loan is repaid.",
            ),
            (
                "5.4. Nếu chậm thanh toán: lãi chậm trả trên số phí quá hạn theo Điều 357 và Điều 468 Bộ luật Dân "
                "sự 2015, tính theo số ngày chậm, và Doanh nghiệp không được đăng Khoản gọi vốn mới cho đến khi "
                "thanh toán.",
                "5.4. If payment is late: late interest on the overdue fee under Civil Code 2015 Arts. 357 and "
                "468, for the days late, and the SME may not post a new Listing until it pays.",
            ),
        ],
    ),
    (
        "Điều 6. Điều kiện gọi vốn / Listing Terms",
        [
            (
                "Điều kiện gọi vốn gồm số tiền (tối đa 100.000.000 đồng), lãi suất (không vượt 20%/năm) và thời "
                "hạn (1–6 tháng), được ghi tại Phụ lục. Đề nghị giao kết phải theo đúng Điều kiện gọi vốn. Kết quả "
                "thẩm định là ý kiến của FundLok, không phải cam kết rằng Khoản gọi vốn sẽ đủ vốn.",
                "The Listing Terms are the amount (at most VND 100,000,000), the rate (no more than 20% a year) "
                "and the term (1–6 months), set out in the Annex. The Standing Offer must follow the Listing "
                "Terms exactly. The assessment is FundLok's opinion, not a promise that the Listing will be "
                "funded.",
            )
        ],
    ),
    (
        "Điều 7. Thời hạn và chấm dứt / Term and termination",
        [
            (
                "Hợp đồng có hiệu lực từ khi ký đến khi khoản vay được trả hết và phí được thanh toán, hoặc khi "
                "Doanh nghiệp không chấp nhận Khoản gọi vốn. Mỗi bên có thể chấm dứt khi bên kia vi phạm nghiêm "
                "trọng và không khắc phục trong 15 ngày kể từ khi được thông báo. Việc chấm dứt không ảnh hưởng "
                "đến các hợp đồng cho vay đã có hiệu lực và phí đã phát sinh. Nếu ngừng cung cấp dịch vụ, FundLok "
                "thông báo trước ít nhất 30 ngày và bàn giao cho Doanh nghiệp các hợp đồng, lịch trả nợ và chứng "
                "từ liên quan đến khoản vay.",
                "This Agreement runs from signing until the loan is fully repaid and the fee is paid, or until "
                "the SME declines the Listing. Either party may terminate if the other commits a serious breach "
                "not cured within 15 days of notice. Termination does not affect loan contracts already in effect "
                "or fees already due. If FundLok stops providing the service, it gives at least 30 days' notice "
                "and hands the SME its contracts, schedules and transfer records.",
            )
        ],
    ),
    (
        "Điều 8. Bảo mật và dữ liệu / Confidentiality and data",
        [
            (
                "Các bên bảo mật thông tin của nhau. Dữ liệu cá nhân được xử lý theo Điều 10 của Điều khoản Dịch "
                "vụ. FundLok chỉ hiển thị cho Nhà đầu tư các thông tin cần thiết để ra quyết định đầu tư.",
                "Each party keeps the other's information confidential. Personal data is processed under Article "
                "10 of the Terms & Services. FundLok shows Investors only the information they need to make an "
                "investment decision.",
            )
        ],
    ),
    (
        "Điều 9. Trách nhiệm / Liability",
        [
            (
                "Mỗi bên chịu trách nhiệm về thiệt hại do lỗi của mình theo pháp luật. FundLok không chịu trách "
                "nhiệm về việc Nhà đầu tư không chuyển tiền.",
                "Each party is liable for losses caused by its own fault as the law provides. FundLok is not "
                "liable for an Investor failing to transfer.",
            )
        ],
    ),
    (
        "Điều 10. Luật áp dụng và tranh chấp / Governing law and disputes",
        [
            (
                "Pháp luật Việt Nam. Thương lượng trong 30 ngày; sau đó Tòa án có thẩm quyền của Việt Nam, hoặc "
                "VIAC nếu các bên thỏa thuận bằng văn bản.",
                "Vietnamese law. Negotiation for 30 days; then the competent Vietnamese court, or VIAC if the "
                "parties agree in writing.",
            )
        ],
    ),
    (
        "Điều 11. Điều khoản chung / General",
        [
            (
                "Hợp đồng được ký bằng chữ ký số và có giá trị như bản giấy. Thứ tự ưu tiên: Hợp đồng này, sau đó "
                "Điều khoản Dịch vụ.",
                "This Agreement is signed digitally and has the same force as a paper contract. Order of "
                "precedence: this Agreement, then the Terms & Services.",
            )
        ],
    ),
]

ANNEX_TITLE = "Phụ lục – Điều kiện gọi vốn / Annex – Listing Terms"

# Shown while the rate is not set yet: it comes from FundLok's assessment.
RATE_AFTER_ASSESSMENT = (
    "Do FundLok đề xuất sau thẩm định, không vượt 20%/năm",
    "Proposed by FundLok after its assessment, at most 20% a year",
)

SIGNATURES = (
    ("ĐẠI DIỆN BÊN A / FOR PARTY A", "Công ty Cổ phần FundLok"),
    ("ĐẠI DIỆN BÊN B / FOR PARTY B", "Ký số doanh nghiệp / Enterprise digital signature"),
)

# What fills a field the platform does not know yet.
BLANK = "................"
