# Agent Instructions

- Cache external data as much as needed for the app to function, minimizing external network traffic.
- Log every API request and every call to an external system; include operational metadata and duration, but never log credentials, request bodies, or sensitive user data.
- Keep the app usable on mobile and desktop within the Home Assistant interface.
- Implement mobile friendly alternatives if needed
- times in database in UTC. Display times in localtime.

## Backend tests

When running backend tests, always use the VS Code integrated terminal and the **Run backend tests** task via the terminal/task runner. Do not run `python -m pytest` through an isolated shell command, because it may use a different Python environment. The task executes:

```bash
python -m pytest
```

