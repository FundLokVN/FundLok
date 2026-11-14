"""Contract 4, "Hợp đồng dịch vụ dành cho Nhà đầu tư / Investor Service
Agreement", as the investor reads it and confirms it with a one-time code
sent to their phone by Zalo, with their first commitment.

Copied from Contract set v0.3 (5 Nov 2026), a DRAFT pending IBL review, except
how Party B signs: v0.3 says "with a remote digital signature"; investors are
individuals who rarely hold one, so from v0.3.1 Party B confirms with a
one-time code (SIGNING, ANNEX_PART_OF, SIGNATURES and SIGNED_BY_CODE below).
That wording is FundLok's own and also awaits IBL's review. If the Vietnamese and English differ, the Vietnamese prevails.
VERSION is printed on every page, so a signed copy says which text it was;
bump it whenever a word here changes, and every investor signs the new text
before their next commitment.
"""

VERSION = "investor-agreement-v0.3.1"

TITLE = ("4. Hợp đồng dịch vụ dành cho Nhà đầu tư", "Investor Service Agreement")

PREVAILS = ("Bản tiếng Việt được ưu tiên áp dụng.", "The Vietnamese text prevails.")

SIGNING = (
    "Ký kết: lần cam kết đầu tiên, Bên B xác nhận bằng mã xác thực một lần (OTP) gửi qua Zalo đến số điện thoại "
    "của Bên B, trong cùng luồng với Hợp đồng cho vay. Các lần cam kết sau chỉ xác nhận Xác nhận đầu tư (Phụ lục).",
    "Signed: at the first commitment, Party B confirming with a one-time code (OTP) sent by Zalo to Party B's "
    "phone number, in the same flow as the Loan Contract. Later commitments confirm only an Investment "
    "Confirmation (Annex).",
)

PARTY_A = (
    "**Bên A – FundLok:** Công ty Cổ phần FundLok, mã số doanh nghiệp 1501167629, trụ sở chính: Thửa đất "
    "số 31, Tờ bản đồ số 121, khóm Thuận Tiến B, phường Bình Minh, tỉnh Vĩnh Long; đại diện: Ông Vương "
    "Vĩnh Lộc, chức vụ: Tổng Giám đốc.",
    "Party A – FundLok: FundLok Joint-Stock Company, enterprise code 1501167629, head office: Land Parcel "
    "No. 31, Cadastral Map Sheet No. 121, Thuan Tien B Quarter, Binh Minh Ward, Vinh Long Province; "
    "represented by Mr Vuong Vinh Loc, General Director.",
)

# Party B's paragraph is built from the investor's own details: see pdf.party_b().
PARTY_B_VI = (
    "**Bên B – Nhà đầu tư:** {name}, số định danh cá nhân / mã số doanh nghiệp {code}, địa chỉ {address}, "
    "tài khoản ngân hàng đăng ký {account} tại {bank}."
)
PARTY_B_EN = (
    "Party B – Investor: {name}, personal ID / enterprise code {code}, address {address}, registered bank "
    "account {account} at {bank}."
)

