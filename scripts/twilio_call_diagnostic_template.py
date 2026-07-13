# Twilio call diagnostic template — NOT part of the LeadPilot build.
#
# Provided by Twilio support while investigating known-issues-log.md
# Issue 005 (`Policy evaluation failed` on IncomingPhoneNumbers/
# OutgoingCallerIds). Places a single outbound test call directly via
# client.calls.create(), independent of any LeadPilot code — useful
# for isolating "is this a LeadPilot bug or a Twilio account issue"
# the next time something in the Twilio integration misbehaves.
#
# None of LeadPilot's own code imports or calls this file. It isn't
# one of the 11 Step 2 tools and never runs as part of the product.
#
# Usage: copy this file, fill in YOUR_ACCOUNT_SID/YOUR_AUTH_TOKEN with
# real values locally, run it, then throw the copy away — never commit
# a version with real credentials filled in. This template itself only
# ever contains placeholders and is safe to commit as-is.

from twilio.rest import Client

# Your Twilio credentials
account_sid = 'YOUR_ACCOUNT_SID'
auth_token = 'YOUR_AUTH_TOKEN'

# Phone numbers
from_number = '+15165308341'  # Your Twilio number
to_number = '+0987654321'     # The destination number

# The URL Twilio will request when the call is answered
twiml_url = 'http://demo.twilio.com/docs/voice.xml'

client = Client(account_sid, auth_token)

call = client.calls.create(
    to=to_number,
    from_=from_number,
    url=twiml_url
)

print(f"Call initiated. SID: {call.sid}")
