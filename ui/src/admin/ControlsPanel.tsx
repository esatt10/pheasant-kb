import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { RegionControls, RegionControlsChange } from "../api/admin";

const money = (amount: number | null, currency: string | null) => {
  if (amount == null) return "—";
  const code = currency || "USD";
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: code }).format(amount);
  } catch {
    return `${amount.toFixed(2)} ${code}`;
  }
};

/**
 * The operator's two controls over this knowledge base: a kill switch that
 * cuts everyone's access but an operator admin's, and a monthly limit on what
 * it spends on models. An Admin sees them; only an operator admin (one the
 * deployment pinned, or the shared token) changes them, and the server says
 * which this caller is (`can_change`).
 */
export function ControlsPanel() {
  const queryClient = useQueryClient();
  const controls = useQuery({
    queryKey: ["region-controls"],
    queryFn: api.regionControls,
    refetchInterval: 30_000,
  });
  const change = useMutation({
    mutationFn: (body: RegionControlsChange) => api.setRegionControls(body),
    onSuccess: (data) => queryClient.setQueryData(["region-controls"], data),
  });

  if (controls.isLoading) return <p className="muted small">Loading…</p>;
  if (controls.isError)
    return <div className="banner banner--error">{(controls.error as Error).message}</div>;
  const data = controls.data!;
  return (
    <>
      {!data.can_change ? (
        <div className="banner banner--info" role="note">
          Only an operator admin (an Admin set by the deployment) can change these. You can see
          whether access is suspended and how much of this month&apos;s limit is used.
        </div>
      ) : null}
      {change.isError ? (
        <div className="banner banner--error">{(change.error as Error).message}</div>
      ) : null}
      <KillSwitch data={data} busy={change.isPending} onChange={(body) => change.mutate(body)} />
      <SpendLimit data={data} busy={change.isPending} onChange={(body) => change.mutate(body)} />
    </>
  );
}

function KillSwitch({
  data,
  busy,
  onChange,
}: {
  data: RegionControls;
  busy: boolean;
  onChange: (body: RegionControlsChange) => void;
}) {
  const [reason, setReason] = useState("");
  const { suspended } = data.access;
  return (
    <section className="section controls-card" aria-labelledby="controls-access">
      <div className="controls-card__head">
        <h2 className="section__title" id="controls-access">
          Access
        </h2>
        <span className={`pill ${suspended ? "pill--warn" : "pill--accent"}`}>
          {suspended ? "Suspended" : "Open"}
        </span>
      </div>
      <p className="muted small">
        The kill switch cuts every person, agent and MCP connection off from this knowledge base at
        once (within seconds), except operator admins, who can turn it back on. Indexing and saved
        content are untouched. To remove one person instead, use the Access tab.
      </p>
      {suspended ? (
        <div className="banner banner--warn" role="status">
          Access is suspended
          {data.access.since ? ` since ${new Date(data.access.since).toLocaleString()}` : ""}
          {data.access.by ? ` by ${data.access.by}` : ""}.
          {data.access.reason ? ` Reason shown to everyone: “${data.access.reason}”.` : ""}
        </div>
      ) : null}
      {data.can_change ? (
        suspended ? (
          <button
            type="button"
            className="btn btn--primary"
            disabled={busy}
            onClick={() => onChange({ suspended: false })}
          >
            Restore access
          </button>
        ) : (
          <form
            className="controls-card__form"
            onSubmit={(event) => {
              event.preventDefault();
              if (
                window.confirm(
                  "Suspend access for everyone except operator admins? Open chats, agents and MCP connections stop working at once.",
                )
              )
                onChange({ suspended: true, reason: reason.trim() || null });
            }}
          >
            <label className="field">
              <span>Reason (optional, shown to everyone refused)</span>
              <input
                className="text-input"
                value={reason}
                maxLength={280}
                placeholder="e.g. Billing review, back on Monday"
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
            <button type="submit" className="btn btn--danger" disabled={busy}>
              Suspend access
            </button>
          </form>
        )
      ) : null}
    </section>
  );
}

