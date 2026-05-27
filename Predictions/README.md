# Prediction History

Each scheduled prediction run writes CSV snapshots under this folder:

- `Predictions/latest.csv`: newest full prediction run
- `Predictions/runs/<timestamp>.csv`: full prediction run archive
- `Predictions/<market-id>/latest.csv`: newest prediction for one market
- `Predictions/<market-id>/<timestamp>.csv`: one archived prediction for one market
- `Predictions/<market-id>/history.csv`: append-style market history

The same per-market folders also keep `latest.json` and timestamped JSON files
for structured consumers. Closed markets are stored with `forecastStatus:
"closed"` and no probability.
