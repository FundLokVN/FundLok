import { Heading, Link, Text } from "@react-email/components";

import { CtaButton, Layout } from "./_components/Layout";
import { v } from "./_components/jinja";
import { h1, link as linkStyle, paragraph, small } from "./_components/styles";

// send_sign_in_locked_notice. `where` is the address of the attempt that
// locked the account, as the API saw it; Python escapes it like every value.
type Props = {
  attempts?: string;
  minutes?: string;
  where?: string;
  securityLink?: string;
  resetLink?: string;
  preview?: boolean;
};

export default function SignInLocked({
  attempts = v("attempts"),
  minutes = v("minutes"),
  where = v("where"),
  securityLink = v("security_link"),
  resetLink = v("reset_link"),
  preview,
}: Props) {
  return (
    <Layout preview={preview}>
      <Heading as="h1" style={heading}>
        Too many failed sign-in attempts
      </Heading>
      <Text style={paragraph}>
        Someone entered the wrong password for your <strong>FundLok</strong>{" "}
        account {attempts} times, most recently from {where}. None of those
        attempts signed in. To protect you, signing in with a password is
        paused for {minutes} minutes.
      </Text>
      <Text style={paragraph}>
        If that was you, wait {minutes} minutes and try again, or reset your
        password now:
      </Text>
      <CtaButton href={resetLink} label="Reset your password" />
      <Text style={small}>
        If it was not you, someone may know your email address. A strong
        password and two-factor authentication keep them out; you can turn it
        on from your{" "}
        <Link href={securityLink} style={linkStyle}>
          security settings
        </Link>
        .
      </Text>
    </Layout>
  );
}

SignInLocked.PreviewProps = {
  attempts: "10",
  minutes: "15",
  where: "203.0.113.7",
  securityLink: "https://fundlok.com/dashboard/security",
  resetLink: "https://fundlok.com/forgot-password",
  preview: true,
} satisfies Props;

const heading = { ...h1, margin: "0 0 16px", fontSize: "22px" };
