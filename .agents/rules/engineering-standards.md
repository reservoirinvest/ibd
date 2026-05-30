# Engineering Standards & Agent Guardrails

## Code Quality & Modern Patterns
* **Python Conventions:** Adhere strictly to PEP 8 styling. Use explicit type hinting throughout.
* **Asynchronous Design:** Leverage modern `asyncio` design patterns. Avoid blocking calls in async loops; use thread pools (`run_in_executor`) for blocking I/O bound tasks if necessary.

## Frontend & UI Architecture
* **Progressive Enhancement:** UI components must serve Semantic HTML5 first. JavaScript functionality must layer on top seamlessly, ensuring the interface degrades gracefully if JS fails or is disabled.
* **Vector Graphics:** All UI iconography and graphics must use hand-optimized, lightweight SVGs embedded inline or cached efficiently. No heavy raster assets for UI elements.

## Data Ingestion & Network Pacing
* **Rate Limiting:** Data gathering pipelines must dynamically throttle outgoing requests to respect external API pacing configurations.
* **Hybrid Fallbacks:** Always prioritize high-throughput, free bulk sources for initial data passes, utilizing premium or restrictive APIs (like IBKR) solely as a targeted fallback layer.

## Terminal & User Observability
* **Console Noise Control:** Running `uv run ibd` must yield an explicit, clean interface. Remove all conversational or raw stream dumps from the default output.
* **Visual Progress Tracking:** Consolidate data loops into deterministic progress bars tracking completion percentages, remaining items, and processing speeds.