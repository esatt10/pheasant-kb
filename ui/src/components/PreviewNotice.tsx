/** A page whose feature is still in preview says so before anyone acts on it. */
export function PreviewNotice({ feature }: { feature: string }) {
  return (
    <div className="banner banner--warn preview-notice" role="note">
      <span className="pill preview-notice__pill">Preview</span>
      <span>
        {feature} is in preview. Its behaviour is not yet consistent between runs or
        releases, so treat its numbers and recommendations as indicative, not as decisions.
      </span>
    </div>
  );
}
