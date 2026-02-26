"""
Twilio Integration — SMS patient notification
Step 8: Send dashboard link to patient's phone number

Docs: https://www.twilio.com/docs/sms/quickstart/python
"""

import os
from typing import Optional


class TwilioClient:
    def __init__(self) -> None:
        self.account_sid  = os.getenv("TWILIO_ACCOUNT_SID")
        self.auth_token   = os.getenv("TWILIO_AUTH_TOKEN")
        self.from_number  = os.getenv("TWILIO_FROM_NUMBER")

    def send(self, to: str, body: str) -> Optional[str]:
        """
        Sends an SMS message.
        Returns the Twilio message SID, or None if not configured.
        """
        if not all([self.account_sid, self.auth_token, self.from_number]):
            print(f"[Twilio] Not configured. Would send to {to}:\n  {body}")
            return None

        from twilio.rest import Client
        client = Client(self.account_sid, self.auth_token)

        message = client.messages.create(
            body=body,
            from_=self.from_number,
            to=to,
        )

        print(f"[Twilio] SMS sent → {to}  (SID: {message.sid})")
        return message.sid
