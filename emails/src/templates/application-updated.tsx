import { Heading, Link, Text } from "@react-email/components";

import { CtaButton, Layout } from "./_components/Layout";
import { v } from "./_components/jinja";
import { h1, link as linkStyle, paragraph, small } from "./_components/styles";

// send_application_updated_email. Says only that the application was UPDATED,
// never the decision: the verdict belongs behind the sign-in, with the
// disclosures that must accompany it. Vietnamese first, English beneath,
// because the account has no stored language.
type Props = { link?: string; preview?: boolean };

export default function ApplicationUpdated({ link = v("link"), preview }: Props) {
  return (
    <Layout preview={preview} lang="vi">
      <Heading as="h1" style={heading}>
        Hồ sơ vay của bạn đã được cập nhật
      </Heading>
      <Text style={paragraph}>
        Hồ sơ đề nghị vay của bạn trên <strong>FundLok</strong> vừa có cập nhật
        mới. Vui lòng đăng nhập để xem chi tiết.
      </Text>
      <Heading as="h2" style={subheading}>
        Your loan application has been updated
      </Heading>
      <Text style={paragraph}>
        Your loan application on <strong>FundLok</strong> has a new update. Please
        sign in to see the details.
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

ApplicationUpdated.PreviewProps = {
  link: "https://fundlok.com/dashboard",
  preview: true,
} satisfies Props;

const heading = { ...h1, margin: "0 0 16px", fontSize: "22px" };
const subheading = { ...h1, margin: "28px 0 12px", fontSize: "18px" };
