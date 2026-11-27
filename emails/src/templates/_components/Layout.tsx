import type { ReactNode } from "react";
import {
  Body,
  Button,
  Column,
  Container,
  Head,
  Heading,
  Html,
  Img,
  Row,
  Section,
  Text,
} from "@react-email/components";

import { If, v } from "./jinja";
import { BODY_TEXT, FONT, SUBTLE } from "./styles";

export const SYSTEM_FOOTER =
  "FundLok · This is an automated message, please do not reply.";

// The internal emails come from a real mailbox that can take a reply.
export const INTERNAL_FOOTER = "FundLok · Sent by the FundLok team.";

/**
 * The branded shell every email shares, with a headline banner for composed mail.
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
  heading,
  footer = SYSTEM_FOOTER,
  lang = "en",
  preview = false,
}: {
  children: ReactNode;
  heading?: string;
  footer?: string;
  lang?: string;
  preview?: boolean;
}) {
  return (
    <Html lang={lang}>
      <Head />
      <Body style={body}>
        <Container style={container}>
          <Row>
            <Column style={header}>
              <Img
                src={preview ? "/static/email-logo.png" : v("logo_src")}
                alt="FundLok"
                width="144"
                height="51"
                style={logo}
              />
            </Column>
          </Row>
          <If test="heading" value={heading}>
            <Row>
              <Column style={banner}>
                <Heading as="h1" style={bannerHeading}>{heading}</Heading>
              </Column>
            </Row>
          </If>
          <Row>
            <Column style={card}>{children}</Column>
          </Row>
          <Row>
            <Column style={footerSection}>
              <Text style={footerText}>{footer}</Text>
            </Column>
          </Row>
        </Container>
      </Body>
    </Html>
  );
}

/**
 * The colour is set on the anchor itself
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
  backgroundColor: "#f1f5f3",
  WebkitFontSmoothing: "antialiased" as const,
};

const container = { maxWidth: "600px", width: "100%" };

const header = {
  backgroundColor: "#0a1512",
  padding: "12px 24px",
  borderBottom: "3px solid #8bdc43",
};

const logo = {
  display: "block",
  color: "#ffffff",
  fontFamily: FONT,
  height: "auto",
  border: 0,
  outline: "none",
  textDecoration: "none",
};

const banner = {
  backgroundColor: "#e8f4ee",
  padding: "32px 24px",
  borderBottom: "1px solid #d6e7df",
};

const bannerHeading = {
  margin: 0,
  color: "#103e30",
  fontFamily: FONT,
  fontSize: "30px",
  fontWeight: 700,
  lineHeight: "1.2",
  overflowWrap: "anywhere" as const,
};

const card = {
  backgroundColor: "#ffffff",
  padding: "26px 24px 28px",
  fontFamily: FONT,
  color: BODY_TEXT,
  fontSize: "16px",
  lineHeight: "1.6",
};

const footerSection = {
  backgroundColor: "#f7faf8",
  borderTop: "1px solid #dce7e1",
  padding: "16px 24px",
};

const footerText = {
  fontFamily: FONT,
  margin: 0,
  color: SUBTLE,
  fontSize: "13px",
  lineHeight: "1.6",
};

const buttonRow = { margin: "24px 0 12px", textAlign: "left" as const };

const button = {
  display: "inline-block",
  padding: "14px 28px",
  backgroundColor: "#14633e",
  color: "#ffffff",
  textDecoration: "none",
  borderRadius: "4px",
  fontWeight: 600,
  fontSize: "16px",
  fontFamily: FONT,
};
