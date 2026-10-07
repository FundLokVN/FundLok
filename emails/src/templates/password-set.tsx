import { PasswordNotice } from "./_components/PasswordNotice";
import { v } from "./_components/jinja";

// send_password_changed_notice(first_time=True): an OAuth account adding its
// first password.
type Props = { resetLink?: string; year?: string; preview?: boolean };

export default function PasswordSet({
  resetLink = v("reset_link"),
  year = v("year"),
  preview,
}: Props) {
  return (
    <PasswordNotice
      headline="Your password was set"
      lede={
        <>
          A password was just added to your <strong>FundLok</strong> account. You
          can now sign in with your email address and password.
        </>
      }
      resetLink={resetLink}
      year={year}
      preview={preview}
    />
  );
}

PasswordSet.PreviewProps = {
  resetLink: "https://fundlok.com/forgot-password",
  year: "2026",
  preview: true,
} satisfies Props;
