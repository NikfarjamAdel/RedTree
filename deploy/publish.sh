#!/bin/zsh
# Publishes the function app from a clean copy that holds only what the
# pipeline needs at run time. The copy is made from an explicit list rather
# than relying on .funcignore: Core Tools' ignore matching has let .env and
# data/ through before, and its --list-included-files ignores it entirely.
#
# Before publishing: the unit tests must pass, and the copy must contain no
# forbidden files and no secrets.
#
# Usage: deploy/publish.sh            test, list the files, ask, then publish
#        deploy/publish.sh --dry-run  test and list the files only

set -e
cd "$(dirname "$0")/.."

[[ -f deploy/azure.env ]] || { echo "Missing deploy/azure.env: copy deploy/azure.env.example to deploy/azure.env and fill in your names"; exit 1; }
source deploy/azure.env                  # FUNC_APP, RG, KEYVAULT, ...
# config/ stays out: the pipeline reads property facts from the database,
# and property.json (private) is only read locally by setup_property.py.
INCLUDE=(function_app.py host.json requirements.txt python sql)

# 1. Tests first: broken code is never published
echo "Running tests..."
.venv/bin/python -m pytest -q || { echo "Tests failed: not publishing"; exit 1; }

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

for item in $INCLUDE; do
    rsync -a --exclude "__pycache__" --exclude ".DS_Store" "$item" "$STAGE/"
done

# 2. Nothing secret or private in the copy: no forbidden files...
FORBIDDEN=$(find "$STAGE" \( -name ".env*" -o -name "data" -o -name "*.bacpac" \
                            -o -name "logs" -o -name ".venv" -o -name "property.json" \))
if [[ -n "$FORBIDDEN" ]]; then
    echo "Refusing to publish, found:"
    echo "${FORBIDDEN//$STAGE\//  }"
    exit 1
fi

# ...and no secrets pasted into a file by mistake
SECRETS=$(grep -rIl -e "AccountKey=" -e "sk-ant-" -e "Password=" "$STAGE" || true)
if [[ -n "$SECRETS" ]]; then
    echo "Refusing to publish, possible secrets in:"
    echo "${SECRETS//$STAGE\//  }"
    exit 1
fi

echo "Files to publish:"
(cd "$STAGE" && find . -type f | sed 's|^\./|  |' | sort)
echo "$(cd "$STAGE" && find . -type f | wc -l | tr -d ' ') files"

[[ "$1" == "--dry-run" ]] && exit 0

read "reply?Publish these to $FUNC_APP? [Y/N] "
[[ "$reply" == [yY] ]] || { echo "Not published"; exit 1; }

cd "$STAGE"
# The copy has no local.settings.json, so func is told the runtime here
FUNCTIONS_WORKER_RUNTIME=python func azure functionapp publish "$FUNC_APP"
