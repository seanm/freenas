from middlewared.alert.base import AlertService
from middlewared.schema import Dict, Str


class MailAlertService(AlertService):
    title = "E-Mail"

    schema = Dict(
        "mail_attributes",
        Str("email", default=""),
        strict=True,
    )

    async def send(self, alerts, gone_alerts, new_alerts):
        email = self.attributes["email"]
        if not email:
            email = (await self.middleware.call("user.query", [("username", "=", "root")], {"get": True}))["email"]
        if not email:
            self.logger.trace("E-Mail address for root not configured, not sending e-mail")
            return

        text = await self._format_alerts(alerts, gone_alerts, new_alerts)

        await self.middleware.call("mail.send", {
            "subject": "Alerts",
            "text": text,
            "html": text.replace("\n", "<br>"),
            "to": [email],
        })
