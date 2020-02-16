from middlewared.async_validators import check_path_resides_within_volume, resolve_hostname
from middlewared.schema import accepts, Bool, Dict, Dir, Int, Str
from middlewared.validators import Exact, Match, Or, Range
from middlewared.service import private, SystemServiceService, ValidationErrors
import middlewared.sqlalchemy as sa


class FTPModel(sa.Model):
    __tablename__ = 'services_ftp'

    id = sa.Column(sa.Integer(), primary_key=True)
    ftp_port = sa.Column(sa.Integer(), default=21)
    ftp_clients = sa.Column(sa.Integer(), default=32)
    ftp_ipconnections = sa.Column(sa.Integer(), default=0)
    ftp_loginattempt = sa.Column(sa.Integer(), default=3)
    ftp_timeout = sa.Column(sa.Integer(), default=120)
    ftp_rootlogin = sa.Column(sa.Boolean(), default=False)
    ftp_onlyanonymous = sa.Column(sa.Boolean(), default=False)
    ftp_anonpath = sa.Column(sa.String(255), nullable=True, default=False)
    ftp_onlylocal = sa.Column(sa.Boolean(), default=False)
    ftp_banner = sa.Column(sa.Text())
    ftp_filemask = sa.Column(sa.String(3), default="077")
    ftp_dirmask = sa.Column(sa.String(3), default="077")
    ftp_fxp = sa.Column(sa.Boolean(), default=False)
    ftp_resume = sa.Column(sa.Boolean(), default=False)
    ftp_defaultroot = sa.Column(sa.Boolean(), default=False)
    ftp_ident = sa.Column(sa.Boolean(), default=False)
    ftp_reversedns = sa.Column(sa.Boolean(), default=False)
    ftp_masqaddress = sa.Column(sa.String(120))
    ftp_passiveportsmin = sa.Column(sa.Integer(), default=0)
    ftp_passiveportsmax = sa.Column(sa.Integer(), default=0)
    ftp_localuserbw = sa.Column(sa.Integer(), default=0)
    ftp_localuserdlbw = sa.Column(sa.Integer(), default=0)
    ftp_anonuserbw = sa.Column(sa.Integer(), default=0)
    ftp_anonuserdlbw = sa.Column(sa.Integer(), default=0)
    ftp_tls = sa.Column(sa.Boolean(), default=False)
    ftp_tls_policy = sa.Column(sa.String(120), default="on")
    ftp_tls_opt_allow_client_renegotiations = sa.Column(sa.Boolean(), default=False)
    ftp_tls_opt_allow_dot_login = sa.Column(sa.Boolean(), default=False)
    ftp_tls_opt_allow_per_user = sa.Column(sa.Boolean(), default=False)
    ftp_tls_opt_common_name_required = sa.Column(sa.Boolean(), default=False)
    ftp_tls_opt_enable_diags = sa.Column(sa.Boolean(), default=False)
    ftp_tls_opt_export_cert_data = sa.Column(sa.Boolean(), default=False)
    ftp_tls_opt_no_cert_request = sa.Column(sa.Boolean(), default=False)
    ftp_tls_opt_no_empty_fragments = sa.Column(sa.Boolean(), default=False)
    ftp_tls_opt_no_session_reuse_required = sa.Column(sa.Boolean(), default=False)
    ftp_tls_opt_stdenvvars = sa.Column(sa.Boolean(), default=False)
    ftp_tls_opt_dns_name_required = sa.Column(sa.Boolean(), default=False)
    ftp_tls_opt_ip_address_required = sa.Column(sa.Boolean(), default=False)
    ftp_ssltls_certificate_id = sa.Column(sa.ForeignKey('system_certificate.id'), index=True, nullable=True)
    ftp_options = sa.Column(sa.Text())


