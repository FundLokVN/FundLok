import { InternalMessage } from "./_components/InternalMessage";
import type { InternalMessageProps } from "./_components/InternalMessage";

// Admin > Email, template "official": plain message with full company signature block automatically appended

export default function InternalOfficial(props: InternalMessageProps) {
  return <InternalMessage variant="official" {...props} />;
}