# (heading, [(vi, en), ...]) in order.
ARTICLES: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Điều 1. Phạm vi / Scope",
        [
            (
                "Hợp đồng này là hợp đồng khung cho mọi khoản đầu tư của Nhà đầu tư trên nền tảng. Mỗi khoản đầu "
                "tư có một Xác nhận đầu tư và một Hợp đồng cho vay riêng giữa Nhà đầu tư và Doanh nghiệp. Cam kết "
                "chưa phải là khoản vay: Doanh nghiệp có thể không chấp nhận đến Hạn chấp nhận, khi đó không có "
                "tiền nào được chuyển.",
                "This is a master agreement for all of the Investor's investments on the platform. Each "
                "investment has its own Investment Confirmation and its own Loan Contract between the Investor "
                "and the SME. A commitment is not yet a loan: the SME may decline up to the acceptance deadline, "
                "in which case no money moves.",
            )
        ],
    ),
    (
        "Điều 2. Dịch vụ của FundLok / FundLok's services",
        [
            (
                "FundLok: (a) hiển thị các Khoản gọi vốn và tóm tắt kết quả thẩm định; (b) cung cấp luồng cam kết "
                "và ký điện tử; (c) tạo mã VietQR cho Ngày giải ngân; (d) đối chiếu ảnh chụp giao dịch, tính lịch "
                "trả nợ, lãi, thuế khấu trừ và gia hạn theo công thức trong Hợp đồng cho vay; (e) lưu hồ sơ; (f) "
                "gửi thông báo, nhắc hạn và báo cáo hằng tháng.",
                "FundLok: (a) shows Listings and a summary of its assessment; (b) provides the commitment and "
                "e-signing flow; (c) generates VietQR codes for the Disbursement Date; (d) checks transfer "
                "screenshots, and computes the schedule, interest, withheld tax and extensions using the formulas "
                "in the Loan Contract; (e) keeps records; (f) sends notices, reminders and monthly statements.",
            )
        ],
    ),
    (
        "Điều 3. Ủy quyền hạn chế / Limited mandate",
        [
            (
                "Nhà đầu tư ủy quyền cho FundLok, chỉ trong phạm vi sau: chia sẻ dữ liệu khoản đầu tư với Doanh "
                "nghiệp; ghi nhận chứng từ và lịch trả nợ; áp dụng công thức gia hạn đã thỏa thuận trong Hợp đồng "
                "cho vay; báo cáo cơ quan nhà nước theo quy định. FundLok không có quyền quyết định đầu tư, ký hợp "
                "đồng hay nhận, giữ, chuyển tiền thay Nhà đầu tư. Thông báo lịch trả nợ và nhắc hạn là tính năng "
                "tự động của nền tảng gửi tới các bên; FundLok không đại diện Nhà đầu tư đòi nợ, không thực hiện "
                "hoạt động thu hồi nợ và không phải là một bên của bất kỳ giao dịch chuyển tiền nào.",
                "The Investor authorises FundLok, only for the following: to share the investment's data with "
                "the SME; to record evidence and schedules; to apply the extension formula agreed in the Loan "
                "Contract; to report to the authorities as the law requires. FundLok has no power to decide "
                "investments, sign contracts, or receive, hold or transfer money for the Investor. Schedule "
                "notices and reminders are an automatic platform feature sent to the parties; FundLok does not "
                "act for the Investor in collecting debts, carries out no debt collection, and is not a party to "
                "any transfer.",
            )
        ],
    ),
    (
        "Điều 4. Những việc FundLok không làm / What FundLok does not do",
        [
            (
                "FundLok không bảo đảm gốc, lãi hay việc gọi đủ vốn; không tư vấn đầu tư; không mua lại khoản vay; "
                "không bù đắp thiếu hụt.",
                "FundLok does not guarantee principal, interest or full funding; gives no investment advice; does "
                "not buy back loans; and does not cover shortfalls.",
            )
        ],
    ),
    (
        "Điều 5. Nghĩa vụ của Nhà đầu tư / Investor obligations",
        [
            (
                "5.1. Hoàn thành xác minh danh tính; chỉ dùng tài khoản ngân hàng chính chủ; mỗi khoản đầu tư tối "
                "thiểu 10.000.000 đồng và tối đa bằng số tiền còn lại của Khoản gọi vốn.",
                "5.1. Complete identity verification; use only bank accounts in its own name; invest at least VND "
                "10,000,000 and at most the amount still open on each Listing.",
            ),
            (
                "5.2. Khi Doanh nghiệp chấp nhận, chuyển đúng số tiền đã cam kết trước 15 giờ Ngày giải ngân và "
                "tải ảnh chụp lệnh chuyển. Với khoản bổ sung theo Điều 5 của Đề nghị giao kết, Nhà đầu tư chuyển "
                "tiền ngay khi cam kết; ngày chuyển tiền là Ngày giải ngân của khoản đó.",
                "5.2. Once the SME accepts, transfer exactly the committed amount before 3 pm on the Disbursement "
                "Date and upload the transfer screenshot. For a top-up under Article 5 of the Standing Offer, the "
                "Investor transfers as soon as it commits; the day of transfer is that tranche's Disbursement "
                "Date.",
            ),
            (
                "5.3. Kiểm tra các khoản trả nợ và bấm \"Đã nhận\" hoặc \"Chưa nhận\" trong 24 giờ; im lặng được "
                "coi là đã nhận, nhưng Nhà đầu tư vẫn có quyền khiếu nại trong 30 ngày kèm sao kê ngân hàng theo "
                "Điều 6 của Hợp đồng cho vay.",
                "5.3. Check repayments and press \"Received\" or \"Not received\" within 24 hours; silence counts "
                "as received, but the Investor may still dispute within 30 days with a bank statement under "
                "Article 6 of the Loan Contract.",
            ),
            (
                "5.4. Nếu không chuyển tiền trước 15 giờ Ngày giải ngân, tài khoản của Nhà đầu tư bị khóa ngay. Để "
                "mở lại ở lần đăng nhập tiếp theo, Nhà đầu tư phải xác minh danh tính (eKYC) lại và ký cam kết "
                "không tái phạm; nếu tái phạm, tài khoản bị khóa vĩnh viễn. Các biện pháp này không áp dụng nếu "
                "Nhà đầu tư chứng minh được sự kiện bất khả kháng hoặc sự cố của hệ thống ngân hàng (Điều 156 Bộ "
                "luật Dân sự 2015).",
                "5.4. If the Investor does not transfer before 3 pm on the Disbursement Date, its account is "
                "locked at once. To unlock it at the next log-on, the Investor must complete identity "
                "verification (eKYC) again and sign an undertaking not to repeat; a repeat means the account is "
                "closed permanently. These measures do not apply if the Investor proves force majeure or a "
                "banking-system outage (Civil Code 2015, Art. 156).",
            ),
        ],
    ),
    (
        "Điều 6. Phí dịch vụ / Service fee",
        [
            (
                "6.1. Phí dịch vụ của mỗi khoản đầu tư bằng 2,5%/năm trên gốc, cố định tại thời điểm ký: tổng phí "
                "= P × 2,5% × M / 12, chưa gồm VAT.",
                "6.1. The service fee for each investment is 2.5% a year on principal, fixed at signing: total "
                "fee = P × 2.5% × M / 12, excluding VAT.",
            ),
            (
                "6.2. Phí phát sinh hằng tháng theo tỷ lệ số tiền Nhà đầu tư thực nhận trong tháng: phí tháng = "
                "tổng phí × (số thực nhận trong tháng / T). Không nhận được tiền thì không phát sinh phí.",
                "6.2. The fee accrues monthly in proportion to what the Investor actually receives that month: "
                "monthly fee = total fee × (received that month / T). No money received means no fee.",
            ),
            (
                "6.3. FundLok xuất hóa đơn hằng tháng. Nhà đầu tư thanh toán trong 07 ngày vào tài khoản của "
                "FundLok bằng mã QR riêng. Phí không bao giờ được khấu trừ vào khoản trả nợ của Doanh nghiệp; "
                "FundLok không chạm vào tiền lãi.",
                "6.3. FundLok invoices monthly. The Investor pays within 7 days into FundLok's account using a "
                "separate QR code. The fee is never deducted from the SME's repayments; FundLok never touches the "
                "interest.",
            ),
            (
                "6.4. Khi còn hóa đơn quá hạn, Nhà đầu tư không được cam kết khoản đầu tư mới.",
                "6.4. While an invoice is overdue, the Investor may not make new commitments.",
            ),
            (
                "6.5. Phí quá hạn: FundLok nhắc thanh toán qua các kênh đã đăng ký. Không áp dụng phí chậm trả; tài "
                "khoản của Nhà đầu tư bị khóa đối với cam kết mới cho đến khi thanh toán. Các khoản đầu tư đang "
                "hiệu lực vẫn tiếp tục. FundLok không sử dụng dịch vụ đòi nợ.",
                "6.5. Overdue fees: FundLok sends payment reminders through the registered channels. There is no "
                "late fee; the Investor's account is locked for new commitments until it pays. Live investments "
                "carry on. FundLok does not use debt-collection services.",
            ),
        ],
    ),
    (
        "Điều 7. Xác nhận đầu tư / Investment Confirmation",
        [
            (
                "Mỗi lần cam kết, Nhà đầu tư ký một Xác nhận đầu tư (Phụ lục) cùng lúc với Hợp đồng cho vay. Xác "
                "nhận đầu tư là một phần của Hợp đồng này và chỉ có hiệu lực khi Hợp đồng cho vay tương ứng có "
                "hiệu lực.",
                "At each commitment, the Investor signs an Investment Confirmation (Annex) together with the Loan "
                "Contract. The Confirmation forms part of this Agreement and takes effect only if the matching "
                "Loan Contract takes effect.",
            )
        ],
    ),
    (
        "Điều 8. Thời hạn và chấm dứt / Term and termination",
        [
            (
                "Hợp đồng không thời hạn. Nhà đầu tư có thể chấm dứt bất kỳ lúc nào bằng thông báo; việc chấm dứt "
                "không ảnh hưởng đến các khoản đầu tư đang hiệu lực, dịch vụ cho các khoản đó và phí đã hoặc sẽ "
                "phát sinh từ các khoản đó. FundLok có thể chấm dứt khi Nhà đầu tư vi phạm nghiêm trọng và không "
                "khắc phục trong 15 ngày. Nếu ngừng cung cấp dịch vụ, FundLok thông báo trước ít nhất 30 ngày và "
                "bàn giao cho Nhà đầu tư các hợp đồng, lịch trả nợ và chứng từ liên quan đến khoản đầu tư.",
                "This Agreement has no fixed term. The Investor may terminate at any time by notice; termination "
                "does not affect live investments, the service for them, or fees accrued or to accrue on them. "
                "FundLok may terminate for a serious breach not cured within 15 days. If FundLok stops providing "
                "the service, it gives at least 30 days' notice and hands the Investor its contracts, schedules "
                "and transfer records.",
            )
        ],
    ),
    (
        "Điều 9. Bảo mật và dữ liệu / Confidentiality and data",
        [
            (
                "Dữ liệu cá nhân được xử lý theo Điều 10 của Điều khoản Dịch vụ. Nhà đầu tư bảo mật thông tin về "
                "Doanh nghiệp nhận được qua nền tảng.",
                "Personal data is processed under Article 10 of the Terms & Services. The Investor keeps "
                "confidential the SME information it receives through the platform.",
            )
        ],
    ),
    (
        "Điều 10. Trách nhiệm / Liability",
        [
            (
                "FundLok chịu trách nhiệm về thiệt hại do lỗi của chính mình theo pháp luật, nhưng không chịu "
                "trách nhiệm về việc Doanh nghiệp không trả nợ.",
                "FundLok is liable for losses caused by its own fault as the law provides, but not for an SME "
                "failing to repay.",
            )
        ],
    ),
    (
        "Điều 11. Luật áp dụng, tranh chấp và điều khoản chung / Law, disputes and general",
        [
            (
                "Pháp luật Việt Nam. Thương lượng trong 30 ngày; sau đó Tòa án có thẩm quyền của Việt Nam, hoặc "
                "VIAC nếu các bên thỏa thuận bằng văn bản. Thứ tự ưu tiên: Xác nhận đầu tư, Hợp đồng này, Điều "
                "khoản Dịch vụ.",
                "Vietnamese law. Negotiation for 30 days; then the competent Vietnamese court, or VIAC if the "
                "parties agree in writing. Order of precedence: the Investment Confirmation, this Agreement, the "
                "Terms & Services.",
            )
        ],
    ),
]

