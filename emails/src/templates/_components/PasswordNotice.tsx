import type { ReactNode } from "react";
import { Heading, Text } from "@react-email/components";

import { CtaButton, Layout } from "./Layout";
import { copyright, h1, paragraph } from "./styles";

/**
 * The body shared by password-changed and password-set: the two differ only
 * in the headline and the opening line ("was changed" would be untrue for an
 * account setting its first password). Two templates rather than a Jinja
 * branch, so each export reads as the email it sends.
 */
export function PasswordNotice({
  headline,
  lede,
  resetLink,
  year,
  preview,
}: {
  headline: string;
  lede: ReactNode;
  resetLink: string;
  year: string;
  preview?: boolean;
}) {
  return (
    <Layout preview={preview}>
      <Heading as="h1" style={h1}>
        {headline}
      </Heading>
      <Text style={paragraph}>{lede}</Text>
      <Text style={paragraph}>
        If this was you, nothing further is needed — you can ignore this email.
      </Text>
      <Text style={paragraph}>
        <strong>If this wasn&apos;t you</strong>, reset your password immediately
        using the button below, and contact us so we can secure your account.
      </Text>
      <CtaButton href={resetLink} label="Reset your password" />
      <Text style={copyright}>{`© ${year} FundLok. All rights reserved.`}</Text>
    </Layout>
  );
}
