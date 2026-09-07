# Workspace tool paths

- GitHub CLI is installed at `C:\Program Files\GitHub CLI\gh.exe`.
- `gh.exe` is not necessarily present on `PATH`; always invoke it using the absolute path above.

# Workflow boundaries

- Cover preflight and novel scraping must remain independent workflows.
- `.github/workflows/cover-preflight.yml` must not run `catalog_parser.py`, `crawler.py`, or fetch a novel source website.
- In cover preflight, `catalog_url` may only be used to validate book identity and derive stable storage keys. It must not trigger an HTTP request.
- Cover preflight must build its minimal configuration from dispatched inputs and the persisted book-profile snapshot. It must not require chapter lists, chapter titles, selection matrices, or scraper artifacts.
- Before changing a workflow, identify its required inputs, produced artifacts, and downstream consumers. Do not invoke a larger workflow merely to obtain a small subset of its data.
- When changing cover, scraping, merge, or publication behavior, run tests for both the changed stage and its directly adjacent stages.
- If the existing tests do not enforce a workflow boundary affected by a change, add a regression test before considering the change complete.
