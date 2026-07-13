#!/usr/bin/env bash
# One-off credential/number check for send_lead_text (Decision 032 follow-up).
# Doesn't send anything, doesn't cost anything, doesn't print secrets.
# Run from the repo root: bash scripts/test_twilio_creds.sh

set -euo pipefail

ENV_FILE="${1:-.env.local}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "No $ENV_FILE found. Copy .env.example to .env.local and fill in TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM_NUMBER first."
    exit 1
fi

TWILIO_ACCOUNT_SID=$(grep -E '^TWILIO_ACCOUNT_SID=' "$ENV_FILE" | cut -d '=' -f2-)
TWILIO_AUTH_TOKEN=$(grep -E '^TWILIO_AUTH_TOKEN=' "$ENV_FILE" | cut -d '=' -f2-)
TWILIO_FROM_NUMBER=$(grep -E '^TWILIO_FROM_NUMBER=' "$ENV_FILE" | cut -d '=' -f2-)

if [[ -z "$TWILIO_ACCOUNT_SID" || -z "$TWILIO_AUTH_TOKEN" ]]; then
    echo "TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is empty in $ENV_FILE — fill both in before testing."
    exit 1
fi

echo "1. Checking account credentials (SID + auth token)..."
ACCOUNT_STATUS=$(curl -s -o /tmp/twilio_account_check.json -w "%{http_code}" \
    -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}" \
    "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}.json")

if [[ "$ACCOUNT_STATUS" == "200" ]]; then
    STATUS_FIELD=$(python3 -c "import json;print(json.load(open('/tmp/twilio_account_check.json'))['status'])" 2>/dev/null || echo "unknown")
    TYPE_FIELD=$(python3 -c "import json;print(json.load(open('/tmp/twilio_account_check.json'))['type'])" 2>/dev/null || echo "unknown")
    echo "   OK (200) — account status: $STATUS_FIELD, account type: $TYPE_FIELD"
else
    echo "   FAILED ($ACCOUNT_STATUS) — credentials are wrong or the account is suspended. This is the 401 Abdoul hit."
    rm -f /tmp/twilio_account_check.json
    exit 1
fi

echo "2. Checking TWILIO_FROM_NUMBER ($TWILIO_FROM_NUMBER) is actually owned by this account..."
NUMBERS_STATUS=$(curl -s -o /tmp/twilio_numbers_check.json -w "%{http_code}" \
    -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}" \
    "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/IncomingPhoneNumbers.json")

if [[ "$NUMBERS_STATUS" == "200" ]]; then
    if grep -q "$TWILIO_FROM_NUMBER" /tmp/twilio_numbers_check.json; then
        echo "   OK (200) — $TWILIO_FROM_NUMBER is a real number on this account."
    else
        echo "   MISMATCH (200, but not found) — this account's actual number(s):"
        python3 -c "import json;[print('     ', n['phone_number']) for n in json.load(open('/tmp/twilio_numbers_check.json'))['incoming_phone_numbers']]" 2>/dev/null \
            || echo "     (couldn't parse response)"
        echo "   Compare against TWILIO_FROM_NUMBER in .env.local — likely a formatting mismatch (missing '+', wrong digits)."
    fi
else
    echo "   FAILED ($NUMBERS_STATUS):"
    python3 -c "import json;d=json.load(open('/tmp/twilio_numbers_check.json'));print('    ', d.get('message', d))" 2>/dev/null \
        || cat /tmp/twilio_numbers_check.json
fi

echo "3. Checking verified caller IDs (trial accounts can only send to these)..."
VERIFIED_STATUS=$(curl -s -o /tmp/twilio_verified_check.json -w "%{http_code}" \
    -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}" \
    "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/OutgoingCallerIds.json")

if [[ "$VERIFIED_STATUS" == "200" ]]; then
    COUNT=$(python3 -c "import json;print(len(json.load(open('/tmp/twilio_verified_check.json'))['outgoing_caller_ids']))" 2>/dev/null || echo "?")
    echo "   OK (200) — $COUNT verified caller ID(s) on this account. If testing send_lead_text against a specific lead number, that number needs to be in this list (trial accounts only)."
else
    echo "   FAILED ($VERIFIED_STATUS):"
    python3 -c "import json;d=json.load(open('/tmp/twilio_verified_check.json'));print('    ', d.get('message', d))" 2>/dev/null \
        || cat /tmp/twilio_verified_check.json
fi

rm -f /tmp/twilio_account_check.json /tmp/twilio_numbers_check.json /tmp/twilio_verified_check.json
echo "Done."
