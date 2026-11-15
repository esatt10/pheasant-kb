import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "../api/client";
import type { CapacityRequest, FleetReport } from "../api/admin";
import { CopyLine } from "../components/CopyLine";
import { bytes, compact, money } from "./UsagePanel";

/**
 * The knowledge bases of this fleet: create one in a free slot, ask the
 * operator for more, and (for the operator) see every one's usage and answer
 * requests. Creating claims capacity that already exists; nothing on this page
 * provisions anything, which is why "no slot is free" is a request, not a
 * button that spends somebody's money.
 */
export function FleetPanel() {
  const queryClient = useQueryClient();
  const fleet = useQuery({ queryKey: ["fleet"], queryFn: api.fleet, retry: false });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["fleet"] });

  if (fleet.isLoading) return <p className="muted small">Loading…</p>;
  if (fleet.isError) {
    const error = fleet.error as Error;
    const off = error instanceof ApiError && error.status === 404;
    return (
      <div className={off ? "banner banner--info" : "banner banner--error"}>
        {off
          ? "This region is one knowledge base on its own. Its operator can provision several side by side (a fleet), each with its own sources and people."
          : error.message}
      </div>
    );
  }
  const data = fleet.data!;
  const { capacity } = data;

  return (
    <>
      <div className="stat-row admin-stats">
        <div className="stat">
          <span className="stat__value">{capacity.claimed}</span>
          <span className="stat__label">Knowledge bases</span>
        </div>
        <div className="stat">
          <span className="stat__value">{capacity.free}</span>
          <span className="stat__label">Free slots</span>
          <span className="muted small">of {capacity.slots} the operator provisioned</span>
        </div>
      </div>

      <section className="section">
        <h2 className="section__title">New knowledge base</h2>
        {data.can_create ? (
          <CreateForm onDone={refresh} />
        ) : (
          <div className="banner banner--warn">
            Every provisioned slot is in use. Ask the operator for more below; adding one is a
            deployment, and theirs to decide.
          </div>
        )}
      </section>

      <section className="section">
        <h2 className="section__title">Knowledge bases</h2>
        <KnowledgeBases data={data} />
      </section>

      <section className="section">
        <h2 className="section__title">Capacity requests</h2>
        <p className="muted small">
          A request is a record of an ask and its answer. It provisions nothing by itself.
        </p>
        <RequestForm onDone={refresh} />
        <Requests requests={data.requests} canResolve={data.can_resolve} onDone={refresh} />
      </section>
    </>
  );
}

function CreateForm({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [admins, setAdmins] = useState("");
  const create = useMutation({
    mutationFn: () =>
      api.createKnowledgeBase({
        name: name.trim(),
        description: description.trim(),
        admins: admins.split(/[\s,]+/).filter(Boolean),
      }),
    onSuccess: () => {
      setName("");
      setDescription("");
      setAdmins("");
      onDone();
    },
  });
  return (
    <form
      className="admin-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (name.trim()) create.mutate();
      }}
    >
      <label className="field">
        <span>Name</span>
        <input className="text-input" value={name} onChange={(event) => setName(event.target.value)} />
      </label>
      <label className="field">
        <span>Description</span>
        <input
          className="text-input"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
      </label>
      <label className="field">
        <span>Other admins (sign-in names)</span>
        <input
          className="text-input"
          placeholder="colleague@organization.com"
          value={admins}
          onChange={(event) => setAdmins(event.target.value)}
        />
      </label>
      <button className="btn btn--primary" disabled={!name.trim() || create.isPending}>
        {create.isPending ? "Creating…" : "Create knowledge base"}
      </button>
      <p className="muted small admin-form__wide">
        You become its Admin. It starts empty, with its own sources, Contributors and Readers.
      </p>
      {create.isError ? (
        <div className="banner banner--error admin-form__wide">{(create.error as Error).message}</div>
      ) : null}
      {create.isSuccess ? (
        <div className="admin-form__wide">
          <div className="banner">
            Created <strong>{create.data.knowledge_base.name}</strong>. Its first page takes about a
            minute to open: a knowledge base nobody is using is asleep.
          </div>
          {create.data.sign_in_url ? <CopyLine command={create.data.sign_in_url} /> : null}
        </div>
      ) : null}
    </form>
  );
}

