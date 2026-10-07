import type { ReactNode } from "react";
import {
  Body,
  Button,
  Container,
  Head,
  Html,
  Img,
  Section,
  Text,
} from "@react-email/components";

import { v } from "./jinja";
import { FONT, GREEN, MUTED, BODY_TEXT } from "./styles";

export const SYSTEM_FOOTER =
  "FundLok · This is an automated message, please do not reply.";

// The internal emails come from a real mailbox that can take a reply.
export const INTERNAL_FOOTER = "FundLok · Sent by the FundLok team.";

/**
 * The branded shell every email shares: dark logo band, white card, footer.
 *
 * Dark band, not white: the brand mark is only ever placed on black or very
 * dark teal (Handbook v3 §7.3), and the mark itself is the light-on-dark
 * variant.
 *
 * The logo is a CID image attached to the message (app/utils/email.py), not a
 * hosted URL: clients that block remote images, and local development with no
 * public host, would both show a broken box. A base64 data: URI is not an
 * option either, because Gmail strips it. Python passes `logo_src`; the dev
 * preview uses the static copy instead.
 */
export function Layout({
  children,
  footer = SYSTEM_FOOTER,
  lang = "en",
  preview = false,
}: {
  children: ReactNode;
  footer?: string;
  lang?: string;
  preview?: boolean;
}) {
  return (
    <Html lang={lang}>
      <Head />
      <Body style={body}>
        <Container style={container}>
          <Section style={header}>
            <Img
              src={preview ? "/static/email-logo.png" : v("logo_src")}
              alt="FundLok"
              width="180"
              height="64"
              style={logo}
            />
          </Section>
          <Section style={card}>{children}</Section>
          <Section style={footerSection}>
            <Text style={footerText}>{footer}</Text>
          </Section>
        </Container>
      </Body>
    </Html>
  );
}

/**
 * A centred call-to-action button. The colour is set on the anchor itself
 * because some clients (Outlook, a few webmail readers) otherwise repaint the
 * link blue, which is unreadable on the green fill.
 */
export function CtaButton({ href, label }: { href: string; label: string }) {
  return (
    <Section style={buttonRow}>
      <Button href={href} style={button}>
        {label}
      </Button>
    </Section>
  );
}

const body = {
  margin: 0,
  padding: "24px 12px",
  backgroundColor: "#f4f7f6",
  WebkitFontSmoothing: "antialiased" as const,
};

const container = { maxWidth: "600px", width: "100%" };

const header = {
  backgroundColor: "#0a1512",
  padding: "28px 20px",
  borderRadius: "12px 12px 0 0",
  textAlign: "center" as const,
};

const logo = {
  display: "block",
  margin: "0 auto",
  width: "180px",
  height: "auto",
  border: 0,
  outline: "none",
  textDecoration: "none",
};

const card = {
  backgroundColor: "#ffffff",
  padding: "36px 40px",
  fontFamily: FONT,
  color: BODY_TEXT,
  fontSize: "16px",
  lineHeight: "1.6",
};

const footerSection = {
  backgroundColor: "#ffffff",
  borderRadius: "0 0 12px 12px",
  padding: "0 40px 28px",
};

const footerText = {
  fontFamily: FONT,
  borderTop: "1px solid #e5e7eb",
  paddingTop: "18px",
  margin: 0,
  color: MUTED,
  fontSize: "13px",
  lineHeight: "1.6",
  textAlign: "center" as const,
};

const buttonRow = { margin: "32px 0", textAlign: "center" as const };

const button = {
  display: "inline-block",
  padding: "14px 28px",
  backgroundColor: GREEN,
  color: "#ffffff",
  textDecoration: "none",
  borderRadius: "8px",
  fontWeight: 600,
  fontSize: "16px",
  fontFamily: FONT,
};
