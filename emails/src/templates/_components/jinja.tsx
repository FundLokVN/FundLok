import { Fragment, type ReactNode } from "react";

// These templates are exported to static HTML and filled in by Python with
// Jinja (app/utils/email_templates.py), so the exported file has to contain
// Jinja syntax where the values go.
//
// A template's props default to their own placeholder, v("link") ->
// "{{ link }}". `email export` renders with no props, so the export carries
// the placeholders; `email dev` renders with the template's PreviewProps, so
// the preview shows realistic values.

/** The Jinja placeholder for a variable. Python escapes every value. */
export const v = (expression: string) => `{{ ${expression} }}`;

/** True while rendering for export: the prop still holds its placeholder. */
export const isPlaceholder = (value: unknown): boolean =>
  typeof value === "string" && value.startsWith("{{");

/**
 * Show `children` only when `test` is set.
 *
 * In the export the value is still a placeholder, so the decision is left to
 * Jinja: this emits `{% if test %}…{% endif %}` around the children. In the dev
 * preview it decides from the preview value. `test` is a bare variable name, never
 * an expression with quotes: React would escape the quotes and Jinja would no
 * longer parse it.
 */
export function If({
  test,
  value,
  children,
}: {
  test: string;
  value: unknown;
  children: ReactNode;
}) {
  if (isPlaceholder(value)) {
    return (
      <>
        {`{% if ${test} %}`}
        {children}
        {"{% endif %}"}
      </>
    );
  }
  return value ? <>{children}</> : null;
}

/**
 * Plain text with its line breaks, for the dev preview. The export keeps the
 * placeholder (`{{ body | nl2br }}`), where Python does the same job.
 */
export function Lines({ text }: { text: string }) {
  if (isPlaceholder(text)) return <>{text}</>;
  return (
    <>
      {text.split("\n").map((line, index) => (
        <Fragment key={index}>
          {index > 0 && <br />}
          {line}
        </Fragment>
      ))}
    </>
  );
}
