import type { CSSProperties } from "react";

// Inline styles only. Email clients are not browsers: Outlook renders through
// Word, Gmail drops <style> blocks in some contexts, and neither does flexbox.
// React Email turns these into the table-and-inline-style markup that works.

export const FONT = "'Helvetica Neue',Arial,sans-serif";
export const GREEN = "#16a34a";
export const INK = "#111827";
export const BODY_TEXT = "#4b5563";
export const MUTED = "#9ca3af";
export const SUBTLE = "#6b7280";

export const paragraph: CSSProperties = {
  fontFamily: FONT,
  fontSize: "16px",
  lineHeight: "1.6",
  color: BODY_TEXT,
  marginTop: "16px",
  marginBottom: "16px",
};

export const h1: CSSProperties = {
  fontFamily: FONT,
  margin: "0 0 20px",
  fontSize: "24px",
  fontWeight: 700,
  color: INK,
};

export const link: CSSProperties = { color: GREEN };

/** The copy-paste fallback under a button: long URLs must wrap. */
export const breakAll: CSSProperties = { ...paragraph, wordBreak: "break-all" };

/** The small grey © line at the foot of the platform emails. */
export const copyright: CSSProperties = {
  ...paragraph,
  color: MUTED,
  fontSize: "13px",
};

export const small: CSSProperties = {
  ...paragraph,
  color: SUBTLE,
  fontSize: "13px",
};
