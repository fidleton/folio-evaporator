# Agent Instructions

- Cache external data as much as needed for the app to function, minimizing external network traffic.
  - interval times of cached data (stored in the database):
    - 1D : 5 minutes
    - 1W: 30 minutes
    - 1M: 1 day
    - 1Y: 1 day
    - 2Y: 1 day
    - 5Y: 2 weeks
    - YTD: 1day
   - Cached data that cannot be displayed must be removed from the database
   - 1D data stays valid until data for a new trading session is available. This keeps 1D data visible during the weekend and holidays.
- Log every API request and every call to an external system; include operational metadata and duration, but never log credentials, request bodies, or sensitive user data.
- Keep the app usable on mobile and desktop within the Home Assistant interface.
- Implement mobile friendly alternatives if needed
- times in database in UTC. Display times in localtime.
- Always execute and validate the tests

## Backend tests

When running backend tests, always use the VS Code integrated terminal and the **Run backend tests** task via the terminal/task runner. Do not run `python -m pytest` through an isolated shell command, because it may use a different Python environment. The task executes:

```bash
python -m pytest
```

