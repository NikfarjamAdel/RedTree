#!/bin/zsh
# Creates the Azure resources for the pipeline.
# Run one numbered section at a time the first time, checking each result,
# from the repository root. Every name comes from deploy/azure.env (copy
# deploy/azure.env.example and fill it in); none is written here.
#
# Prerequisites (one-off per machine; macOS shown):
#   brew install azure-cli
#   brew tap azure/functions
#   brew trust azure/functions          # Microsoft's official tap
#   brew install azure-functions-core-tools@4
#   brew link azure/functions/azure-functions-core-tools@4   # if 'func' isn't found
#   az login
#
# No secrets are ever written in this file: they're read from .env when
# they're stored in Key Vault.

command -v az   >/dev/null || { echo "Azure CLI not installed (see Prerequisites)"; exit 1; }
command -v func >/dev/null || { echo "Azure Functions Core Tools not installed (see Prerequisites)"; exit 1; }

# --- 1. Names, from deploy/azure.env ---------------------------------------
# Storage, SQL server and Document Intelligence names must be unique across
# Azure: if one is taken, change SUFFIX in deploy/azure.env and start again
# from here.
[[ -f deploy/azure.env ]] || { echo "Missing deploy/azure.env: copy deploy/azure.env.example to deploy/azure.env and fill in your names (run from the repository root)"; exit 1; }
source deploy/azure.env

# --- 2. Right account, then the resource group -----------------------------
# Refuse to run signed in to any tenant but the one in deploy/azure.env.
TENANT=$(az account show --query tenantId -o tsv)
echo "Signed in to tenant: $TENANT"
[[ "$TENANT" == "$EXPECTED_TENANT" ]] || { echo "Wrong account: expected tenant $EXPECTED_TENANT (az login, or fix EXPECTED_TENANT in deploy/azure.env)"; exit 1; }

az group create --name "$RG" --location "$LOCATION" -o table


for ns in Microsoft.Storage Microsoft.Sql Microsoft.CognitiveServices \
          Microsoft.Web Microsoft.KeyVault Microsoft.Insights \
          Microsoft.OperationalInsights; do
    az provider register --namespace "$ns" --wait
    echo "registered: $ns"
done

# --- 3. Storage account and its containers ---------------------------------
# Private containers only; TLS 1.2 minimum.
az storage account create --name "$STORAGE" --resource-group "$RG" \
    --location "$LOCATION" --sku Standard_LRS --kind StorageV2 \
    --min-tls-version TLS1_2 --allow-blob-public-access false -o table

STORAGE_CONN=$(az storage account show-connection-string \
    --name "$STORAGE" --resource-group "$RG" -o tsv)
for c in $CONTAINERS; do
    az storage container create --name "$c" --connection-string "$STORAGE_CONN" -o none
    echo "container: $c"
done

# --- 4. SQL server, firewall rule for this computer, free database ---------
# The admin password is typed here and never stored in this file.
read -s "SQL_PASSWORD?SQL admin password (12+ chars, mixed case, digit, symbol): "; echo

az sql server create --name "$SQL_SERVER" --resource-group "$RG" \
    --location "$LOCATION" --admin-user "$SQL_ADMIN" \
    --admin-password "$SQL_PASSWORD" --minimal-tls-version 1.2 -o table

