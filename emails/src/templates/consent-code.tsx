import { Heading, Text } from "@react-email/components";

import { Layout } from "./_components/Layout";
import { v } from "./_components/jinja";
import { copyright, h1, INK, paragraph } from "./_components/styles";

// app/consent/service.py _deliver_code: the one-time code that confirms
// accepting the Terms & Services and data consent at sign-up.
type Props = { code?: string; minutes?: string; year?: string; preview?: boolean };

const codeStyle = {
  ...paragraph,
  color: INK,
  fontSize: "32px",
  fontWeight: 700,
  letterSpacing: "8px",
  margin: "24px 0",
};

export default function ConsentCode({
  code = v("code"),
  minutes = v("minutes"),
  year = v("year"),
  preview,
}: Props) {
  return (
    <Layout preview={preview}>
      <Heading as="h1" style={h1}>
        Your confirmation code
      </Heading>
      <Text style={paragraph}>
        Enter this code on <strong>FundLok</strong> to accept the Terms &amp;
        Services and data consent:
      </Text>
      <Text style={codeStyle}>{code}</Text>
      <Text style={paragraph}>
        {`It expires in ${minutes} minutes and works once. If you did not ask for it, you can ignore this email.`}
      </Text>
      <Text style={copyright}>{`© ${year} FundLok. All rights reserved.`}</Text>
    </Layout>
  );
}

ConsentCode.PreviewProps = {
  code: "482913",
  minutes: "10",
  year: "2026",
  preview: true,
} satisfies Props;
