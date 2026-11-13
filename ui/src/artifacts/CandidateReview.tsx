/**
 * Reviewing what the region proposes, at the scale it actually proposes it.
 *
 * A list of one-line proposals is fine for three and useless for three
 * hundred, and it never answered the first question a reviewer has: *why is
 * this being suggested?* `router -> pheasant-flock` with "seen 4×" attached is
 * an assertion, not evidence.
 *
 * So the page is built in four layers, because a reviewer asks four questions
 * in order, each one narrower than the last:
 *
 *   1. **What is claimed** — the row: rule, kind, scope, how strong.
 *   2. **On what basis** — expand once: the calls behind it. What was asked,
 *      what came back.
 *   3. **How do I check it** — expand again: the spans. Which trace, which
 *      parent, when, how long — enough to follow one proposal back through a
 *      collector to the request that produced it.
 *   4. **What is actually behind that key** — select a span or the criteria
 *      and a side panel opens on the call: the ids in full, the criteria the
 *      search ran under, and the content-addressed result keys. Selecting one
 *      of those resolves the hash to the text it names.
 *
 * Layer 4 is a *panel*, not another nested disclosure, for two reasons. The
 * content behind a key is unbounded — a chunk is as long as a chunk is — and
 * inlining it would push the row a reviewer is comparing against off the
 * screen. And a reviewer comparing two calls wants the list to hold still
 * while the detail changes beside it, which is what a docked panel does and
 * an accordion cannot.
 *
 * Nothing below layer 1 is fetched until it is opened, and layer 4's content
 * lookup happens only on selection. A hundred proposals would otherwise be a
 * hundred evidence queries nobody asked for, and one opened proposal would be
 * a chunk fetch per result id.
 *
 * And at scale the work is triage, not reading: proposals group by rule
 * (a hundred candidates are usually three or four rules), filter by text,
 * and act in bulk. Selecting a whole group and promoting it is one gesture.
 */
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api } from "../api/client";
import type { CandidateInteraction, MemoryCandidate } from "../api/types";

/** Rows rendered per group before "show more". The DOM cost of a few hundred
 *  expanded-capable rows is real, and a reviewer works top-down anyway. */
const PAGE = 25;

/**
 * What the side panel is showing.
 *
 * A call is always the anchor. A hash key on its own is a string with no
 * provenance — the useful question is never "what is this id" but "what did
 * *this call* get back", so the panel opens on the call and the key is a
 * focus within it.
 */
interface Selection {
  candidate: MemoryCandidate;
  call: CandidateInteraction;
  /** A result id drilled into, or null while the call itself is in view. */
  keyId: string | null;
}

