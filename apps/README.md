# apps/

Deployable applications: the FastAPI Thymira API (`api`, the only door to the runtime) and the
browser web console (`web`, `thymira-web`), a client of that API. Apps contain no domain logic; the
API calls `runtime/`, and the console calls the API over HTTP.