class FTPService(SystemServiceService):

    class Config:
        service = "ftp"
        datastore_prefix = "ftp_"
        datastore_extend = "ftp.ftp_extend"

    @private
    async def ftp_extend(self, data):
        if data['ssltls_certificate']:
            data['ssltls_certificate'] = data['ssltls_certificate']['id']
        return data

    @accepts(Dict(
        'ftp_update',
        Int('port', validators=[Range(min=1, max=65535)]),
        Int('clients', validators=[Range(min=1, max=10000)]),
        Int('ipconnections', validators=[Range(min=0, max=1000)]),
        Int('loginattempt', validators=[Range(min=0, max=1000)]),
        Int('timeout', validators=[Range(min=0, max=10000)]),
        Bool('rootlogin'),
        Bool('onlyanonymous'),
        Dir('anonpath', null=True),
        Bool('onlylocal'),
        Str('banner', max_length=None),
        Str('filemask', validators=[Match(r"^[0-7]{3}$")]),
        Str('dirmask', validators=[Match(r"^[0-7]{3}$")]),
        Bool('fxp'),
        Bool('resume'),
        Bool('defaultroot'),
        Bool('ident'),
        Bool('reversedns'),
        Str('masqaddress'),
        Int('passiveportsmin', validators=[Or(Exact(0), Range(min=1024, max=65535))]),
        Int('passiveportsmax', validators=[Or(Exact(0), Range(min=1024, max=65535))]),
        Int('localuserbw', validators=[Range(min=0)]),
        Int('localuserdlbw', validators=[Range(min=0)]),
        Int('anonuserbw', validators=[Range(min=0)]),
        Int('anonuserdlbw', validators=[Range(min=0)]),
        Bool('tls'),
        Str('tls_policy', enum=["on", "off", "data", "!data", "auth", "ctrl",
                                "ctrl+data", "ctrl+!data", "auth+data", "auth+!data"]),
        Bool('tls_opt_allow_client_renegotiations'),
        Bool('tls_opt_allow_dot_login'),
        Bool('tls_opt_allow_per_user'),
        Bool('tls_opt_common_name_required'),
        Bool('tls_opt_enable_diags'),
        Bool('tls_opt_export_cert_data'),
        Bool('tls_opt_no_cert_request'),
        Bool('tls_opt_no_empty_fragments'),
        Bool('tls_opt_no_session_reuse_required'),
        Bool('tls_opt_stdenvvars'),
        Bool('tls_opt_dns_name_required'),
        Bool('tls_opt_ip_address_required'),
        Int('ssltls_certificate', null=True),
        Str('options', max_length=None),
        update=True
    ))
    async def do_update(self, data):
        """
        Update ftp service configuration.

        `clients` is an integer value which sets the maximum number of simultaneous clients allowed. It defaults to 32.

        `ipconnections` is an integer value which shows the maximum number of connections per IP address. It defaults
        to 0 which equals to unlimited.

        `timeout` is the maximum client idle time in seconds before client is disconnected.

        `rootlogin` is a boolean value which when configured to true enables login as root. This is generally
        discouraged because of the security risks.

        `onlyanonymous` allows anonymous FTP logins with access to the directory specified by `anonpath`.

        `banner` is a message displayed to local login users after they successfully authenticate. It is not displayed
        to anonymous login users.

        `filemask` sets the default permissions for newly created files which by default are 077.

        `dirmask` sets the default permissions for newly created directories which by default are 077.

        `resume` if set allows FTP clients to resume interrupted transfers.

        `fxp` if set to true indicates that File eXchange Protocol is enabled. Generally it is discouraged as it
        makes the server vulnerable to FTP bounce attacks.

        `defaultroot` when set ensures that for local users, home directory access is only granted if the user
        is a member of group wheel.

        `ident` is a boolean value which when set to true indicates that IDENT authentication is required. If identd
        is not running on the client, this can result in timeouts.

        `masqaddress` is the public IP address or hostname which is set if FTP clients cannot connect through a
        NAT device.

        `localuserbw` is a positive integer value which indicates maximum upload bandwidth in KB/s for local user.
        Default of zero indicates unlimited upload bandwidth ( from the FTP server configuration ).

        `localuserdlbw` is a positive integer value which indicates maximum download bandwidth in KB/s for local user.
        Default of zero indicates unlimited download bandwidth ( from the FTP server configuration ).

        `anonuserbw` is a positive integer value which indicates maximum upload bandwidth in KB/s for anonymous user.
        Default of zero indicates unlimited upload bandwidth ( from the FTP server configuration ).

        `anonuserdlbw` is a positive integer value which indicates maximum download bandwidth in KB/s for anonymous
        user. Default of zero indicates unlimited download bandwidth ( from the FTP server configuration ).

        `tls` is a boolean value which when set indicates that encrypted connections are enabled. This requires a
        certificate to be configured first with the certificate service and the id of certificate is passed on in
        `ssltls_certificate`.

        `tls_policy` defines whether the control channel, data channel, both channels, or neither channel of an FTP
        session must occur over SSL/TLS.

        `tls_opt_enable_diags` is a boolean value when set, logs verbosely. This is helpful when troubleshooting a
        connection.

        `options` is a string used to add proftpd(8) parameters not covered by ftp service.
        """
        old = await self.config()

        new = old.copy()
        new.update(data)

        verrors = ValidationErrors()

        if not ((new["passiveportsmin"] == 0) == (new["passiveportsmax"] == 0)):
            verrors.add("passiveportsmin", "passiveportsmin and passiveportsmax should be both zero or non-zero")
        if not ((new["passiveportsmin"] == 0 and new["passiveportsmax"] == 0) or
                (new["passiveportsmax"] > new["passiveportsmin"])):
            verrors.add("ftp_update.passiveportsmax", "When specified, should be greater than passiveportsmin")

        if new["onlyanonymous"] and not new["anonpath"]:
            verrors.add("ftp_update.anonpath", "This field is required for anonymous login")

        if new["anonpath"]:
            await check_path_resides_within_volume(verrors, self.middleware, "ftp_update.anonpath", new["anonpath"])

        if new["tls"]:
            if not new["ssltls_certificate"]:
                verrors.add(
                    "ftp_update.ssltls_certificate",
                    "Please provide a valid certificate id when TLS is enabled"
                )
            else:
                verrors.extend((await self.middleware.call(
                    "certificate.cert_services_validation", new["ssltls_certificate"],
                    "ftp_update.ssltls_certificate", False
                )))

        if new["masqaddress"]:
            await resolve_hostname(self.middleware, verrors, "ftp_update.masqaddress", new["masqaddress"])

        if verrors:
            raise verrors

        await self._update_service(old, new)

        if not old['tls'] and new['tls']:
            await self.middleware.call('service.start', 'ssl')

        return new
