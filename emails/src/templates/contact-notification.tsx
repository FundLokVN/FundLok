import { Heading, Link, Section, Text } from "@react-email/components";

import { Layout } from "./_components/Layout";
import { Lines, v } from "./_components/jinja";
import { FONT, INK, MUTED, link as linkStyle } from "./_components/styles";

// send_contact_notification: a contact-form submission, forwarded to the team
// inbox. Every value comes from an anonymous public form; Python escapes all of
// them, and `message` goes through nl2br (escape, then newline -> <br/>).
type Props = {
  purpose?: string;
  name?: string;
  email?: string;
  topic?: string;
  message?: string;
  preview?: boolean;
};

export default function ContactNotification({
  purpose = v("purpose"),
  name = v("name"),
  email = v("email"),
  topic = v("topic"),
  message = v("message | nl2br"),
  preview,
}: Props) {
  return (
    <Layout preview={preview}>
      <Heading as="h1" style={h1}>
        New contact form submission
      </Heading>
      <Field label="Purpose">{purpose}</Field>
      <Field label="Name">{name}</Field>
      <Field label="Email">
        <Link href={`mailto:${email}`} style={linkStyle}>
          {email}
        </Link>
      </Field>
      <Field label="Subject">{topic}</Field>
      <Text style={row}>
        <span style={label}>Message</span>
      </Text>
      <Section style={messageBox}>
        <Lines text={message} />
      </Section>
    </Layout>
  );
}

function Field({ label: name, children }: { label: string; children: React.ReactNode }) {
  return (
    <Text style={row}>
      <span style={label}>{name}</span>
      <br />
      {children}
    </Text>
  );
}

ContactNotification.PreviewProps = {
  purpose: "Partnership",
  name: "Nguyễn Văn A",
  email: "someone@example.com",
  topic: "Working together",
  message: "Hello FundLok team,\nWe would like to talk about a partnership.",
  preview: true,
} satisfies Props;

const h1 = {
  margin: "0 0 24px",
  fontSize: "20px",
  fontWeight: 700,
  color: INK,
  fontFamily: FONT,
};

const row = {
  margin: "0 0 8px",
  fontFamily: FONT,
  fontSize: "16px",
  lineHeight: "1.6",
  color: "#4b5563",
};

const label = {
  color: MUTED,
  fontSize: "12px",
  textTransform: "uppercase" as const,
  letterSpacing: "0.08em",
};

const messageBox = {
  color: INK,
  fontSize: "15px",
  lineHeight: "1.6",
  backgroundColor: "#f9fafb",
  borderRadius: "8px",
  padding: "16px",
  marginTop: "8px",
  whiteSpace: "pre-wrap" as const,
  fontFamily: FONT,
};
