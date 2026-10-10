# Language choice

Status: accepted for the first implementation. Date: 10 October 2026.

## Context

The baseline consumes one-minute bars for two symbols and executes through a
remote broker. It needs deterministic research, clear tests and robust recovery.
No measured CPU bottleneck exists yet.

## Decision

Use Python for the strategy and application. Keep broker integration separate
from decision logic. Monetary domain values use Decimal; array analytics can
use NumPy. Do not introduce a second C++ implementation of strategy rules.

## Alternatives and consequences

C++ offers predictable native execution and more direct control over memory,
but introduces compilation, ownership and integration work. These costs do not
address a demonstrated problem in a two-symbol minute-bar system. Python makes
research and API integration easier, while requiring deliberate validation and
attention to CPU-bound workloads.

C++ could become useful for large tick-data replay, specialised numerical
kernels or a separate low-latency feed handler. Profile representative inputs
first, try algorithmic and array-based improvements, then consider a narrow
native component with a stable interface. Measure end-to-end improvement,
including conversion and call overhead, and test against reference results.

A native kernel does not remove remote API latency, change data-feed coverage,
or turn the application into a high-frequency trading platform. The hypothesis
that Python is sufficient will be checked with measurements when workload grows.