MY_IP=$(curl -s https://api.ipify.org)
az sql server firewall-rule create --resource-group "$RG" --server "$SQL_SERVER" \
    --name "laptop-$(date +%Y%m%d)" --start-ip-address "$MY_IP" --end-ip-address "$MY_IP" -o table

# Free offer: serverless General Purpose; pauses (never charges) when the
# monthly free allowance is used up.
az sql db create --resource-group "$RG" --server "$SQL_SERVER" --name "$SQL_DB" \
    --edition GeneralPurpose --compute-model Serverless --family Gen5 \
    --capacity 2 --min-capacity 0.5 --auto-pause-delay 60 \
    --use-free-limit --free-limit-exhaustion-behavior AutoPause -o table

# --- 5. Document Intelligence, free tier -----------------------------------
az cognitiveservices account create --name "$DOCINTEL" --resource-group "$RG" \
    --location "$LOCATION" --kind FormRecognizer --sku F0 \
    --custom-domain "$DOCINTEL" --yes -o table

# --- 6. Values for .env (printed, not stored) ------------------------------
# Copy these into .env.
echo "AZURE_MSSQL_HOST=${SQL_SERVER}.database.windows.net"
echo "AZURE_MSSQL_PORT=1433"
echo "AZURE_MSSQL_DB=${SQL_DB}"
echo "AZURE_MSSQL_USER=${SQL_ADMIN}"
echo "AZURE_STORAGE_CONNECTION_STRING=$(az storage account show-connection-string \
    --name "$STORAGE" --resource-group "$RG" -o tsv)"
echo "AZURE_DOCINTEL_ENDPOINT=$(az cognitiveservices account show --name "$DOCINTEL" \
    --resource-group "$RG" --query properties.endpoint -o tsv)"
echo "AZURE_DOCINTEL_KEY=$(az cognitiveservices account keys list --name "$DOCINTEL" \
    --resource-group "$RG" --query key1 -o tsv)"

# --- 7. Names for the function app -----------------------------------------
# FUNC_STORAGE (the app's own housekeeping storage), FUNC_APP and KEYVAULT
# come from deploy/azure.env (section 1).
echo "Function app $FUNC_APP, its storage $FUNC_STORAGE, Key Vault $KEYVAULT"


# --- 8. Function app: Flex Consumption, Python 3.11 ------------------------
# A separate storage account keeps the app's housekeeping apart from the
# data. Application Insights (logging) is created with the app.
az storage account create --name "$FUNC_STORAGE" --resource-group "$RG" \
    --location "$LOCATION" --sku Standard_LRS --kind StorageV2 \
    --min-tls-version TLS1_2 --allow-blob-public-access false -o none

az functionapp create --name "$FUNC_APP" --resource-group "$RG" \
    --storage-account "$FUNC_STORAGE" --flexconsumption-location "$LOCATION" \
    --runtime python --runtime-version 3.11 -o table


# --- 9. Identity and Key Vault ---------------------------------------------
# The app gets its own Azure identity, allowed only to READ secrets.
# You get permission to WRITE them.
az functionapp identity assign --name "$FUNC_APP" --resource-group "$RG" -o none
APP_IDENTITY=$(az functionapp identity show --name "$FUNC_APP" --resource-group "$RG" \
    --query principalId -o tsv)

az keyvault create --name "$KEYVAULT" --resource-group "$RG" --location "$LOCATION" \
    --enable-rbac-authorization true -o none
KV_ID=$(az keyvault show --name "$KEYVAULT" --query id -o tsv)

az role assignment create --assignee-object-id "$APP_IDENTITY" --assignee-principal-type ServicePrincipal \
    --role "Key Vault Secrets User" --scope "$KV_ID" -o none
az role assignment create --assignee "$(az ad signed-in-user show --query id -o tsv)" \
    --role "Key Vault Secrets Officer" --scope "$KV_ID" -o none
echo "Key Vault ready (role assignments can take a minute or two to apply)"


# --- 10. Secrets: copied from .env into Key Vault, never printed -----------
# Secret names use hyphens; Key Vault doesn't allow underscores. Each
# listing's calendar URL is its own secret, listed in ICAL_SECRETS in
# deploy/azure.env as secret-name:SETTING_NAME.
env_value() { grep "^$1=" .env | cut -d= -f2-; }
for pair in sql-password:AZURE_MSSQL_PASSWORD \
            storage-connection:AZURE_STORAGE_CONNECTION_STRING \
            docintel-key:AZURE_DOCINTEL_KEY \
            anthropic-key:ANTHROPIC_API_KEY \
            gmail-app-password:GMAIL_APP_PASSWORD \
            $ICAL_SECRETS; do
    az keyvault secret set --vault-name "$KEYVAULT" --name "${pair%%:*}" \
        --value "$(env_value ${pair##*:})" -o none && echo "secret: ${pair%%:*}"
done


# --- 11. App settings: plain values, and Key Vault references for secrets --
ref() { echo "@Microsoft.KeyVault(VaultName=${KEYVAULT};SecretName=$1)"; }
ICAL_SETTINGS=()
for pair in $ICAL_SECRETS; do
    ICAL_SETTINGS+=("${pair##*:}=$(ref ${pair%%:*})")
done
az functionapp config appsettings set --name "$FUNC_APP" --resource-group "$RG" -o none --settings \
    "AZURE_MSSQL_HOST=$(env_value AZURE_MSSQL_HOST)" \
    "AZURE_MSSQL_PORT=$(env_value AZURE_MSSQL_PORT)" \
    "AZURE_MSSQL_DB=$(env_value AZURE_MSSQL_DB)" \
    "AZURE_MSSQL_USER=$(env_value AZURE_MSSQL_USER)" \
    "AZURE_MSSQL_PASSWORD=$(ref sql-password)" \
    "AZURE_STORAGE_CONNECTION_STRING=$(ref storage-connection)" \
    "AZURE_STORAGE_CONTAINER=$(env_value AZURE_STORAGE_CONTAINER)" \
    "AZURE_EXTRACTION_CONTAINER=$(env_value AZURE_EXTRACTION_CONTAINER)" \
    "AZURE_ICAL_CONTAINER=$(env_value AZURE_ICAL_CONTAINER)" \
    "AZURE_CATEGORISATION_CONTAINER=$(env_value AZURE_CATEGORISATION_CONTAINER)" \
    "AZURE_EXPORTS_CONTAINER=$(env_value AZURE_EXPORTS_CONTAINER)" \
    "AZURE_DOCINTEL_ENDPOINT=$(env_value AZURE_DOCINTEL_ENDPOINT)" \
    "AZURE_DOCINTEL_KEY=$(ref docintel-key)" \
    "ANTHROPIC_API_KEY=$(ref anthropic-key)" \
    "GMAIL_USER=$(env_value GMAIL_USER)" \
    "GMAIL_APP_PASSWORD=$(ref gmail-app-password)" \
    "ALERT_EMAIL=$(env_value ALERT_EMAIL)" \
    $ICAL_SETTINGS
echo "app settings set"


# --- 12. Let Azure services reach the database -----------------------------
# The 0.0.0.0 rule means "any Azure service", not just this function app:
# an accepted trade-off. The password (in Key Vault) is the main protection.
az sql server firewall-rule create --resource-group "$RG" --server "$SQL_SERVER" \
    --name AllowAzureServices --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 -o table
