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
};

/**
 * The body of an email an admin composes in the console (Admin > Email). The
 * admin writes plain text: Python escapes it and turns newlines into <br/>
 * (the nl2br filter), so nothing they type is ever treated as HTML. The heading
 * and the button are optional.
 */
export function InternalMessage({
  variant,
  heading = v("heading"),
  body = v("body | nl2br"),
  buttonLabel = v("button_label"),
  buttonUrl = v("button_url"),
  preview,
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
    </Layout>
  );
}

export const internalPreview = {
  heading: "Quarterly review on Friday",
  body: "Hi team,\n\nThe quarterly review moves to Friday at 10:00.\nPlease have your updates ready the day before.",
  buttonLabel: "Open the agenda",
  buttonUrl: "https://fundlok.com",
  preview: true,
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
