import { PasswordNotice } from "./_components/PasswordNotice";
import { v } from "./_components/jinja";

// send_password_changed_notice(first_time=False).
type Props = { resetLink?: string; year?: string; preview?: boolean };

export default function PasswordChanged({
  resetLink = v("reset_link"),
  year = v("year"),
  preview,
}: Props) {
  return (
    <PasswordNotice
      headline="Your password was changed"
      lede={
        <>
          The password on your <strong>FundLok</strong> account was just changed.
        </>
      }
      resetLink={resetLink}
      year={year}
      preview={preview}
    />
  );
}

PasswordChanged.PreviewProps = {
  resetLink: "https://fundlok.com/forgot-password",
  year: "2026",
  preview: true,
} satisfies Props;
