import { Heading, Link, Text } from "@react-email/components";

import { CtaButton, Layout } from "./_components/Layout";
import { v } from "./_components/jinja";
import { breakAll, copyright, h1, link as linkStyle, paragraph } from "./_components/styles";

// send_admin_invite_email. `inviter` is a person's display name, which is
// user-editable profile text: Python escapes it like every value.
type Props = {
  inviter?: string;
  setupLink?: string;
  forgotLink?: string;
  expiresHours?: string;
  year?: string;
  preview?: boolean;
};

export default function AdminInvite({
  inviter = v("inviter"),
  setupLink = v("setup_link"),
  forgotLink = v("forgot_link"),
  expiresHours = v("expires_hours"),
  year = v("year"),
  preview,
}: Props) {
  return (
    <Layout preview={preview}>
      <Heading as="h1" style={h1}>
        Set up your admin account
      </Heading>
      <Text style={paragraph}>
        <strong>{inviter}</strong> has created a <strong>FundLok</strong> admin
        account for this email address. Choose a password to finish setting it
        up:
      </Text>
      <CtaButton href={setupLink} label="Set your password" />
      <Text style={paragraph}>
        If the button doesn&apos;t work, copy and paste this link into your
        browser:
      </Text>
      <Text style={breakAll}>
        <Link href={setupLink} style={linkStyle}>
          {setupLink}
        </Link>
      </Text>
      <Text style={paragraph}>
        {`This link works once and expires in ${expiresHours} hours. If it has expired, request a new one at `}
        <Link href={forgotLink} style={linkStyle}>
          {forgotLink}
        </Link>
        .
      </Text>
      <Text style={paragraph}>
        If you weren&apos;t expecting this, you can ignore this email, and please
        let us know.
      </Text>
      <Text style={copyright}>{`© ${year} FundLok. All rights reserved.`}</Text>
    </Layout>
  );
}

AdminInvite.PreviewProps = {
  inviter: "Phat Huynh",
  setupLink: "https://fundlok.com/reset-password?token=preview-token&invite=1",
  forgotLink: "https://fundlok.com/forgot-password",
  expiresHours: "24",
  year: "2026",
  preview: true,
} satisfies Props;
