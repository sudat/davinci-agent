type ErrorNoticeProps = {
  code: string;
  detail: string;
};

/** Structured backend error display: shows the typed code + detail. */
export default function ErrorNotice({ code, detail }: ErrorNoticeProps) {
  return (
    <div className="error-notice" role="alert" data-testid="error-notice">
      <span className="error-code">[{code}]</span>{" "}
      {detail !== "" ? <span>{detail}</span> : null}
    </div>
  );
}
