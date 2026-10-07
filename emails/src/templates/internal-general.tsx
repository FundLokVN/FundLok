import {
  InternalMessage,
  internalPreview,
  type InternalMessageProps,
} from "./_components/InternalMessage";

// Admin > Email, template "general": a plain message.
export default function InternalGeneral(props: InternalMessageProps) {
  return <InternalMessage variant="general" {...props} />;
}

InternalGeneral.PreviewProps = internalPreview;
