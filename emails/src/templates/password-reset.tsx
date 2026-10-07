import { Heading, Link, Text } from "@react-email/components";

import { CtaButton, Layout } from "./_components/Layout";
import { v } from "./_components/jinja";
import { breakAll, copyright, h1, link as linkStyle, paragraph } from "./_components/styles";

// send_password_reset_email.
type Props = { link?: string; year?: string; preview?: boolean };

export default function PasswordReset({
  link = v("link"),
  year = v("year"),
  preview,
}: Props) {
  return (
    <Layout preview={preview}>
      <Heading as="h1" style={h1}>
        Reset Your Password
      </Heading>
      <Text style={paragraph}>
        You requested to reset your password for your <strong>FundLok</strong>{" "}
        account. Click the button below to set a new password:
      </Text>
      <CtaButton href={link} label="Reset Password" />
      <Text style={paragraph}>
        If the button doesn&apos;t work, you can copy and paste the following link
        into your browser:
      </Text>
      <Text style={breakAll}>
        <Link href={link} style={linkStyle}>
          {link}
        </Link>
      </Text>
      <Text style={paragraph}>This password reset link will expire in 1 hour.</Text>
      <Text style={paragraph}>
        If you did not request a password reset, please ignore this email.
      </Text>
      <Text style={copyright}>{`© ${year} FundLok. All rights reserved.`}</Text>
    </Layout>
  );
}

PasswordReset.PreviewProps = {
  link: "https://fundlok.com/reset-password?token=preview-token",
  year: "2026",
  preview: true,
} satisfies Props;
