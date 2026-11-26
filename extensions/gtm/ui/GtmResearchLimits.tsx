export const MAX_RESEARCH_LIMIT = 250;

export type ResearchLimits = { page: string; excerpt: string };

export function validLimit(value: string, min: number): boolean {
  return value === "" || (/^\d+$/.test(value) && Number(value) >= min && Number(value) <= MAX_RESEARCH_LIMIT);
}

export function GtmResearchLimits({ value, onChange, deep }: {
  value: ResearchLimits;
  onChange: (value: ResearchLimits) => void;
  deep: boolean;
}) {
  return <>
    <label className="field">
      <span>General page attempts</span>
      <input className="text-input" type="number" min={4} max={MAX_RESEARCH_LIMIT}
        value={value.page} onChange={(event) => onChange({ ...value, page: event.target.value })}
        aria-invalid={!validLimit(value.page, 4)} />
    </label>
    {deep ? <label className="field">
      <span>Company excerpts</span>
      <input className="text-input" type="number" min={1} max={MAX_RESEARCH_LIMIT}
        value={value.excerpt} onChange={(event) => onChange({ ...value, excerpt: event.target.value })}
        aria-invalid={!validLimit(value.excerpt, 1)} />
    </label> : null}
  </>;
}
