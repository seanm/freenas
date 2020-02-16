import base64
import hashlib
import os
import syslog

from middlewared.schema import accepts, Bool, Dict, Int, List, Str, ValidationErrors
from middlewared.validators import Range
from middlewared.service import SystemServiceService
import middlewared.sqlalchemy as sa


class SSHModel(sa.Model):
    __tablename__ = 'services_ssh'

    id = sa.Column(sa.Integer(), primary_key=True)
    ssh_bindiface = sa.Column(sa.MultiSelectField(), default=[])
    ssh_tcpport = sa.Column(sa.Integer(), default=22)
    ssh_rootlogin = sa.Column(sa.Boolean(), default=False)
    ssh_passwordauth = sa.Column(sa.Boolean(), default=False)
    ssh_kerberosauth = sa.Column(sa.Boolean(), default=False)
    ssh_tcpfwd = sa.Column(sa.Boolean(), default=False)
    ssh_compression = sa.Column(sa.Boolean(), default=False)
    ssh_privatekey = sa.Column(sa.Text())
    ssh_sftp_log_level = sa.Column(sa.String(20))
    ssh_sftp_log_facility = sa.Column(sa.String(20))
    ssh_host_dsa_key = sa.Column(sa.Text(), nullable=True)
    ssh_host_dsa_key_pub = sa.Column(sa.Text(), nullable=True)
    ssh_host_dsa_key_cert_pub = sa.Column(sa.Text(), nullable=True)
    ssh_host_ecdsa_key = sa.Column(sa.Text(), nullable=True)
    ssh_host_ecdsa_key_pub = sa.Column(sa.Text(), nullable=True)
    ssh_host_ecdsa_key_cert_pub = sa.Column(sa.Text(), nullable=True)
    ssh_host_ed25519_key_pub = sa.Column(sa.Text(), nullable=True)
    ssh_host_ed25519_key = sa.Column(sa.Text(), nullable=True)
    ssh_host_ed25519_key_cert_pub = sa.Column(sa.Text(), nullable=True)
    ssh_host_key = sa.Column(sa.Text(), nullable=True)
    ssh_host_key_pub = sa.Column(sa.Text(), nullable=True)
    ssh_host_rsa_key = sa.Column(sa.Text(), nullable=True)
    ssh_host_rsa_key_pub = sa.Column(sa.Text(), nullable=True)
    ssh_host_rsa_key_cert_pub = sa.Column(sa.Text(), nullable=True)
    ssh_options = sa.Column(sa.Text())


class SSHService(SystemServiceService):

    class Config:
        service = "ssh"
        service_model = "ssh"
        datastore_prefix = "ssh_"

    @accepts()
    def bindiface_choices(self):
        """
        Available choices for the bindiface attribute of SSH service.
        """
        return self.middleware.call_sync('interface.choices')

    @accepts(Dict(
        'ssh_update',
        List('bindiface', items=[Str('iface')]),
        Int('tcpport', validators=[Range(min=1, max=65535)]),
        Bool('rootlogin'),
        Bool('passwordauth'),
        Bool('kerberosauth'),
        Bool('tcpfwd'),
        Bool('compression'),
        Str('sftp_log_level', enum=["", "QUIET", "FATAL", "ERROR", "INFO", "VERBOSE", "DEBUG", "DEBUG2", "DEBUG3"]),
        Str('sftp_log_facility', enum=["", "DAEMON", "USER", "AUTH", "LOCAL0", "LOCAL1", "LOCAL2", "LOCAL3", "LOCAL4",
                                       "LOCAL5", "LOCAL6", "LOCAL7"]),
        Str('options', max_length=None),
        update=True
    ))
    async def do_update(self, data):
        """
        Update settings of SSH daemon service.

        If `bindiface` is empty it will listen for all available addresses.

        .. examples(websocket)::

          Make sshd listen only to igb0 interface.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "ssh.update",
                "params": [{
                    "bindiface": ["igb0"]
                }]
            }

        """
        old = await self.config()

        new = old.copy()
        new.update(data)

        if new['bindiface']:
            verrors = ValidationErrors()
            iface_choices = await self.middleware.call('ssh.bindiface_choices')
            invalid_ifaces = list(filter(lambda x: x not in iface_choices, new['bindiface']))
            if invalid_ifaces:
                verrors.add(
                    'ssh_update.bindiface',
                    f'The following interfaces are not valid: {", ".join(invalid_ifaces)}',
                )
            verrors.check()

        await self._update_service(old, new)

        keyfile = "/usr/local/etc/ssh/ssh_host_ecdsa_key.pub"
        if os.path.exists(keyfile):
            with open(keyfile, "rb") as f:
                pubkey = f.read().strip().split(None, 3)[1]
            decoded_key = base64.b64decode(pubkey)
            key_digest = hashlib.sha256(decoded_key).digest()
            ssh_fingerprint = (b"SHA256:" + base64.b64encode(key_digest).replace(b"=", b"")).decode("utf-8")

            syslog.openlog(logoption=syslog.LOG_PID, facility=syslog.LOG_USER)
            syslog.syslog(syslog.LOG_ERR, 'ECDSA Fingerprint of the SSH KEY: ' + ssh_fingerprint)
            syslog.closelog()

        return new
