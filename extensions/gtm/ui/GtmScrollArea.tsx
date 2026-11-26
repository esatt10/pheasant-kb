import { useEffect, useRef, useState, type ReactNode } from "react";

export function GtmScrollArea({ label, className, children }: {
  label: string;
  className?: string;
  children: ReactNode;
}) {
  const areaRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState(false);

  useEffect(() => {
    const area = areaRef.current;
    const content = contentRef.current;
    if (!area || !content) return;
    const measure = () => setOverflow(
      area.scrollHeight > area.clientHeight || area.scrollWidth > area.clientWidth,
    );
    const observer = new ResizeObserver(measure);
    observer.observe(area);
    observer.observe(content);
    measure();
    return () => observer.disconnect();
  }, []);

  return <div ref={areaRef} className={className ? `gtm-scroll-area ${className}` : "gtm-scroll-area"}
    role={overflow ? "region" : undefined} aria-label={overflow ? label : undefined}
    tabIndex={overflow ? 0 : undefined}>
    <div ref={contentRef} className="gtm-scroll-area__content">{children}</div>
  </div>;
}
