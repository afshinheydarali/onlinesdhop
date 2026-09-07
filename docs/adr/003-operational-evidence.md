# ADR 003: operational evidence stays local and reproducible

## Decision

Expose metadata only through structured request logs, bounded in process metrics, and dependency aware readiness. Keep synthetic seed/reset, backup, restore, demo, and benchmark commands local and restricted to explicitly named `_test` databases.

## Rationale

Portfolio evidence should be inspectable without leaking customer data or requiring production credentials. A measured benchmark artifact records its machine, runtime, dataset, request mix, concurrency, latency percentiles, throughput, and errors; it does not claim production capacity or pretend to have query counts that were not instrumented.

## Consequences

The HTTP counters reset with the process and are suitable for local diagnostics. Production deployments should export equivalent metrics to the approved monitoring system. Restore tests use a separate database so the source fixture remains independently verifiable.
