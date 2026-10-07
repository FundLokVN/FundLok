import {
  InternalMessage,
  internalPreview,
  type InternalMessageProps,
} from "./_components/InternalMessage";

// Admin > Email, template "announcement": an "Announcement" label above the
// heading.
export default function InternalAnnouncement(props: InternalMessageProps) {
  return <InternalMessage variant="announcement" {...props} />;
}

InternalAnnouncement.PreviewProps = internalPreview;
