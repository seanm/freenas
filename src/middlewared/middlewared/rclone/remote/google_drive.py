from middlewared.rclone.base import BaseRcloneRemote
from middlewared.schema import Str


class GoogleDriveRcloneRemote(BaseRcloneRemote):
    name = "GOOGLE_DRIVE"
    title = "Google Drive"

    rclone_type = "drive"

    credentials_schema = [
        Str("client_id", title="OAuth Client ID", default=""),
        Str("client_secret", title="OAuth Client Secret", default=""),
        Str("token", title="Access Token", required=True, max_length=None),
        Str("team_drive", title="Team Drive ID (if connecting to Team Drive)"),
    ]
    credentials_oauth = True
    refresh_credentials = ["token"]
