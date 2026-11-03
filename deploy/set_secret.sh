#!/bin/zsh
# Replaces one secret in Key Vault and in .env, typed in without being shown.
# The function app is then made to reload its Key Vault secrets.
#
# Usage: deploy/set_secret.sh <key-vault-name> <env-name>
#   e.g. deploy/set_secret.sh anthropic-key ANTHROPIC_API_KEY
#        deploy/set_secret.sh gmail-app-password GMAIL_APP_PASSWORD
#        deploy/set_secret.sh ical-url-suite AIRBNB_ICAL_URL_SUITE  (one of ICAL_SECRETS)

set -e
cd "$(dirname "$0")/.."

[[ -f deploy/azure.env ]] || { echo "Missing deploy/azure.env: copy deploy/azure.env.example to deploy/azure.env and fill in your names"; exit 1; }
source deploy/azure.env                  # FUNC_APP, RG, KEYVAULT, ...

[[ $# -eq 2 ]] || { echo "Usage: $0 <key-vault-name> <env-name>"; exit 1; }
grep -q "^$2=" .env || { echo "$2 is not in .env"; exit 1; }

read -s "VALUE?New value for $2 (not shown): "
echo
[[ -n "$VALUE" ]] || { echo "Empty value, nothing changed"; exit 1; }

az keyvault secret set --vault-name "$KEYVAULT" --name "$1" --value "$VALUE" -o none
echo "Key Vault: $1 updated"

NAME="$2" VALUE="$VALUE" python3 -c "
import os, re, pathlib
p = pathlib.Path('.env')
t, n = re.subn(rf'(?m)^{os.environ[\"NAME\"]}=.*$',
               lambda m: os.environ['NAME'] + '=' + os.environ['VALUE'], p.read_text())
assert n == 1
p.write_text(t)"
echo ".env: $2 updated"

# A restart is not enough: the app caches Key Vault values and re-reads them
# only when its configuration changes (or every 24 hours), so change a setting.
az functionapp config appsettings set --name "$FUNC_APP" --resource-group "$RG" -o none \
    --settings "KEYVAULT_REFRESHED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$FUNC_APP will reload its Key Vault secrets"