function SpendLimit({
  data,
  busy,
  onChange,
}: {
  data: RegionControls;
  busy: boolean;
  onChange: (body: RegionControlsChange) => void;
}) {
  const spend = data.spend;
  const [draft, setDraft] = useState(spend.limit != null ? String(spend.limit) : "");
  useEffect(() => {
    setDraft(spend.limit != null ? String(spend.limit) : "");
  }, [spend.limit]);
  const percent = Math.min(100, Math.max(0, spend.percent_used ?? 0));
  const parsed = Number(draft);
  const valid = draft.trim() !== "" && Number.isFinite(parsed) && parsed >= 0;
  return (
    <section className="section controls-card" aria-labelledby="controls-spend">
      <div className="controls-card__head">
        <h2 className="section__title" id="controls-spend">
          Monthly model spend
        </h2>
        {spend.reached ? (
          <span className="pill pill--warn">Limit reached</span>
        ) : spend.enforced ? (
          <span className="pill">Within limit</span>
        ) : (
          <span className="pill">Not enforced</span>
        )}
      </div>
      <p className="muted small">
        Answering, judgement and embedding calls this calendar month (UTC), at the prices the
        deployment states. At the limit, answers and new embeddings stop until{" "}
        {new Date(`${spend.resets_on}T00:00:00Z`).toLocaleDateString(undefined, {
          month: "long",
          day: "numeric",
          timeZone: "UTC",
        })}{" "}
        or until an operator admin raises it. Text search, browsing and reading keep working.
      </p>
      {spend.note ? <div className="banner banner--info">{spend.note}</div> : null}
      <div
        className={`controls-meter${spend.reached ? " controls-meter--reached" : ""}`}
        role="meter"
        aria-label="Share of this month's spend limit used"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(percent)}
      >
        <span style={{ width: `${percent}%` }} />
      </div>
      <dl className="controls-figures">
        <div>
          <dt>Spent in {spend.month}</dt>
          <dd>{spend.spent != null ? money(spend.spent, spend.currency) : `${percent.toFixed(1)}%`}</dd>
        </div>
        <div>
          <dt>Limit</dt>
          <dd>
            {spend.limit != null ? money(spend.limit, spend.currency) : data.can_change ? "None" : "—"}
            {data.can_change && !spend.custom_limit ? (
              <span className="muted small"> (deployment default)</span>
            ) : null}
          </dd>
        </div>
        <div>
          <dt>Remaining</dt>
          <dd>{money(spend.remaining, spend.currency)}</dd>
        </div>
      </dl>
      {spend.unpriced_models.length ? (
        <p className="muted small">
          Not counted (no price stated): {spend.unpriced_models.join(", ")}.
        </p>
      ) : null}
      {data.can_change ? (
        <form
          className="controls-card__form"
          onSubmit={(event) => {
            event.preventDefault();
            if (valid) onChange({ monthly_spend_limit: parsed });
          }}
        >
          <label className="field">
            <span>Monthly limit ({spend.currency || "USD"})</span>
            <input
              className="text-input"
              type="number"
              inputMode="decimal"
              min={0}
              step="any"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
            />
          </label>
          <div className="controls-card__actions">
            <button
              type="submit"
              className="btn btn--primary"
              disabled={busy || !valid || parsed === spend.limit}
            >
              Save limit
            </button>
            {spend.custom_limit ? (
              <button
                type="button"
                className="btn"
                disabled={busy}
                onClick={() => onChange({ monthly_spend_limit: null })}
                title={
                  spend.default_limit != null
                    ? `Back to ${money(spend.default_limit, spend.currency)}`
                    : "Back to the deployment's setting"
                }
              >
                Use the default
              </button>
            ) : null}
          </div>
        </form>
      ) : null}
    </section>
  );
}
