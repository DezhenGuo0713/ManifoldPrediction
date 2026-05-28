# Prediction History

Each scheduled prediction run writes CSV snapshots under this folder:

- `Predictions/latest.csv`: newest full prediction run
- `Predictions/<timestamp>.csv`: all markets from one prediction run
- `Predictions/runs/<timestamp>.csv`: full prediction run archive
- `Predictions/<market-id>/latest.csv`: newest prediction for one market
- `Predictions/<market-id>/<timestamp>.csv`: one archived prediction for one market
- `Predictions/<market-id>/history.csv`: append-style market history

The same per-market folders also keep `latest.json` and timestamped JSON files
for structured consumers. Closed markets are stored with `forecastStatus:
"closed"` and no probability.

Forecast rows store the final probability plus the three component signals:
current market probability, web-search forecast, and no-search model prior. The
default blend uses weighted logit averaging with market/search/prior weights
`0.40 / 0.40 / 0.20`.