ANNEX_TITLE = "Phụ lục – Xác nhận đầu tư / Annex – Investment Confirmation"

# The Annex as the template prints it: one per commitment, so its values stay blank here.
ANNEX_ROWS = (
    ("Mã khoản gọi vốn / Listing ID", "{blank}"),
    ("Doanh nghiệp / SME", "{blank}"),
    ("Số tiền cam kết / Committed amount (P)", "{blank} VND (≥ 10.000.000)"),
    ("Lãi suất, thời hạn / Rate, term", "{blank}%/năm, {blank} tháng / a year, months"),
    ("Phí dịch vụ dự kiến / Expected service fee", "P × 2,5% × M / 12 = {blank} VND + VAT"),
    ("Ngày giải ngân dự kiến / Expected Disbursement Date", "{blank}"),
)

# The filled-in Annex of one commitment (pdf.render_annex). Not contract text beyond the Article 7 clause it quotes.
ANNEX_PART_OF = (
    "Phụ lục này là một phần của Hợp đồng dịch vụ dành cho Nhà đầu tư, phiên bản {version}, Bên B đã xác nhận bằng mã OTP ngày "
    "{signed_on}.",
    "This Annex forms part of the Investor Service Agreement, version {version}, confirmed by Party B with a one-time code on "
    "{signed_on}.",
)
ANNEX_PARTY_A = "**Bên A – FundLok:** Công ty Cổ phần FundLok, mã số doanh nghiệp 1501167629."
ANNEX_PARTY_B = "**Bên B – Nhà đầu tư:** {name}, số định danh cá nhân / mã số doanh nghiệp {code}."
ANNEX_CONFIRMED = (
    "Bên B xác nhận điện tử trên nền tảng FundLok lúc {at} (giờ Việt Nam), cùng lúc chấp nhận Hợp đồng cho vay.",
    "Party B confirmed electronically on the FundLok platform at {at} (Vietnam time), together with accepting the "
    "Loan Contract.",
)

