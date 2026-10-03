# Pheasant-lab top-K retrieval ablation (2026-10-03 UTC)

The deployed lab uses `assistant.retrieval.per_query_results: 10` and
`assistant.retrieval.max_context_passages: 16`. This experiment varied the
per-query limit through the existing per-request `options` field, leaving the
deployed config, corpus, model effort, and context limit unchanged. All runs
used the five labeled `remarkable.zip` questions at four concurrent answers
with a warm query-embedding cache. The cases are development data; repetitions
are not distinct questions.

| Trial | Requests | First answer text p95 | Complete answer p95 | Mean retrieved passages | Input tokens | Automated quality proxy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| K=10, context=16, first control | 10 | 4.82 s | 7.78 s | 8.2 | 76,964 | 10/10 |
| K=6, context=16 | 10 | 4.75 s | 7.45 s | 6.2 | 57,676 | 10/10 |
| K=4, context=16 | 10 | 6.57 s | 9.61 s | 4.4 | 41,798 | 10/10 |
| K=6, context=12 | 10 | 5.22 s | 8.16 s | 6.2 | 57,676 | 10/10 |
| K=10, context=16, second control | 10 | 6.81 s | 9.50 s | 8.2 | 76,964 | 10/10 |

In the ten-repetition comparison, K=6 completed 50/50 answers with
first-answer-text p95 5.24 s and completed-answer p95 8.34 s. K=10 completed
50/50 with first-answer-text p95 8.20 s and completed-answer p95 11.10 s.
K=6 used 288,380 input tokens. K=10 token totals are unknown for the full
run because one provider failure did not return usage; the matched short runs
showed a 25% input-token reduction at K=6.

Both long runs scored 49/50 on the automated fact/citation proxy. The K=6
miss was a data-mesh answer citing one passage outside the labeled acceptable
set; K=10 had the *same retrieved evidence set* for that question, so this
does not establish a K-caused retrieval miss. The K=10 miss was an extractive
fallback after the provider returned HTTP 429. Neither miss counts as a
quality-passing answer. Semantic support has not been manually adjudicated.

The p95 improvement is uncertain. The K=6 run had two 28–31-second provider
calls, and its distinct-question cluster interval for first-text p95 was
3.00–31.60 s; the K=10 interval was 7.08–11.46 s. The five-question set is
too narrow for a release gate, and sequential run order cannot fully remove
provider-load variation. Lowering K=6's context cap from 16 to 12 reduced no
additional tokens in this set. K=4 increased the measured p95, so it is not
a useful candidate from this evidence.

The PostgreSQL lexical query still ranks candidates before applying `LIMIT`;
a smaller K primarily cuts returned evidence, hydration, and model context.
The observed ranking-time differences cannot be attributed to the limit from
these runs. Keep the deployed K=10/context=16 until a larger held-out set
shows K=6 preserves supported answers, particularly multi-document and
procedural ones. A later candidate could use K=6 for short questions and
expand to K=10 when evidence is insufficient, but a model can falsely judge
partial evidence sufficient, so this still needs a quality gate.

The raw reports and per-request manifests are under
`.pytest_artifacts/topk_ablation/`. The deployed setting was not changed.
