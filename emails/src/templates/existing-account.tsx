import { Heading, Link, Text } from "@react-email/components";

import { CtaButton, Layout } from "./_components/Layout";
import { v } from "./_components/jinja";
import { copyright, h1, link as linkStyle, paragraph } from "./_components/styles";

// send_existing_account_notice: sent instead of a verification email when
// someone registers with an address that already has an account.
type Props = { loginLink?: string; resetLink?: string; year?: string; preview?: boolean };

export default function ExistingAccount({
  loginLink = v("login_link"),
  resetLink = v("reset_link"),
  year = v("year"),
  preview,
}: Props) {
  return (
    <Layout preview={preview}>
      <Heading as="h1" style={h1}>
        Your account already exists
      </Heading>
      <Text style={paragraph}>
        We received a request to create a new <strong>FundLok</strong> account
        with this email address, but you already have one.
      </Text>
      <Text style={paragraph}>
        If this was you, just sign in — there&apos;s no need to register again. If
        you&apos;ve forgotten your password, you can reset it.
      </Text>
      <CtaButton href={loginLink} label="Sign in" />
      <Text style={paragraph}>
        Forgot your password?{" "}
        <Link href={resetLink} style={linkStyle}>
          Reset it here
        </Link>
        .
      </Text>
      <Text style={paragraph}>
        If you did not try to register, you can safely ignore this email — no
        changes were made to your account.
      </Text>
      <Text style={copyright}>{`© ${year} FundLok. All rights reserved.`}</Text>
    </Layout>
  );
}

ExistingAccount.PreviewProps = {
  loginLink: "https://fundlok.com/login",
  resetLink: "https://fundlok.com/forgot-password",
  year: "2026",
  preview: true,
} satisfies Props;