function KnowledgeBases({ data }: { data: FleetReport }) {
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>Knowledge base</th>
            <th>Admins</th>
            <th>Size</th>
            {data.operator ? (
              <>
                <th>Questions (30 d)</th>
                <th>People</th>
                <th>Tokens</th>
                <th>Cost</th>
                <th>Answer p95</th>
              </>
            ) : null}
          </tr>
        </thead>
        <tbody>
          {data.knowledge_bases.map((kb) => (
            <tr key={kb.slot} className={kb.status === "free" ? "row--disabled" : undefined}>
              <td>
                {kb.status === "free" ? (
                  <span className="muted">Free slot</span>
                ) : kb.url && !kb.current ? (
                  <a href={kb.url}>{kb.name}</a>
                ) : (
                  kb.name
                )}
                {kb.current ? <span className="pill pill--accent admin-pill">this one</span> : null}
                <div className="muted small">
                  {kb.slot}
                  {kb.description ? ` · ${kb.description}` : ""}
                </div>
              </td>
              <td className="small">{kb.admins.map((admin) => admin.email).join(", ") || "—"}</td>
              <td>
                {bytes(kb.size.indexed_bytes)}
                {kb.size.limit_gb ? (
                  <div className="muted small">
                    {Math.round((kb.size.limit_used ?? 0) * 100)}% of {kb.size.limit_gb} GB
                  </div>
                ) : null}
              </td>
              {data.operator ? (
                <>
                  <td>{kb.usage ? compact(kb.usage.questions) : "—"}</td>
                  <td>{kb.usage ? compact(kb.usage.active_people) : "—"}</td>
                  <td>{kb.usage ? compact(kb.usage.tokens) : "—"}</td>
                  <td>{kb.usage ? money(kb.usage.cost, data.currency) : "—"}</td>
                  <td>
                    {kb.usage?.answer_p95_ms ? `≤ ${kb.usage.answer_p95_ms / 1000} s` : "—"}
                  </td>
                </>
              ) : null}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RequestForm({ onDone }: { onDone: () => void }) {
  const [kind, setKind] = useState("slots");
  const [quantity, setQuantity] = useState(1);
  const [reason, setReason] = useState("");
  const file = useMutation({
    mutationFn: () => api.requestCapacity({ kind, quantity, reason: reason.trim() }),
    onSuccess: () => {
      setReason("");
      onDone();
    },
  });
  return (
    <form
      className="admin-form"
      onSubmit={(event) => {
        event.preventDefault();
        file.mutate();
      }}
    >
      <label className="field">
        <span>Ask for</span>
        <select className="text-input" value={kind} onChange={(event) => setKind(event.target.value)}>
          <option value="slots">More knowledge bases</option>
          <option value="storage">More room in this one (GB)</option>
        </select>
      </label>
      <label className="field">
        <span>How many</span>
        <input
          className="text-input"
          type="number"
          min={1}
          max={500}
          value={quantity}
          onChange={(event) => setQuantity(Number(event.target.value) || 1)}
        />
      </label>
      <label className="field">
        <span>Why</span>
        <input className="text-input" value={reason} onChange={(event) => setReason(event.target.value)} />
      </label>
      <button className="btn" disabled={file.isPending}>
        {file.isPending ? "Sending…" : "Send request to the operator"}
      </button>
      {file.isError ? (
        <div className="banner banner--error admin-form__wide">{(file.error as Error).message}</div>
      ) : null}
    </form>
  );
}

function Requests({
  requests,
  canResolve,
  onDone,
}: {
  requests: CapacityRequest[];
  canResolve: boolean;
  onDone: () => void;
}) {
  const resolve = useMutation({
    mutationFn: (input: { id: string; status: string; note: string }) =>
      api.resolveCapacityRequest(input.id, { status: input.status, note: input.note }),
    onSuccess: onDone,
  });
  if (!requests.length) return <p className="muted small">No requests.</p>;
  const answer = (id: string, status: string) => {
    const note = window.prompt(`Note for the requester (${status}):`, "");
    if (note !== null) resolve.mutate({ id, status, note });
  };
  return (
    <>
      {resolve.isError ? (
        <div className="banner banner--error">{(resolve.error as Error).message}</div>
      ) : null}
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Asked</th>
              <th>By</th>
              <th>Why</th>
              <th>Answer</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {requests.map((request) => (
              <tr key={request.id}>
                <td>
                  {request.quantity} {request.kind === "slots" ? "knowledge base(s)" : "GB"}
                  <div className="muted small">{request.created_at.slice(0, 10)}</div>
                </td>
                <td className="small">
                  {request.requested_by}
                  <div className="muted small">{request.slot}</div>
                </td>
                <td className="small">{request.reason}</td>
                <td>
                  <span className={request.status === "open" ? "pill pill--warn" : "pill"}>
                    {request.status}
                  </span>
                  {request.note ? <div className="muted small">{request.note}</div> : null}
                </td>
                <td className="admin-actions">
                  {canResolve && request.status !== "fulfilled" ? (
                    <>
                      {request.status === "open" ? (
                        <>
                          <button className="btn btn--small" onClick={() => answer(request.id, "approved")}>
                            Approve
                          </button>
                          <button className="btn btn--small" onClick={() => answer(request.id, "declined")}>
                            Decline
                          </button>
                        </>
                      ) : null}
                      {request.status !== "declined" ? (
                        <button className="btn btn--small" onClick={() => answer(request.id, "fulfilled")}>
                          Mark provisioned
                        </button>
                      ) : null}
                    </>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
