import { Heading, Link, Section, Text } from "@react-email/components";

import { CtaButton, Layout } from "./_components/Layout";
import { v } from "./_components/jinja";
import {
  BODY_TEXT,
  FONT,
  INK,
  h1,
  link as linkStyle,
  paragraph,
  small,
} from "./_components/styles";

// send_investment_receipt_email (app/market/emails.py). Tells the business that
// an investor has uploaded the bank receipt for a transfer to its account, so it
// can look for the money under the payment memo. Says the transfer was
// REPORTED, never that it arrived: FundLok has only the investor's receipt
// until staff check it. The investor's name is masked as on the business's own
// view of its round. Vietnamese first, English beneath, because the account has
// no stored language.
type Props = {
  investor?: string;
  project?: string;
  amountVi?: string;
  amountEn?: string;
  memo?: string;
  uploadedAt?: string;
  link?: string;
  preview?: boolean;
};

export default function InvestmentReceipt({
  investor = v("investor"),
  project = v("project"),
  amountVi = v("amount_vi"),
  amountEn = v("amount_en"),
  memo = v("memo"),
  uploadedAt = v("uploaded_at"),
  link = v("link"),
  preview,
}: Props) {
  return (
    <Layout preview={preview} lang="vi">
      <Heading as="h1" style={heading}>
        Nhà đầu tư đã báo chuyển tiền cho doanh nghiệp của bạn
      </Heading>
      <Text style={paragraph}>
        Nhà đầu tư <strong>{investor}</strong> vừa tải lên biên lai cho khoản
        chuyển vào tài khoản ngân hàng của doanh nghiệp bạn, cho dự án{" "}
        <strong>{project}</strong>.
      </Text>
      <Section style={details}>
        <Text style={detailRow}>
          Số tiền: <strong style={value}>{amountVi}</strong>
        </Text>
        <Text style={detailRow}>
          Nội dung chuyển khoản: <strong style={code}>{memo}</strong>
        </Text>
        <Text style={detailRow}>
          Tải lên lúc: <strong style={value}>{uploadedAt}</strong>
        </Text>
      </Section>
      <Text style={paragraph}>
        Hãy kiểm tra tài khoản ngân hàng của bạn để tìm khoản tiền có nội dung
        này. FundLok đang đối chiếu biên lai với lượt giữ chỗ. Nếu bạn không thấy
        khoản tiền này, hãy liên hệ bộ phận hỗ trợ của FundLok.
      </Text>

      <Heading as="h2" style={subheading}>
        An investor reported a transfer to your business
      </Heading>
      <Text style={paragraph}>
        Investor <strong>{investor}</strong> has uploaded the receipt for a
        transfer to your business&apos;s bank account, for the project{" "}
        <strong>{project}</strong>.
      </Text>
      <Section style={details}>
        <Text style={detailRow}>
          Amount: <strong style={value}>{amountEn}</strong>
        </Text>
        <Text style={detailRow}>
          Payment memo: <strong style={code}>{memo}</strong>
        </Text>
        <Text style={detailRow}>
          Uploaded: <strong style={value}>{uploadedAt}</strong>
        </Text>
      </Section>
      <Text style={paragraph}>
        Check your bank account for money carrying this memo. FundLok is
        checking the receipt against the reservation. If you don&apos;t see
        this money, contact FundLok support.
      </Text>

      <CtaButton href={link} label="Xem trên FundLok / View on FundLok" />
      <Text style={{ ...small, wordBreak: "break-all" }}>
        <Link href={link} style={linkStyle}>
          {link}
        </Link>
      </Text>
    </Layout>
  );
}

InvestmentReceipt.PreviewProps = {
  investor: "N. A***",
  project: "CÔNG TY CỔ PHẦN FUNDLOK",
  amountVi: "10.000.000 ₫",
  amountEn: "₫10,000,000",
  memo: "FL8TKK1UAQ",
  uploadedAt: "02/11/2026 12:40 (GMT+7)",
  link: "https://fundlok.com/dashboard",
  preview: true,
} satisfies Props;

const heading = { ...h1, margin: "0 0 16px", fontSize: "22px" };
const subheading = { ...h1, margin: "28px 0 12px", fontSize: "18px" };
const details = {
  backgroundColor: "#f9fafb",
  border: "1px solid #e5e7eb",
  borderRadius: "8px",
  padding: "12px 16px",
  margin: "0 0 16px",
};
const detailRow = {
  fontFamily: FONT,
  fontSize: "14px",
  lineHeight: "22px",
  color: BODY_TEXT,
  margin: "0",
};
const value = { color: INK };
const code = { color: INK, fontFamily: "Menlo,Consolas,monospace" };