# Not contract text: under the Annex when the agreement is filled in for the commitment it is signed with.
ANNEX_NOTE_FILLED = (
    "Ghi chú: Phụ lục trên là Xác nhận đầu tư của lần cam kết {ref}, ký cùng Hợp đồng này. Các lần cam kết sau "
    "được điền và xác nhận trên nền tảng.",
    "Note: the Annex above is the Investment Confirmation of commitment {ref}, signed with this Agreement. Later "
    "commitments are completed and confirmed on the platform.",
)

# Not contract text: says how the Annex above gets its values.
ANNEX_NOTE = (
    "Ghi chú: Phụ lục được điền và xác nhận trên nền tảng tại mỗi lần cam kết.",
    "Note: the Annex is completed and confirmed on the platform at each commitment.",
)

SIGNATURES = (
    ("ĐẠI DIỆN BÊN A / FOR PARTY A", "Công ty Cổ phần FundLok"),
    ("BÊN B / PARTY B", "Xác nhận bằng mã OTP qua Zalo / Confirmed by one-time code via Zalo"),
)

# Not contract text: printed under Party B's block on the copy kept once the investor has confirmed with the code.
SIGNED_BY_CODE = (
    "Bên B đã xác nhận Hợp đồng này lúc {at} (giờ Việt Nam) bằng mã xác thực một lần gửi qua Zalo đến số điện "
    "thoại {phone}.",
    "Party B confirmed this Agreement at {at} (Vietnam time) with a one-time code sent by Zalo to phone number "
    "{phone}.",
)

# What fills a field the platform does not know yet.
BLANK = "................"
BLANK_NAME = "......................................"
