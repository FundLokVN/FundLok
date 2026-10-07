import { Heading, Link, Text } from "@react-email/components";

import { CtaButton, Layout } from "./_components/Layout";
import { v } from "./_components/jinja";
import { breakAll, copyright, h1, link as linkStyle, paragraph } from "./_components/styles";

// send_verification_email: the link that activates a new account.
type Props = { link?: string; year?: string; preview?: boolean };

export default function Verification({
  link = v("link"),
  year = v("year"),
  preview,
}: Props) {
  return (
    <Layout preview={preview}>
      <Heading as="h1" style={h1}>
        Verify Your Email Address
      </Heading>
      <Text style={paragraph}>
        Thank you for signing up with <strong>FundLok</strong>. To activate your
        account and start investing or raising funds, please verify your email
        address by clicking the button below:
      </Text>
      <CtaButton href={link} label="Verify Email" />
      <Text style={paragraph}>
        If the button doesn&apos;t work, you can copy and paste the following link
        into your browser:
      </Text>
      <Text style={breakAll}>
        <Link href={link} style={linkStyle}>
          {link}
        </Link>
      </Text>
      <Text style={paragraph}>This verification link will expire in 24 hours.</Text>
      <Text style={copyright}>{`© ${year} FundLok. All rights reserved.`}</Text>
    </Layout>
  );
}

Verification.PreviewProps = {
  link: "https://fundlok.com/verify-email?token=preview-token",
  year: "2026",
  preview: true,
} satisfies Props;
