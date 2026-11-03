#!/bin/zsh
# Run the daily pipeline in Azure now, rather than waiting for its schedule.
# The function's master key is fetched each time, so nothing secret is stored here.
# Results appear in the function app's logs after a few minutes.

cd "$(dirname "$0")/.."
[[ -f deploy/azure.env ]] || { echo "Missing deploy/azure.env: copy deploy/azure.env.example to deploy/azure.env and fill in your names"; exit 1; }
source deploy/azure.env                  # FUNC_APP, RG, KEYVAULT, ...

MASTER=$(az functionapp keys list --name "$FUNC_APP" --resource-group "$RG" \
         --query masterKey -o tsv) || { echo "Could not get the key (signed in to az?)"; exit 1; }

STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "https://$FUNC_APP.azurewebsites.net/admin/functions/daily_pipeline" \
    -H "x-functions-key: $MASTER" -H "Content-Type: application/json" -d '{"input": ""}')

if [[ "$STATUS" == "202" ]]; then
    echo "Started. Check Monitoring -> Logs in a few minutes."
else
    echo "Not started: HTTP $STATUS"
    exit 1
fi