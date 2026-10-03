# Pheasant-lab answer streaming and latency check (2026-10-03 UTC)

The lab now sends provisional answer text as `draft` SSE events while Luna is
generating. The client replaces that text with the verified `answer` event,
including citations. The first progress event is tracked separately; it is not
time to first answer text. HTTP and MCP still return the same completed-answer
behavior through the shared assistant service.

## Reproduce

The case manifest is `benchmarks/pheasant-lab-remarkable-20261002.json`.
Questions, expected facts, and reports are outside indexed sources. Set
`PHEASANT_API_TOKEN` in the environment, then run:

```powershell
python scripts/benchmark_assistant_latency.py --base-url http://127.0.0.1:8765 --token-env PHEASANT_API_TOKEN --cases benchmarks/pheasant-lab-remarkable-20261002.json --concurrency 4 --repeats 2 --transport sse --output-dir .pytest_artifacts/assistant-latency
```

The five current questions are from the indexed `remarkable.zip` corpus: four
moderate and one complex. The two repetitions are not independent questions.
The warm runs use query-embedding cache hits; cold runs include five fresh
embedding misses and five repeated-query hits. Each report records corpus and
effective-config fingerprints. The corpus was unchanged within every compared
run. This is a development sample, not the planned 120-question held-out set.

| Configuration, four concurrent | First answer text p95 | Complete answer p95 | Completed | Automated fact/citation proxy |
| --- | ---: | ---: | ---: | ---: |
| Original, no short deadline, no answer streaming | unavailable | 7.23 s | 10/10 | not comparable; labels corrected later |
| Final candidate, 1 CPU, mixed fresh/hit | 7.47 s | 8.11 s | 10/10 | 10/10 |
| Final candidate, 1 CPU, warm | 5.21 s | 6.86 s | 10/10 | 10/10 |
| Final candidate, 2 CPU, mixed fresh/hit | 9.04 s | 9.53 s | 10/10 | 10/10 |
| Final candidate, 2 CPU, warm | 6.15 s | 7.09 s | 10/10 | 10/10 |

The one-CPU limit was retained. This small, noisy sample does not prove a
completed-answer speedup over the original. Streaming changes the experience:
answer text can appear before the final answer and citations arrive. Its draft
is provisional and must not be counted as a verified complete answer.

For the ten-request baseline, reported token use was 71,710 input and 2,282
output tokens. The ten-request one-CPU warm candidate used 76,964 input,
2,247 output, and 1,111 reasoning tokens; 76,934 input tokens were reported
cached by the provider. USD cost is unknown because the benchmark does not
assume a price schedule. These counts include all actual provider calls in
each run and should not be mistaken for a model-effort-normalized cost trial.

In the one-CPU warm run, PostgreSQL lexical ranking took a median 1.07 s per
request (maximum 1.57 s), evidence hydration 0.17 s median, and the Luna
provider call 1.63 s median (maximum 5.13 s). These stage measurements explain
why first text remains in seconds; parallel durations are not summed as if they
were elapsed time. A prior exact-rank SQL rewrite and two-CPU API allowance did
not improve measured p95 and were discarded. Explicit low reasoning reduced
some provider latency, but a warm run lowered the quality proxy to 0.7, so the
lab retains Luna's medium default. No short or medium assistant deadline is
configured; the provider timeout remains 180 seconds.

At eight concurrent SSE requests, four completed and six received the expected
admission rejection (HTTP 429); no request timed out. Five sequential MCP
requests and ten HTTP requests completed with a 1.0 automated quality proxy.
The post-rollout SSE smoke check completed 5/5 with answer text emitted on
every request. Semantic citation support has not been manually adjudicated;
the five-case proxy cannot establish the full quality promotion gate.

**Target verdict:** moderate p95 completed answer <=800 ms: unmet; complex
p95 <=3 s: unmet; difficult multi-hop <=10 s: unmeasured in this case set;
long reports/visuals: unmeasured. First-answer-text p95 at four concurrent
requests is 5.21 s warm and 7.47 s with fresh embedding work, so no
subsecond first-text claim is made.

The deployed image is `pheasant:lab-streaming-final-20261002`
(`sha256:4697370806c5d9b57282a787e8c7e30cc2f664fed322a47add5d29714584f3f2`).
The generated `pheasant.yaml` comes from
`deploy/compose/answers/pheasant-lab.json`; API, graph, indexer, logger, and
four workers use the same versioned image. PostgreSQL, NATS, LanceDB, source
registrations, and named volumes were retained. The previous image
`pheasant:lab-source-lifecycle-v2-20261002` remains available for rollback.

Validation: 187 focused host tests passed (one skipped), including streaming,
provider failure, source upload, sync idempotency, and configuration freshness;
one Windows named-pipe process-executor test was deselected because the host
sandbox denies pipe creation. Ruff lint and format checks and `mkdocs build
--strict` passed. A broader host run reached 275 passes before a Windows
file-replacement permission failure. Host MCP-specific tests cannot run in
this virtualenv because it has MCP SDK 1.x while this repository requires
2.x; the built container served five successful MCP answer requests.

The raw summaries are in `.pytest_artifacts/perf_no_deadline_baseline_c4/`,
`.pytest_artifacts/perf_final_cpu1_cold/`,
`.pytest_artifacts/perf_final_cpu1_warm/`,
`.pytest_artifacts/perf_final_cpu2_cold/`,
`.pytest_artifacts/perf_final_cpu2_warm/`,
`.pytest_artifacts/perf_final_http/`,
`.pytest_artifacts/perf_final_mcp/`, and
`.pytest_artifacts/perf_final_overload/`.