function relative(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function parseList(raw: string | null | undefined): string[] {
  if (!raw) return [];
  try {
    const value = JSON.parse(raw);
    return Array.isArray(value) ? value.map(String) : [];
  } catch {
    return [];
  }
}

function parseObject(raw: string | null | undefined): Record<string, unknown> {
  if (!raw) return {};
  try {
    const value = JSON.parse(raw);
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

/**
 * Result ids come in two grammars and the list should read as one thing.
 *
 * A chunk id carries its own content hash — `chunk:{source}:{path}:sha256=
 * <hex>:chunk=0007` — and an artifact id names a whole document —
 * `file:{source}:{path}:branch={b}`. Which one a call recorded is not the
 * reviewer's problem, so both render as the path they name, with the hash
 * shown underneath where there is one. The untruncated id is always one row
 * above, because the reason to open this is to get a value you can paste.
 */
function keyLabel(id: string): { head: string; hash: string | null } {
  const chunk = /^chunk:[^:]+:(.+):sha256=([0-9a-f]+):chunk=(\d+)$/.exec(id);
  if (chunk) return { head: `${chunk[1]} #${Number(chunk[3])}`, hash: chunk[2] };
  const artifact = /^file:[^:]+:(.+):branch=([^:]*)$/.exec(id);
  if (artifact) return { head: artifact[1], hash: null };
  return { head: id, hash: null };
}

/** The text a content-addressed key names, fetched only once it is selected. */
function KeyContent({ keyId }: { keyId: string }) {
  const content = useQuery({
    queryKey: ["node-content", keyId],
    queryFn: () => api.nodeContent(keyId),
    retry: false,
  });

  if (content.isPending) return <p className="candidate__hint">Resolving the key…</p>;
  if (content.isError) {
    const status = content.error instanceof ApiError ? content.error.status : 0;
    if (status === 404) {
      // Not an error so much as an answer. The id names an exact content
      // hash, so re-indexed or deleted text leaves the key pointing at
      // nothing — which tells the reviewer that what this call returned is
      // no longer what the region holds.
      return (
        <p className="sidepanel__note">
          This key does not resolve in the current index. It names an exact
          content hash, so the text it was cut from has changed or been removed
          since the call — what came back then is not what is indexed now.
        </p>
      );
    }
    if (status === 403) {
      return <p className="sidepanel__note">You are not permitted to read the content behind this key.</p>;
    }
    return <p className="error">{String(content.error)}</p>;
  }

  const text = content.data.content;
  if (!text) {
    return <p className="sidepanel__note">The key resolves, but the row carries no text.</p>;
  }
  return <pre className="sidepanel__content">{text}</pre>;
}

/** One labelled hash, shown whole. Truncation belongs in the list, not here —
 *  the reason to open the panel is to get the value you can paste. */
function KeyRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="sidepanel__kv">
      <span className="sidepanel__k">{label}</span>
      <code className="sidepanel__v">{value}</code>
    </div>
  );
}

/**
 * Layer 4. Docked beside the list, not over it: the reviewer is comparing a
 * proposal against its evidence, and a modal would hide the thing being
 * compared.
 *
 * One call, whole, in one scrolling column — the correlation keys, the
 * criteria the search actually ran under, what was asked, and the ids that
 * came back. A selected key resolves *in place* rather than replacing the
 * view, because the question a reviewer is answering is "did this criteria
 * produce that text", and an answer that hides half the question is not one.
 */
function CriteriaPanel({
  selection,
  onFocusKey,
  onClear,
}: {
  selection: Selection;
  onFocusKey: (keyId: string | null) => void;
  onClear: () => void;
}) {
  const { candidate, call, keyId } = selection;
  const ids = parseList(call.result_ids_json);
  const paths = parseList(call.result_paths_json);
  const criteria = parseObject(call.criteria_json);
  const criteriaEntries = Object.entries(criteria).filter(
    ([, value]) => value !== null && value !== undefined && value !== "",
  );

  // Escape clears, which is the one gesture every panel in every tool shares.
  // It unwinds one level at a time: an open key first, then the panel, so a
  // reviewer reading a chunk does not lose the call by pressing it once.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (keyId) onFocusKey(null);
      else onClear();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [keyId, onFocusKey, onClear]);

  return (
    <aside className="sidepanel" aria-label="Selected call">
      <header className="sidepanel__head">
        <div className="sidepanel__title">
          <code>{call.operation}</code>
          <span className="candidate__hint">{relative(call.started_at)}</span>
        </div>
        <button type="button" className="sidepanel__close" onClick={onClear} aria-label="Clear selection">
          Clear
        </button>
      </header>
      <p className="sidepanel__sub candidate__hint">
        One call behind <code>{candidate.rule_id}</code>
      </p>

      <div className="sidepanel__body">
        <h4 className="sidepanel__h">Correlation keys</h4>
        <KeyRow label="trace" value={call.trace_id} />
        <KeyRow label="span" value={call.span_id} />
        {call.parent_span_id && <KeyRow label="parent" value={call.parent_span_id} />}
        <KeyRow label="event" value={call.id} />

        <h4 className="sidepanel__h">Criteria</h4>
        {criteriaEntries.length === 0 ? (
          <p className="sidepanel__note">
            No criteria were recorded — the call ran with this region&rsquo;s
            defaults.
          </p>
        ) : (
          <div>
            {criteriaEntries.map(([key, value]) => (
              <KeyRow
                key={key}
                label={key}
                value={typeof value === "string" ? value : JSON.stringify(value)}
              />
            ))}
          </div>
        )}

        <h4 className="sidepanel__h">Asked</h4>
        <p className="sidepanel__ask">{call.query_text ?? <em>redacted</em>}</p>

        {call.answer_text && (
          <>
            <h4 className="sidepanel__h">Answered</h4>
            <pre className="sidepanel__content sidepanel__content--short">{call.answer_text}</pre>
          </>
        )}

        <h4 className="sidepanel__h">
          Content keys
          {ids.length > 0 && <span className="candidates__count">{ids.length}</span>}
        </h4>
        {ids.length === 0 ? (
          <p className="sidepanel__note">
            {paths.length > 0
              ? "This surface answered with paths rather than ids, so there is nothing here to resolve."
              : "Nothing came back — this call is evidence of a gap, not of a match."}
          </p>
        ) : (
          <>
            <p className="sidepanel__note">Select one to read what it names.</p>
            <ul className="sidepanel__keys">
              {ids.map((id) => {
                const { head, hash } = keyLabel(id);
                const isOpen = keyId === id;
                return (
                  <li key={id}>
                    <button
                      type="button"
                      className="sidepanel__key"
                      onClick={() => onFocusKey(isOpen ? null : id)}
                      aria-expanded={isOpen}
                      title={id}
                    >
                      <span className="sidepanel__key-head">
                        {isOpen ? "▾" : "▸"} {head}
                      </span>
                      {hash && <code className="sidepanel__key-hash">sha256={hash.slice(0, 12)}…</code>}
                    </button>
                    {isOpen && (
                      <div className="sidepanel__resolved">
                        <KeyRow label="id" value={id} />
                        <KeyContent keyId={id} />
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </>
        )}

        {paths.length > 0 && (
          <>
            <h4 className="sidepanel__h">Paths</h4>
            <ul className="sidepanel__paths">
              {paths.map((path) => (
                <li key={path}>
                  <code className="candidate__path">{path}</code>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </aside>
  );
}

/** Layers 2 and 3 for one proposal. Fetched only when opened. */
function Evidence({
  candidate,
  selection,
  onSelect,
}: {
  candidate: MemoryCandidate;
  selection: Selection | null;
  onSelect: (call: CandidateInteraction, keyId: string | null) => void;
}) {
  const [showTrace, setShowTrace] = useState(false);
  const trail = useQuery({
    queryKey: ["memory-candidate-evidence", candidate.id],
    queryFn: () => api.artifactCandidateEvidence(candidate.id),
    retry: false,
  });

  if (trail.isPending) return <p className="candidate__hint">Loading the evidence…</p>;
  if (trail.isError) {
    return <p className="error">Could not load the evidence: {String(trail.error)}</p>;
  }

  const { interactions, named, found } = trail.data;
  const selectedCall = selection?.candidate.id === candidate.id ? selection.call.id : null;

  return (
    <div className="candidate__evidence">
      <div className="candidate__evidence-head">
        <strong>Why this was proposed</strong>
        {/* Evidence ages out: the hot window is retention-bounded, so a
            pending proposal can outlive the rows behind it. Saying so beats
            showing a short list that looks like the whole story. */}
        {found < named && (
          <span className="candidate__hint">
            {found} of {named} recorded calls still in the hot window — the rest
            have aged out past the retention window.
          </span>
        )}
      </div>

      {interactions.length === 0 ? (
        <p className="candidate__hint">
          No contributing calls are still retained. The counts above remain the
          totals the rule actually saw.
        </p>
      ) : (
        <table className="candidate__calls">
          <thead>
            <tr>
              <th>Asked</th>
              <th>Came back</th>
              <th>Where</th>
              <th>When</th>
            </tr>
          </thead>
          <tbody>
            {interactions.map((call) => {
              const paths = parseList(call.result_paths_json);
              return (
                <tr key={call.id}>
                  <td className="candidate__q">{call.query_text ?? <em>redacted</em>}</td>
                  <td>
                    {paths.length === 0 ? (
                      <em className="candidate__nothing">nothing</em>
                    ) : (
                      paths.slice(0, 3).map((path) => (
                        <code key={path} className="candidate__path">
                          {path}
                        </code>
                      ))
                    )}
                    {paths.length > 3 && (
                      <span className="candidate__hint"> +{paths.length - 3}</span>
                    )}
                  </td>
                  <td className="candidate__hint">
                    {call.modality} · {call.session_id ?? "—"}
                  </td>
                  <td className="candidate__hint">{relative(call.started_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {interactions.length > 0 && (
        <>
          <button
            type="button"
            className="candidate__disclose"
            onClick={() => setShowTrace((open) => !open)}
            aria-expanded={showTrace}
          >
            {showTrace ? "▾" : "▸"} Trace — the steps that led here
          </button>
          {showTrace && (
            <>
              <p className="candidate__hint candidate__trace-hint">
                Select a key or the criteria to open the call beside the list.
              </p>
              <ol className="candidate__trace">
                {interactions.map((call) => {
                  const isSelected = selectedCall === call.id;
                  const keyCount = parseList(call.result_ids_json).length;
                  const hasCriteria = Object.keys(parseObject(call.criteria_json)).length > 0;
                  return (
                    <li key={call.id} className={isSelected ? "candidate__trace--on" : undefined}>
                      <span className={`candidate__status candidate__status--${call.status}`}>
                        {call.status}
                      </span>
                      <code>{call.operation}</code>
                      <span className="candidate__hint">
                        {call.duration_ms === null ? "—" : `${call.duration_ms.toFixed(1)}ms`}
                      </span>
                      <span className="candidate__hint">trace</span>
                      <button
                        type="button"
                        className="candidate__key"
                        onClick={() => onSelect(call, null)}
                        aria-pressed={isSelected}
                        title={`trace id ${call.trace_id}`}
                      >
                        {call.trace_id.slice(0, 12)}
                      </button>
                      <span className="candidate__hint">span</span>
                      <button
                        type="button"
                        className="candidate__key"
                        onClick={() => onSelect(call, null)}
                        aria-pressed={isSelected}
                        title={`span id ${call.span_id}`}
                      >
                        {call.span_id.slice(0, 8)}
                      </button>
                      {call.parent_span_id && (
                        <>
                          <span className="candidate__hint">← parent</span>
                          <button
                            type="button"
                            className="candidate__key"
                            onClick={() => onSelect(call, null)}
                            aria-pressed={isSelected}
                            title={`parent span id ${call.parent_span_id}`}
                          >
                            {call.parent_span_id.slice(0, 8)}
                          </button>
                        </>
                      )}
                      {hasCriteria && (
                        <button
                          type="button"
                          className="candidate__key candidate__key--criteria"
                          onClick={() => onSelect(call, null)}
                          aria-pressed={isSelected}
                          title="the criteria this search ran under"
                        >
                          criteria
                        </button>
                      )}
                      {keyCount > 0 && (
                        <button
                          type="button"
                          className="candidate__key candidate__key--criteria"
                          onClick={() => onSelect(call, null)}
                          aria-pressed={isSelected}
                          title="the content-addressed ids this call returned"
                        >
                          {keyCount} key{keyCount === 1 ? "" : "s"}
                        </button>
                      )}
                    </li>
                  );
                })}
                <li className="candidate__trace-end">
                  <span className="candidate__status candidate__status--formed">consolidated</span>
                  <span className="candidate__hint">
                    {candidate.rule_id} proposed “{candidate.text}” from the{" "}
                    {candidate.observations} call{candidate.observations === 1 ? "" : "s"} above,
                    across {candidate.sessions} session
                    {candidate.sessions === 1 ? "" : "s"}.
                  </span>
                </li>
              </ol>
            </>
          )}
        </>
      )}
    </div>
  );
}

export function CandidateReview() {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [opened, setOpened] = useState<Set<string>>(new Set());
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [shown, setShown] = useState<Record<string, number>>({});
  const [selection, setSelection] = useState<Selection | null>(null);

  // The panel is fixed to the right edge, so the page has to give up the width
  // rather than be covered by it — a reviewer comparing a row against the
  // panel needs both. Set on the document because the shell that owns the
  // scrolling column is three components above this one, and threading a
  // boolean up through the page to move a margin is worse than one variable.
  useEffect(() => {
    const root = document.documentElement;
    if (selection) root.style.setProperty("--side-panel", "24rem");
    else root.style.removeProperty("--side-panel");
    return () => {
      root.style.removeProperty("--side-panel");
    };
  }, [selection]);

  const listing = useQuery({
    queryKey: ["memory-candidates"],
    queryFn: () => api.artifactCandidates({ status: "pending" }),
    retry: false,
  });

  const decide = useMutation({
    mutationFn: async ({ ids, promote }: { ids: string[]; promote: boolean }) => {
      // Sequential, not parallel: each promotion writes a memory record
      // through the ordinary path, and a burst of concurrent writes into one
      // memory source buys nothing on a review action a human just clicked.
      for (const id of ids) {
        if (promote) await api.promoteArtifactCandidate(id);
        else await api.rejectArtifactCandidate(id);
      }
      return ids.length;
    },
    onSuccess: (_count, { ids }) => {
      setSelected(new Set());
      // A panel anchored to a proposal that has just been decided is showing
      // evidence for a question nobody is asking any more.
      setSelection((current) =>
        current && ids.includes(current.candidate.id) ? null : current,
      );
      queryClient.invalidateQueries({ queryKey: ["memory-candidates"] });
      queryClient.invalidateQueries({ queryKey: ["artifact-records"] });
      queryClient.invalidateQueries({ queryKey: ["artifacts"] });
    },
  });

  const candidates = listing.data?.candidates ?? [];

  const groups = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const matching = needle
      ? candidates.filter(
          (c) =>
            c.text.toLowerCase().includes(needle) ||
            c.rule_id.toLowerCase().includes(needle) ||
            (c.subject ?? "").toLowerCase().includes(needle),
        )
      : candidates;
    const byRule = new Map<string, MemoryCandidate[]>();
    for (const candidate of matching) {
      const bucket = byRule.get(candidate.rule_id) ?? [];
      // Strongest evidence first: a reviewer working top-down should meet the
      // best-supported proposals before the marginal ones.
      bucket.push(candidate);
      byRule.set(candidate.rule_id, bucket);
    }
    for (const bucket of byRule.values()) {
      bucket.sort(
        (a, b) => b.sessions - a.sessions || b.observations - a.observations ||
          a.text.localeCompare(b.text),
      );
    }
    return [...byRule.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [candidates, filter]);

  if (listing.isError || candidates.length === 0) return null;

  const visible = groups.flatMap(([, bucket]) => bucket).map((c) => c.id);
  const allVisibleSelected =
    visible.length > 0 && visible.every((id) => selected.has(id));

  const toggle = (set: Set<string>, id: string) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  };

  const select = (candidate: MemoryCandidate) => (call: CandidateInteraction, keyId: string | null) =>
    setSelection((current) =>
      // Clicking the already-selected call's key again closes it, so the
      // gesture that opened the panel is the gesture that clears it.
      current && current.candidate.id === candidate.id && current.call.id === call.id && current.keyId === keyId
        ? null
        : { candidate, call, keyId },
    );

  return (
    <section className="section candidates">
      <h2>
        Proposed <span className="candidates__count">{candidates.length}</span>
      </h2>
      <p className="section__hint">
        Patterns this region noticed in how it is used. <strong>These are not
        published artifacts yet</strong> — nothing here is retrievable until you promote
        it. Open one to see the calls it came from. Rejecting is permanent: it
        will not be proposed again.
      </p>

      <div className="candidates__toolbar">
        <input
          className="input"
          type="search"
          placeholder="Filter proposals…"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          aria-label="Filter proposals"
        />
        <label className="candidates__all">
          <input
            type="checkbox"
            checked={allVisibleSelected}
            onChange={() =>
              setSelected(allVisibleSelected ? new Set() : new Set(visible))
            }
          />
          Select all shown
        </label>
        <span className="candidates__selection">
          {selected.size > 0 ? `${selected.size} selected` : ""}
        </span>
        <button
          type="button"
          className="button button--primary"
          disabled={selected.size === 0 || decide.isPending}
          onClick={() => decide.mutate({ ids: [...selected], promote: true })}
        >
          Promote
        </button>
        <button
          type="button"
          className="button"
          disabled={selected.size === 0 || decide.isPending}
          onClick={() => decide.mutate({ ids: [...selected], promote: false })}
        >
          Reject
        </button>
      </div>
      {decide.isError && <p className="error">{String(decide.error)}</p>}

      {groups.map(([rule, bucket]) => {
        const isCollapsed = collapsed.has(rule);
        const limit = shown[rule] ?? PAGE;
        const groupIds = bucket.map((c) => c.id);
        const groupSelected = groupIds.every((id) => selected.has(id));
        return (
          <div className="candidates__group" key={rule}>
            <div className="candidates__group-head">
              <button
                type="button"
                className="candidate__disclose"
                onClick={() => setCollapsed(toggle(collapsed, rule))}
                aria-expanded={!isCollapsed}
              >
                {isCollapsed ? "▸" : "▾"} <code>{rule}</code>
                <span className="candidates__count">{bucket.length}</span>
              </button>
              <button
                type="button"
                className="candidates__link"
                onClick={() =>
                  setSelected((current) => {
                    const next = new Set(current);
                    groupIds.forEach((id) => (groupSelected ? next.delete(id) : next.add(id)));
                    return next;
                  })
                }
              >
                {groupSelected ? "clear group" : "select group"}
              </button>
            </div>

            {!isCollapsed &&
              bucket.slice(0, limit).map((candidate) => {
                const isOpen = opened.has(candidate.id);
                return (
                  <div className="candidate" key={candidate.id}>
                    <div className="candidate__row">
                      <input
                        type="checkbox"
                        checked={selected.has(candidate.id)}
                        onChange={() => setSelected(toggle(selected, candidate.id))}
                        aria-label={`Select ${candidate.text}`}
                      />
                      <button
                        type="button"
                        className="candidate__disclose"
                        onClick={() => setOpened(toggle(opened, candidate.id))}
                        aria-expanded={isOpen}
                        aria-label={isOpen ? "Hide evidence" : "Show evidence"}
                      >
                        {isOpen ? "▾" : "▸"}
                      </button>
                      <span className="candidate__text">{candidate.text}</span>
                      <span className="chip">{candidate.kind}</span>
                      <span className="chip">{candidate.scope}</span>
                      <span className="candidate__strength" title="how strong the evidence is">
                        {candidate.observations}× / {candidate.sessions} session
                        {candidate.sessions === 1 ? "" : "s"}
                      </span>
                      <span className="candidate__hint">{relative(candidate.last_seen)}</span>
                      <span className="candidate__row-actions">
                        <button
                          type="button"
                          className="button button--primary"
                          disabled={decide.isPending}
                          onClick={() =>
                            decide.mutate({ ids: [candidate.id], promote: true })
                          }
                        >
                          Promote
                        </button>
                        <button
                          type="button"
                          className="button"
                          disabled={decide.isPending}
                          onClick={() =>
                            decide.mutate({ ids: [candidate.id], promote: false })
                          }
                        >
                          Reject
                        </button>
                      </span>
                    </div>
                    {isOpen && (
                      <Evidence
                        candidate={candidate}
                        selection={selection}
                        onSelect={select(candidate)}
                      />
                    )}
                  </div>
                );
              })}

            {!isCollapsed && bucket.length > limit && (
              <button
                type="button"
                className="candidates__link"
                onClick={() => setShown({ ...shown, [rule]: limit + PAGE })}
              >
                Show {Math.min(PAGE, bucket.length - limit)} more of {bucket.length}
              </button>
            )}
          </div>
        );
      })}

      {selection && (
        <CriteriaPanel
          selection={selection}
          onFocusKey={(keyId) =>
            setSelection((current) => (current ? { ...current, keyId } : current))
          }
          onClear={() => setSelection(null)}
        />
      )}
    </section>
  );
}
