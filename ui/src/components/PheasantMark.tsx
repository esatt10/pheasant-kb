/**
 * The pheasant wordmark.
 *
 * The artwork is a full-colour illustration rather than a monochrome glyph, so
 * unlike the letter tile it replaced it does not inherit `currentColor` and the
 * surface behind it stays transparent — the same asset reads correctly on the
 * light and dark bar and on the browser's own tab strip. It is served from
 * `public/`, which is also where the favicon link in `index.html` points, so
 * the tab icon and the in-app mark can never drift apart.
 */
export function PheasantMark({ size = 30 }: { size?: number }) {
  return (
    <img
      src="/pheasant.png"
      width={size}
      height={size}
      alt=""
      aria-hidden
      decoding="async"
    />
  );
}
