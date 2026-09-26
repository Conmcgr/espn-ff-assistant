# Reports

`coverage.csv` and completed verification notes are generated outputs and are
ignored by Git. Rebuild coverage and transaction reports from a raw run with:

```bash
uv run python scripts/rebuild_reports.py YOUR_RUN_ID
```

Keep the run ID with any report you use for manual verification.
