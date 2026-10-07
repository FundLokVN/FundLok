import { Heading, Link, Text } from "@react-email/components";

import { CtaButton, Layout } from "./_components/Layout";
import { v } from "./_components/jinja";
import { h1, link as linkStyle, paragraph, small } from "./_components/styles";

// send_new_device_signin_alert. device/browser are derived from a header the
// signing-in party controls; Python escapes them like every value.
type Props = {
  browser?: string;
  device?: string;
  where?: string;
  securityLink?: string;
  resetLink?: string;
  preview?: boolean;
};

export default function NewDeviceSignin({
  browser = v("browser"),
  device = v("device"),
  where = v("where"),
  securityLink = v("security_link"),
  resetLink = v("reset_link"),
  preview,
}: Props) {
  return (
    <Layout preview={preview}>
      <Heading as="h1" style={heading}>
        New sign-in to your FundLok account
      </Heading>
      <Text style={paragraph}>
        Someone signed in from <strong>{`${browser} on ${device}`}</strong>
        {` (${where}). If that was you, nothing more is needed.`}
      </Text>
      <Text style={paragraph}>
        If it was not you, sign that device out and change your password now:
      </Text>
      <CtaButton href={securityLink} label="Review signed-in devices" />
      <Text style={small}>
        You can also{" "}
        <Link href={resetLink} style={linkStyle}>
          reset your password
        </Link>
        . This email was sent because sign-in alerts are switched on for your
        account.
      </Text>
    </Layout>
  );
}

NewDeviceSignin.PreviewProps = {
  browser: "Chrome",
  device: "macOS",
  where: "203.0.113.7",
  securityLink: "https://fundlok.com/dashboard/security",
  resetLink: "https://fundlok.com/forgot-password",
  preview: true,
} satisfies Props;

const heading = { ...h1, margin: "0 0 16px", fontSize: "22px" };
