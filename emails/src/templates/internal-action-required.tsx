import {
  InternalMessage,
  internalPreview,
  type InternalMessageProps,
} from "./_components/InternalMessage";

// Admin > Email, template "action_required": an amber "Action required" banner
// above the heading.
export default function InternalActionRequired(props: InternalMessageProps) {
  return <InternalMessage variant="action_required" {...props} />;
}

InternalActionRequired.PreviewProps = internalPreview;
