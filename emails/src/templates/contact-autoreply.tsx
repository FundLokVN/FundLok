import { Heading, Text } from "@react-email/components";

import { Layout } from "./_components/Layout";
import { INK, paragraph } from "./_components/styles";

// send_contact_autoreply. Takes NO variables, on purpose: it goes to whatever
// address an anonymous submitter typed, so nothing they wrote may appear in it
// (see the docstring in app/utils/email.py).
export default function ContactAutoreply({ preview }: { preview?: boolean }) {
  return (
    <Layout preview={preview}>
      <Heading as="h2" style={h2}>
        Thank you for contacting FundLok!
      </Heading>
      <Text style={paragraph}>Hello,</Text>
      <Text style={paragraph}>
        We have successfully received your information. Our team will review your
        message and get back to you as soon as possible.
      </Text>
      <Text style={paragraph}>
        Best regards,
        <br />
        The FundLok Team
      </Text>
    </Layout>
  );
}

ContactAutoreply.PreviewProps = { preview: true };

const h2 = { margin: "0 0 20px", fontSize: "22px", color: INK };
