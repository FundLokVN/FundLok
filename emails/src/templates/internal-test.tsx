import { Heading, Text } from "@react-email/components";

import { INTERNAL_FOOTER, Layout } from "./_components/Layout";
import { v } from "./_components/jinja";
import { h1, paragraph } from "./_components/styles";

// Admin > Email > "Send test email". Always goes to the admin who pressed the
// button, never to an address from the request.
type Props = { fromAddress?: string; preview?: boolean };

export default function InternalTest({
  fromAddress = v("from_address"),
  preview,
}: Props) {
  return (
    <Layout preview={preview} footer={INTERNAL_FOOTER}>
      <Heading as="h1" style={h1}>
        Your email is set up
      </Heading>
      <Text style={paragraph}>
        {`This test email was sent from ${fromAddress} through the FundLok admin console. If you can read it, sending from this account works.`}
      </Text>
    </Layout>
  );
}

InternalTest.PreviewProps = {
  fromAddress: "team@fundlok.com",
  preview: true,
} satisfies Props;
