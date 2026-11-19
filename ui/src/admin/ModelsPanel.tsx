import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { CatalogModel } from "../api/types";

/**
 * The models this region offers, and what an Admin decides about them.
 *
 * Which models exist is the operator's decision (the deployment's `models`
 * section). An Admin chooses which answering and judgement model is everyone's
 * default (people can still pick another in the composer's model dialog), and
 * which embedding model indexes the region: one at a time, and changing it
 * re-embeds everything already indexed.
 */
export function ModelsPanel() {
  const queryClient = useQueryClient();
  const catalog = useQuery({
    queryKey: ["models"],
    queryFn: api.models,
    // A re-embed in progress is worth watching; otherwise this hardly moves.
    refetchInterval: (query) => (query.state.data?.embedding.state === "reindex_pending" ? 5000 : false),
  });
  const select = useMutation({
    mutationFn: (id: string) => api.selectEmbedding(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["models"] }),
  });

  if (catalog.isLoading) return <p className="muted small">Loading…</p>;
  if (catalog.isError)
    return <div className="banner banner--error">{(catalog.error as Error).message}</div>;
  const data = catalog.data!;
  const pending = data.embedding.state === "reindex_pending";

  return (
    <>
      <section className="section">
        <h2 className="section__title">Embedding model</h2>
        <p className="muted small">
          One embedding model indexes and searches the whole knowledge base. Switching re-embeds
          every passage already indexed (no source is read again), which takes time and costs one
          embedding call per passage. Until it finishes, semantic search is partial and answers
          lean on text and graph search.
        </p>
        {pending ? (
          <div className="banner banner--warn">
            Re-embedding with <strong>{data.embedding.active}</strong> is in progress.
          </div>
        ) : null}
        {select.isError ? (
          <div className="banner banner--error">{(select.error as Error).message}</div>
        ) : null}
        {data.embeddings.length ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Model</th>
                  <th>Vector width</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.embeddings.map((model) => (
                  <tr key={model.id}>
                    <td>
                      {model.label}
                      {model.description ? (
                        <div className="muted small">{model.description}</div>
                      ) : null}
                    </td>
                    <td>{model.dimensions ?? "native"}</td>
                    <td>
                      {model.active ? (
                        <span className="pill pill--accent">
                          {pending ? "in force · re-embedding" : "in force"}
                        </span>
                      ) : !model.available ? (
                        <span className="pill pill--warn">no credential here</span>
                      ) : null}
                    </td>
                    <td className="admin-actions">
                      {!model.active && data.embedding.can_change ? (
                        <button
                          className="btn btn--small"
                          disabled={select.isPending || pending || !model.available}
                          onClick={() => {
                            if (
                              window.confirm(
                                `Switch the embedding model to ${model.label}? Every indexed passage will be re-embedded.`,
                              )
                            )
                              select.mutate(model.id);
                          }}
                        >
                          Switch to this
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted small">
            This region indexes with one configured model
            {data.embedding.model ? ` (${data.embedding.model})` : ""}; its deployment lists no
            others to switch to.
          </p>
        )}
      </section>

      <ModelList
        title="Answering models"
        note="Write the answers. The default answers everyone unless they choose another in the composer's model dialog."
        kind="chat"
        models={data.chat}
        selectable={data.selectable}
        current={data.default_chat}
        provisioned={data.deployment_defaults?.chat ?? null}
        canSet={Boolean(data.can_set_defaults)}
      />
      <ModelList
        title="Judgement models"
        note="Grade whether the evidence is enough, whether an answer is supported, and pick the answering model in Automatic."
        kind="decision"
        models={data.decisions}
        selectable={data.selectable}
        current={data.default_decision}
        provisioned={data.deployment_defaults?.decision ?? null}
        canSet={Boolean(data.can_set_defaults)}
      />
    </>
  );
}

function ModelList({
  title,
  note,
  kind,
  models,
  selectable,
  current,
  provisioned,
  canSet,
}: {
  title: string;
  note: string;
  kind: "chat" | "decision";
  models: CatalogModel[];
  selectable: boolean;
  current: string | null;
  provisioned: string | null;
  canSet: boolean;
}) {
  const queryClient = useQueryClient();
  const choose = useMutation({
    mutationFn: (id: string | null) => api.setDefaultModels({ [kind]: id }),
    onSuccess: (data) => queryClient.setQueryData(["models"], data),
  });
  if (!models.length) return null;
  const label = (id: string | null) => models.find((model) => model.id === id)?.label ?? id;
  const changed = Boolean(provisioned && current && current !== provisioned);
  return (
    <section className="section">
      <h2 className="section__title">{title}</h2>
      <p className="muted small">
        {note} {selectable ? "" : "Per-question choice is off: everyone gets the default."} Adding or
        removing a model is the operator's change to the deployment.
      </p>
      {changed ? (
        <div className="banner banner--info admin-default-note">
          <span>
            <strong>{label(current)}</strong> is the default, chosen here; the deployment provisioned{" "}
            <strong>{label(provisioned)}</strong>.
          </span>
          {canSet ? (
            <button
              className="btn btn--small"
              disabled={choose.isPending}
              onClick={() => choose.mutate(null)}
            >
              Restore deployment default
            </button>
          ) : null}
        </div>
      ) : null}
      {choose.isError ? (
        <div className="banner banner--error">{(choose.error as Error).message}</div>
      ) : null}
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Model</th>
              <th>Maker</th>
              <th>Status</th>
              {canSet ? <th /> : null}
            </tr>
          </thead>
          <tbody>
            {models.map((model) => (
              <tr key={model.id}>
                <td>
                  {model.label}
                  {model.description ? <div className="muted small">{model.description}</div> : null}
                </td>
                <td>{model.family ?? ""}</td>
                <td>
                  {model.default ? <span className="pill pill--accent">default</span> : null}
                  {!model.available ? <span className="pill pill--warn">no credential here</span> : null}
                </td>
                {canSet ? (
                  <td className="admin-actions">
                    {!model.default ? (
                      <button
                        className="btn btn--small"
                        disabled={choose.isPending || !model.available}
                        title={
                          model.available
                            ? `Answer with ${model.label} by default, for everyone in this knowledge base`
                            : "This process holds no credential for it"
                        }
                        onClick={() => choose.mutate(model.id)}
                      >
                        Make default
                      </button>
                    ) : null}
                  </td>
                ) : null}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
