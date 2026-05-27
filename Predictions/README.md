# Prediction History

Each scheduled prediction run writes one JSON file per market into:

`Predictions/<market-id>/<timestamp>.json`

The same folder also contains `latest.json` for the newest prediction for that
market. Closed markets are stored with `status: "closed"` and no probability.
