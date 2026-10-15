import { Heading, Section, Text } from "@react-email/components";

import { CtaButton, INTERNAL_FOOTER, Layout } from "./Layout";
import { If, Lines, v } from "./jinja";
import { FONT, GREEN, h1, paragraph } from "./styles";

export type InternalMessageProps = {
  heading?: string;
  body?: string;
  buttonLabel?: string;
  buttonUrl?: string;
  preview?: boolean;
  includeSignature?: boolean;
  signatureName?: string;
  signatureTitle?: string;
  signaturePhone?: string;
};

/**
 * The body of an email an admin composes in the console (Admin > Email). The
 * admin writes plain text: Python escapes it and turns newlines into <br/>
 * (the nl2br filter), so nothing they type is ever treated as HTML. The heading
 * and the button are optional.
 *
 * The signature is appended only when `includeSignature` is true AND
 * `signatureName` is set. A name with no title/phone still shows; a ticked box
 * with no name shows nothing (there is nothing to sign with).
 */
export function InternalMessage({
  variant,
  heading = v("heading"),
  body = v("body | nl2br"),
  buttonLabel = v("button_label"),
  buttonUrl = v("button_url"),
  preview,
  includeSignature = v("include_signature"),
  signatureName = v("signature_name"),
  signatureTitle = v("signature_title"),
  signaturePhone = v("signature_phone"),
}: InternalMessageProps & {
  variant: "general" | "announcement" | "action_required";
}) {
  return (
    <Layout preview={preview} footer={INTERNAL_FOOTER}>
      {variant === "announcement" && <Text style={eyebrow}>Announcement</Text>}
      {variant === "action_required" && (
        <Section style={callout}>
          <Text style={calloutText}>Action required</Text>
        </Section>
      )}
      <If test="heading" value={heading}>
        <Heading as="h1" style={h1}>
          {heading}
        </Heading>
      </If>
      <Text style={paragraph}>
        <Lines text={body} />
      </Text>
      <If test="button_url" value={buttonUrl}>
        <CtaButton href={buttonUrl} label={buttonLabel} />
      </If>

      <If test="include_signature" value={includeSignature}>
        <br />
        <Text style={signatureName}>{signatureName}</Text>
        <If test="signature_title" value={signatureTitle}>
          <Text style={signatureTitle}>{signatureTitle}</Text>
        </If>
        <If test="signature_phone" value={signaturePhone}>
          <Text style={signatureLine}>
            Mobile: <a href={`tel:${signaturePhone.replace(/\s/g, "")}`} style={signatureLink}>{signaturePhone}</a>
          </Text>
        </If>
      </If>
    </Layout>
  );
}

export const internalPreview = {
  heading: "Quarterly review on Friday",
  body: "Hi team,\n\nThe quarterly review moves to Friday at 10:00.\nPlease have your updates ready the day before.",
  buttonLabel: "Open the agenda",
  buttonUrl: "https://fundlok.com",
  preview: true,
  includeSignature: true,
  signatureName: "Loc Vuong",
  signatureTitle: "Founder & CEO @ FundLok",
  signaturePhone: "+84 943.791.382",
} satisfies InternalMessageProps;

const eyebrow = {
  fontFamily: FONT,
  margin: "0 0 8px",
  color: GREEN,
  fontSize: "12px",
  fontWeight: 700,
  letterSpacing: "0.08em",
  textTransform: "uppercase" as const,
};

const callout = {
  backgroundColor: "#fffbeb",
  borderLeft: "4px solid #f59e0b",
  borderRadius: "6px",
  padding: "10px 16px",
  marginBottom: "20px",
};

const calloutText = {
  fontFamily: FONT,
  margin: 0,
  color: "#92400e",
  fontSize: "14px",
  fontWeight: 700,
};

const signatureName = {
  fontFamily: FONT,
  margin: "0 0 4px",
  fontSize: "18px",
  fontWeight: 700,
};

const signatureTitle = {
  fontFamily: FONT,
  margin: "0 0 12px",
  fontSize: "16px",
  fontWeight: 500,
};

const signatureLine = {
  fontFamily: FONT,
  margin: "0 0 4px",
  fontSize: "16px",
};

const signatureLink = {
  color: "#2563eb",
  textDecoration: "underline",
};
