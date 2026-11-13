# Azure telemetry bootstrap

This module adds workspace-backed Application Insights, an environment-level
managed OpenTelemetry destination for traces/logs, and Log Analytics diagnostic
metrics for every supplied Container App. Pass the existing environment and
workspace IDs, plus a map of app names to IDs (include PostgreSQL and NATS).
Apply this module **before recreating application replicas** so ACA injects
`OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`.

```hcl
module "observability" {
  source              = "./azure-observability"
  name                = "appi-pheasant"
  location            = azurerm_resource_group.pheasant.location
  resource_group_name = azurerm_resource_group.pheasant.name
  workspace_id        = azurerm_log_analytics_workspace.pheasant.id
  environment_id      = azurerm_container_app_environment.pheasant.id
  container_app_ids   = { for name, app in azurerm_container_app.tier : name => app.id }
}
```

Use the universal image with the `otel` extra. Give each Pheasant container
`OTEL_SERVICE_NAME` equal to its app name. Enable `observability.ui_events` in
setup answers for first-party navigation/action spans. The local ledger and
external export are independent. No endpoint means no exporter or network work.
UI telemetry uses the caller's existing authentication and `knowledge.use`
permission, including Reader. Only fixed route/action identifiers are accepted;
form values, DOM text, credentials and URL query strings are excluded.

The module uses ARM JSON Merge PATCH rather than PUT to preserve existing logs,
networking, workload profiles and identities. The connection string is write-only
in the patch resource; protect Terraform state because the Application Insights
resource itself also contains it. No state or credentials belong in the ZIP.

Keep the environment's `logs_destination = "log-analytics"` and existing
workspace association. Do not add duplicate console/system diagnostic routes.
Set Application Insights local authentication on: the managed agent requires it.
No public application ingress or application permission changes are needed.
Removing this module stops managing the agent configuration; explicitly patch
its destinations to empty lists when intentionally disabling it.

## Verify ingestion

In the **Log Analytics workspace**, use the workspace table names:

```kusto
union isfuzzy=true withsource=TableName
  AppRequests, AppDependencies, AppTraces, AppMetrics,
  ContainerAppConsoleLogs_CL, ContainerAppSystemLogs_CL, AzureMetrics
| where TimeGenerated > ago(30m)
| summarize Rows=count(), Latest=max(TimeGenerated) by TableName
```

```kusto
union AppRequests, AppDependencies
| where TimeGenerated > ago(30m)
| project TimeGenerated, AppRoleName, Name, OperationId, ParentId, Id
| order by TimeGenerated desc
```

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(30m)
| summarize Rows=count(), Latest=max(TimeGenerated) by ContainerAppName_s
```

```kusto
AzureMetrics
| where TimeGenerated > ago(30m)
| summarize Samples=count(), Latest=max(TimeGenerated) by ResourceId, MetricName
```

Application Insights' own query editor calls the equivalent tables `requests`,
`dependencies`, and `traces`; workspace queries use `AppRequests`,
`AppDependencies`, and `AppTraces`. `AzureDiagnostics` is not the application
trace table. A single `OperationId` should cover API/MCP requests, sync/indexer
stages, gRPC worker calls, and PostgreSQL/NATS dependency spans. Console sync JSON
uses that same trace ID and preserves span IDs during export.

The stock PostgreSQL and NATS servers expose console logs/platform metrics;
Pheasant emits their correlated client dependency spans. The managed agent's
Application Insights destination accepts traces and logs, **not OTLP metrics**.
`AzureMetrics` holds ACA platform metrics through the diagnostic settings.
Pheasant's existing authenticated Prometheus `/metrics` surface remains available
for application counters and histograms; this module does not scrape it.
An app scaled to zero has no new workload telemetry until it processes work.
Allow several minutes for initial Azure ingestion.

References: [ACA managed OpenTelemetry agent](https://learn.microsoft.com/en-us/azure/container-apps/opentelemetry-agents),
[ACA log tables](https://learn.microsoft.com/en-us/azure/container-apps/log-monitoring),
[ARM JSON Merge PATCH](https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/managed-environments/update?view=rest-resource-manager-containerapps-2026-07-01).
