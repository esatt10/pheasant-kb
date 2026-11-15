import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { CatalogModel } from "../api/types";

/**
 * The models this region offers, and the one switch an Admin has over them.
 *
 * Which models exist is the operator's decision (the deployment's `models`
 * section); people pick the answering model in the chat composer. What is
 * decided here is the embedding model: one is in force for the whole region,
 * and changing it re-embeds everything already indexed.
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
        note="People choose one of these in the chat composer for their session."
        models={data.chat}
        selectable={data.selectable}
      />
      <ModelList
        title="Judgement models"
        note="Grade whether the evidence is enough and whether an answer is supported."
        models={data.decisions}
        selectable={data.selectable}
      />
    </>
  );
}

function ModelList({
  title,
  note,
  models,
  selectable,
}: {
  title: string;
  note: string;
  models: CatalogModel[];
  selectable: boolean;
}) {
  if (!models.length) return null;
  return (
    <section className="section">
      <h2 className="section__title">{title}</h2>
      <p className="muted small">
        {note} {selectable ? "" : "Per-question choice is off: everyone gets the default."} Adding or
        removing a model is the operator's change to the deployment.
      </p>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Model</th>
              <th>Maker</th>
              <th>Status</th>
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
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
