import asyncio
from datetime import datetime, date
from middlewared.event import EventSource
from middlewared.i18n import set_language
from middlewared.logger import CrashReporting
from middlewared.schema import accepts, Bool, Dict, Int, IPAddr, List, Str
from middlewared.service import CallError, ConfigService, no_auth_required, job, private, Service, ValidationErrors
import middlewared.sqlalchemy as sa
from middlewared.utils import Popen, run, start_daemon_thread, sw_buildtime, sw_version, osc
from middlewared.validators import Range

import csv
import io
import os
import platform
import psutil
import re
import requests
import shutil
import socket
import subprocess
try:
    import sysctl
except ImportError:
    sysctl = None
import syslog
import tarfile
import textwrap
import time
import uuid
import warnings

from licenselib.license import ContractType, Features, License

IS_LINUX = platform.system().lower() == 'linux'
SYSTEM_BOOT_ID = None
# Flag telling whether the system completed boot and is ready to use
SYSTEM_READY = False
# Flag telling whether the system is shutting down
SYSTEM_SHUTTING_DOWN = False

CACHE_POOLS_STATUSES = 'system.system_health_pools'
FIRST_INSTALL_SENTINEL = '/data/first-boot'
LICENSE_FILE = '/data/license'

RE_MEMWIDTH = re.compile(r'Total Width:\s*(\d*)')


class SystemAdvancedModel(sa.Model):
    __tablename__ = 'system_advanced'

    id = sa.Column(sa.Integer(), primary_key=True)
    adv_consolemenu = sa.Column(sa.Boolean(), default=False)
    adv_serialconsole = sa.Column(sa.Boolean(), default=False)
    adv_serialport = sa.Column(sa.String(120), default="0x2f8")
    adv_serialspeed = sa.Column(sa.String(120), default="9600")
    adv_powerdaemon = sa.Column(sa.Boolean(), default=False)
    adv_swapondrive = sa.Column(sa.Integer(), default=2)
    adv_consolemsg = sa.Column(sa.Boolean(), default=True)
    adv_traceback = sa.Column(sa.Boolean(), default=True)
    adv_advancedmode = sa.Column(sa.Boolean(), default=False)
    adv_autotune = sa.Column(sa.Boolean(), default=False)
    adv_debugkernel = sa.Column(sa.Boolean(), default=False)
    adv_uploadcrash = sa.Column(sa.Boolean(), default=True)
    adv_anonstats = sa.Column(sa.Boolean(), default=True)
    adv_anonstats_token = sa.Column(sa.Text())
    adv_motd = sa.Column(sa.Text(), default='Welcome')
    adv_boot_scrub = sa.Column(sa.Integer(), default=7)
    adv_fqdn_syslog = sa.Column(sa.Boolean(), default=False)
    adv_sed_user = sa.Column(sa.String(120), default="user")
    adv_sed_passwd = sa.Column(sa.EncryptedText(), default='')
    adv_sysloglevel = sa.Column(sa.String(120), default="f_info")
    adv_syslogserver = sa.Column(sa.String(120), default='')
    adv_syslog_transport = sa.Column(sa.String(12), default="UDP")
    adv_syslog_tls_certificate_id = sa.Column(sa.ForeignKey('system_certificate.id'), index=True, nullable=True)
    adv_kmip_uid = sa.Column(sa.String(255), nullable=True, default=None)


class SystemAdvancedService(ConfigService):

    class Config:
        datastore = 'system.advanced'
        datastore_prefix = 'adv_'
        datastore_extend = 'system.advanced.system_advanced_extend'
        namespace = 'system.advanced'

    @accepts()
    async def serial_port_choices(self):
        """
        Get available choices for `serialport`.
        """
        if await self.middleware.call('failover.hardware') == 'ECHOSTREAM':
            ports = {'0x3f8': '0x3f8'}
        else:
            pipe = await Popen("/usr/sbin/devinfo -u | grep -A 99999 '^I/O ports:' | "
                               "sed -En 's/ *([0-9a-fA-Fx]+).*\(uart[0-9]+\)/\\1/p'", stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, shell=True)
            ports = {y: y for y in (await pipe.communicate())[0].decode().strip().strip('\n').split('\n') if y}

        if not ports or (await self.config())['serialport'] == '0x2f8':
            # We should always add 0x2f8 if ports is false or current value is the default one in db
            # i.e 0x2f8
            ports['0x2f8'] = '0x2f8'

        return ports

    @private
    async def system_advanced_extend(self, data):

        if data.get('sed_user'):
            data['sed_user'] = data.get('sed_user').upper()

        if data.get('sysloglevel'):
            data['sysloglevel'] = data['sysloglevel'].upper()

        if data['syslog_tls_certificate']:
            data['syslog_tls_certificate'] = data['syslog_tls_certificate']['id']

        data.pop('sed_passwd')
        data.pop('kmip_uid')

        return data

    async def __validate_fields(self, schema, data):
        verrors = ValidationErrors()

        serial_choice = data.get('serialport')
        if data.get('serialconsole'):

            if not serial_choice:
                verrors.add(
                    f'{schema}.serialport',
                    'Please specify a serial port when serial console option is checked'
                )
            else:
                data['serialport'] = serial_choice = hex(
                    int(serial_choice)
                ) if serial_choice.isdigit() else serial_choice
                if serial_choice not in await self.serial_port_choices():
                    verrors.add(
                        f'{schema}.serialport',
                        'Serial port specified has not been identified by the system'
                    )

        syslog_server = data.get('syslogserver')
        if syslog_server:
            match = re.match(r"^[\w\.\-]+(\:\d+)?$", syslog_server)
            if not match:
                verrors.add(
                    f'{schema}.syslogserver',
                    'Invalid syslog server format'
                )
            elif ':' in syslog_server:
                port = int(syslog_server.split(':')[-1])
                if port < 0 or port > 65535:
                    verrors.add(
                        f'{schema}.syslogserver',
                        'Port must be in the range of 0 to 65535.'
                    )

        if data['syslog_transport'] == 'TLS':
            await self.middleware.call('certificate.cert_services_validation', data['syslog_tls_certificate'],
                                       f'{schema}.syslog_tls_certificate')

        return verrors, data

    @accepts(
        Dict(
            'system_advanced_update',
            Bool('advancedmode'),
            Bool('autotune'),
            Int('boot_scrub', validators=[Range(min=1)]),
            Bool('consolemenu'),
            Bool('consolemsg'),
            Bool('debugkernel'),
            Bool('fqdn_syslog'),
            Str('motd'),
            Bool('powerdaemon'),
            Bool('serialconsole'),
            Str('serialport'),
            Str('serialspeed', enum=['9600', '19200', '38400', '57600', '115200']),
            Int('swapondrive', validators=[Range(min=0)]),
            Bool('traceback'),
            Bool('uploadcrash'),
            Bool('anonstats'),
            Str('sed_user', enum=['USER', 'MASTER']),
            Str('sed_passwd', private=True),
            Str('sysloglevel', enum=['F_EMERG', 'F_ALERT', 'F_CRIT', 'F_ERR',
                                     'F_WARNING', 'F_NOTICE', 'F_INFO',
                                     'F_DEBUG', 'F_IS_DEBUG']),
            Str('syslogserver'),
            Str('syslog_transport', enum=['UDP', 'TCP', 'TLS']),
            Int('syslog_tls_certificate', null=True),
            update=True
        )
    )
    async def do_update(self, data):
        """
        Update System Advanced Service Configuration.

        `consolemenu` should be disabled if the menu at console is not desired. It will default to standard login
        in the console if disabled.

        `autotune` when enabled executes autotune script which attempts to optimize the system based on the installed
        hardware.

        When `syslogserver` is defined, logs of `sysloglevel` or above are sent.
        """
        config_data = await self.config()
        config_data['sed_passwd'] = await self.sed_global_password()
        original_data = config_data.copy()
        config_data.update(data)

        verrors, config_data = await self.__validate_fields('advanced_settings_update', config_data)
        if verrors:
            raise verrors

        if len(set(config_data.items()) ^ set(original_data.items())) > 0:
            if original_data.get('sed_user'):
                original_data['sed_user'] = original_data['sed_user'].lower()
            if config_data.get('sed_user'):
                config_data['sed_user'] = config_data['sed_user'].lower()
            if not config_data['sed_passwd'] and config_data['sed_passwd'] != original_data['sed_passwd']:
                # We want to make sure kmip uid is None in this case
                adv_config = await self.middleware.call('datastore.config', self._config.datastore)
                asyncio.ensure_future(
                    self.middleware.call('kmip.reset_sed_global_password', adv_config['adv_kmip_uid'])
                )
                config_data['kmip_uid'] = None

            await self.middleware.call(
                'datastore.update',
                self._config.datastore,
                config_data['id'],
                config_data,
                {'prefix': self._config.datastore_prefix}
            )

            if original_data['boot_scrub'] != config_data['boot_scrub']:
                await self.middleware.call('service.restart', 'cron')

            loader_reloaded = False
            if original_data['motd'] != config_data['motd']:
                await self.middleware.call('service.start', 'motd', {'onetime': False})

            if original_data['consolemenu'] != config_data['consolemenu']:
                await self.middleware.call('service.start', 'ttys', {'onetime': False})

            if original_data['powerdaemon'] != config_data['powerdaemon']:
                await self.middleware.call('service.restart', 'powerd', {'onetime': False})

            if original_data['serialconsole'] != config_data['serialconsole']:
                await self.middleware.call('service.start', 'ttys', {'onetime': False})
                if not loader_reloaded:
                    await self.middleware.call('service.reload', 'loader', {'onetime': False})
                    loader_reloaded = True
            elif (
                original_data['serialspeed'] != config_data['serialspeed'] or
                original_data['serialport'] != config_data['serialport']
            ):
                if not loader_reloaded:
                    await self.middleware.call('service.reload', 'loader', {'onetime': False})
                    loader_reloaded = True

            if original_data['autotune'] != config_data['autotune']:
                if not loader_reloaded:
                    await self.middleware.call('service.reload', 'loader', {'onetime': False})
                    loader_reloaded = True
                await self.middleware.call('system.advanced.autotune', 'loader')
                await self.middleware.call('system.advanced.autotune', 'sysctl')

            if (
                original_data['debugkernel'] != config_data['debugkernel'] and
                not loader_reloaded
            ):
                await self.middleware.call('service.reload', 'loader', {'onetime': False})

            if original_data['fqdn_syslog'] != config_data['fqdn_syslog']:
                await self.middleware.call('service.restart', 'syslogd', {'onetime': False})

            if (
                original_data['sysloglevel'].lower() != config_data['sysloglevel'].lower() or
                original_data['syslogserver'] != config_data['syslogserver'] or
                original_data['syslog_transport'] != config_data['syslog_transport'] or
                original_data['syslog_tls_certificate'] != config_data['syslog_tls_certificate']
            ):
                await self.middleware.call('service.restart', 'syslogd')

            if config_data['sed_passwd'] and original_data['sed_passwd'] != config_data['sed_passwd']:
                await self.middleware.call('kmip.sync_sed_keys')

        return await self.config()

    @accepts()
    async def sed_global_password(self):
        """
        Returns configured global SED password.
        """
        passwd = (await self.middleware.call(
            'datastore.config', 'system.advanced', {'prefix': self._config.datastore_prefix}
        ))['sed_passwd']
        return passwd if passwd else await self.middleware.call('kmip.sed_global_password')

    @private
    def autotune(self, conf='loader'):
        if b'/' not in subprocess.run(['whereis', 'autotune'], capture_output=True).stdout:
            return
        if self.middleware.call_sync('system.product_type') == 'CORE':
            kernel_reserved = 1073741824
            userland_reserved = 2417483648
        else:
            kernel_reserved = 6442450944
            userland_reserved = 4831838208
        cp = subprocess.run(
            [
                'autotune', '-o', f'--kernel-reserved={kernel_reserved}',
                f'--userland-reserved={userland_reserved}', '--conf', conf
            ], capture_output=True
        )
        if cp.returncode != 0:
            self.logger.warn('autotune for [%s] failed with error: [%s].',
                             conf, cp.stderr.decode())
        return cp.returncode


class SystemService(Service):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__product_type = None

    @no_auth_required
    @accepts()
    async def is_freenas(self):
        """
        FreeNAS is now TrueNAS CORE.

        DEPRECATED: Use `system.product_type`
        """
        return (await self.product_type()) == 'CORE'

    @no_auth_required
    @accepts()
    async def product_type(self):
        """
        Returns the type of the product.

        CORE - TrueNAS Core, community version
        ENTERPRISE - TrueNAS Enterprise, appliance version
        """
        if self.__product_type is None:
            license = await self.middleware.run_in_thread(self._get_license)
            self.__product_type = 'CORE' if (
                not license or
                license['model'].lower().startswith('freenas')
            ) else 'ENTERPRISE'
        return self.__product_type

    @no_auth_required
    @accepts()
    async def product_name(self):
        """
        Returns name of the product we are using.
        """
        return "TrueNAS"

    @accepts()
    def version(self):
        """
        Returns software version of the system.
        """
        return sw_version()

    @accepts()
    async def boot_id(self):
        """
        Returns an unique boot identifier.

        It is supposed to be unique every system boot.
        """
        return SYSTEM_BOOT_ID

    @no_auth_required
    @accepts()
    async def environment(self):
        """
        Return environment in which product is running. Possible values:
        - DEFAULT
        - EC2
        """
        if os.path.exists("/.ec2"):
            return "EC2"

        return "DEFAULT"

    @private
    async def platform(self):
        return osc.SYSTEM

    @accepts()
    async def ready(self):
        """
        Returns whether the system completed boot and is ready to use
        """
        return await self.middleware.call("system.state") != "BOOTING"

    @accepts()
    async def state(self):
        """
        Returns system state:
        "BOOTING" - System is booting
        "READY" - System completed boot and is ready to use
        "SHUTTING_DOWN" - System is shutting down
        """
        if SYSTEM_SHUTTING_DOWN:
            return "SHUTTING_DOWN"
        if SYSTEM_READY:
            return "READY"
        return "BOOTING"

    @staticmethod
    def _get_license():
        if not os.path.exists(LICENSE_FILE):
            return

        with open(LICENSE_FILE, 'r') as f:
            license_file = f.read().strip('\n')

        try:
            licenseobj = License.load(license_file)
        except Exception:
            return

        license = {
            "model": licenseobj.model,
            "system_serial": licenseobj.system_serial,
            "system_serial_ha": licenseobj.system_serial_ha,
            "contract_type": ContractType(licenseobj.contract_type).name.upper(),
            "contract_start": licenseobj.contract_start,
            "contract_end": licenseobj.contract_end,
            "legacy_contract_hardware": (
                licenseobj.contract_hardware.name.upper()
                if licenseobj.contract_type == ContractType.legacy
                else None
            ),
            "legacy_contract_software": (
                licenseobj.contract_software.name.upper()
                if licenseobj.contract_type == ContractType.legacy
                else None
            ),
            "customer_name": licenseobj.customer_name,
            "expired": licenseobj.expired,
            "features": [],
            "addhw": licenseobj.addhw,
        }
        for feature in licenseobj.features:
            license["features"].append(feature.name.upper())
        # Licenses issued before 2017-04-14 had a bug in the feature bit
        # for fibre channel, which means they were issued having
        # dedup+jails instead.
        if (
            licenseobj.contract_start < date(2017, 4, 14) and
            Features.dedup in licenseobj.features and
            Features.jails in licenseobj.features
        ):
            license["features"].append(Features.fibrechannel.name.upper())
        return license

    @private
    def license_path(self):
        return LICENSE_FILE

    @accepts(Str('license'))
    def license_update(self, license):
        """
        Update license file.
        """
        try:
            License.load(license)
        except Exception:
            raise CallError('This is not a valid license.')

        with open(LICENSE_FILE, 'w+') as f:
            f.write(license)

        self.middleware.call_sync('etc.generate', 'rc')

        self.__product_type = None
        self.middleware.run_coroutine(
            self.middleware.call_hook('system.post_license_update'), wait=False,
        )

    @accepts()
    async def info(self):
        """
        Returns basic system information.
        """
        buildtime = sw_buildtime()
        if buildtime:
            buildtime = datetime.fromtimestamp(int(buildtime)),

        uptime = (await (await Popen(
            ['env', '-u', 'TZ', 'uptime'], stdout=subprocess.PIPE
        )).communicate())[0].decode().split(',')
        uptime = ', '.join([uptime[i].strip() for i in range(2)])

        serial = await self._system_serial()

        product = (await(await Popen(
            ['dmidecode', '-s', 'system-product-name'],
            stdout=subprocess.PIPE,
        )).communicate())[0].decode().strip() or None

        manufacturer = (await(await Popen(
            ['dmidecode', '-s', 'system-manufacturer'],
            stdout=subprocess.PIPE,
        )).communicate())[0].decode().strip() or None

        # https://superuser.com/questions/893560/how-do-i-tell-if-my-memory-is-ecc-or-non-ecc/893569#893569
        ecc_results = RE_MEMWIDTH.findall((await run(['dmidecode', '-t', '17'])).stdout.decode())

        return {
            'version': self.version(),
            'buildtime': buildtime,
            'hostname': socket.gethostname(),
            'physmem': psutil.virtual_memory().total,
            'model': osc.get_cpu_model(),
            'cores': psutil.cpu_count(logical=True),
            'loadavg': os.getloadavg(),
            'uptime': uptime,
            'uptime_seconds': time.time() - psutil.boot_time(),
            'system_serial': serial,
            'system_product': product,
            'license': await self.middleware.run_in_thread(self._get_license),
            'boottime': datetime.fromtimestamp(psutil.boot_time()),
            'datetime': datetime.utcnow(),
            'timezone': (await self.middleware.call('datastore.config', 'system.settings'))['stg_timezone'],
            'system_manufacturer': manufacturer,
            'ecc_memory': all(dimm_module.strip() == '72' for dimm_module in (ecc_results or ['']))
        }

    @accepts(Str('feature', enum=['DEDUP', 'FIBRECHANNEL', 'JAILS', 'VM']))
    async def feature_enabled(self, name):
        """
        Returns whether the `feature` is enabled or not
        """
        is_core = (await self.middleware.call('system.product_type')) == 'CORE'
        if name == 'FIBRECHANNEL' and is_core:
            return False
        elif is_core:
            return True
        license = await self.middleware.run_in_thread(self._get_license)
        if license and name in license['features']:
            return True
        return False

    @private
    async def _system_serial(self):
        return (await(await Popen(
            ['dmidecode', '-s', 'system-serial-number'],
            stdout=subprocess.PIPE,
        )).communicate())[0].decode().strip() or None

    @accepts(Dict('system-reboot', Int('delay', required=False), required=False))
    @job()
    async def reboot(self, job, options=None):
        """
        Reboots the operating system.

        Emits an "added" event of name "system" and id "reboot".
        """
        if options is None:
            options = {}

        self.middleware.send_event('system', 'ADDED', id='reboot', fields={
            'description': 'System is going to reboot',
        })

        delay = options.get('delay')
        if delay:
            await asyncio.sleep(delay)

        await Popen(['/sbin/shutdown', '-r', 'now'])

    @accepts(Dict('system-shutdown', Int('delay', required=False), required=False))
    @job()
    async def shutdown(self, job, options=None):
        """
        Shuts down the operating system.

        An "added" event of name "system" and id "shutdown" is emitted when shutdown is initiated.
        """
        if options is None:
            options = {}

        delay = options.get('delay')
        if delay:
            await asyncio.sleep(delay)

        await Popen(['/sbin/poweroff'])

    @private
    @job(lock='system.debug_generate')
    def debug_generate(self, job):
        """
        Generate system debug file.

        Result value will be the absolute path of the file.
        """
        system_dataset_path = self.middleware.call_sync('systemdataset.config')['path']
        if system_dataset_path is not None:
            direc = os.path.join(system_dataset_path, 'ixdiagnose')
        else:
            direc = '/var/tmp/ixdiagnose'
        dump = os.path.join(direc, 'ixdiagnose.tgz')

        # Be extra safe in case we have left over from previous run
        if os.path.exists(direc):
            shutil.rmtree(direc)

        cp = subprocess.Popen(
            ['ixdiagnose', '-d', direc, '-s', '-F', '-p'],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=1
        )

        for line in iter(cp.stdout.readline, ''):
            line = line.rstrip()

            if line.startswith('**'):
                percent, help = line.split(':')
                job.set_progress(
                    int(percent.split()[-1].strip('%')),
                    help.lstrip()
                )
        _, stderr = cp.communicate()

        if cp.returncode != 0:
            raise CallError(f'Failed to generate debug file: {stderr}')

        job.set_progress(100, 'Debug generation finished')

        return dump

    @accepts()
    @job(lock='system.debug', pipes=['output'])
    def debug(self, job):
        """
        Job to stream debug file.

        This method is meant to be used in conjuntion with `core.download` to get the debug
        downloaded via HTTP.
        """
        job.set_progress(0, 'Generating debug file')
        debug_job = self.middleware.call_sync(
            'system.debug_generate',
            job_on_progress_cb=lambda encoded: job.set_progress(int(encoded['progress']['percent'] * 0.9),
                                                                encoded['progress']['description'])
        )

        standby_debug = None
        if self.middleware.call_sync('failover.licensed'):
            try:
                standby_debug = self.middleware.call_sync(
                    'failover.call_remote', 'system.debug_generate', [], {'job': True}
                )
            except Exception:
                self.logger.warn('Failed to get debug from standby node', exc_info=True)
            else:
                remote_ip = self.middleware.call_sync('failover.remote_ip')
                url = self.middleware.call_sync(
                    'failover.call_remote', 'core.download', ['filesystem.get', [standby_debug], 'debug.txz'],
                )[1]

                url = f'http://{remote_ip}:6000{url}'
                standby_debug = io.BytesIO()
                with requests.get(url, stream=True) as r:
                    for i in r.iter_content(chunk_size=1048576):
                        if standby_debug.tell() > 20971520:
                            raise CallError(f'Standby debug file is bigger than 20MiB.')
                        standby_debug.write(i)

        debug_job.wait_sync()
        if debug_job.error:
            raise CallError(debug_job.error)

        job.set_progress(90, 'Preparing debug file for streaming')

        if standby_debug:
            # Debug file cannot be big on HA because we put both debugs in memory
            # so they can be downloaded at once.
            try:
                if os.stat(debug_job.result).st_size > 20971520:
                    raise CallError(f'Debug file is bigger than 20MiB.')
            except FileNotFoundError:
                raise CallError('Debug file was not found, try again.')

            network = self.middleware.call_sync('network.configuration.config')
            node = self.middleware.call_sync('failover.node')

            tario = io.BytesIO()
            with tarfile.open(fileobj=tario, mode='w') as tar:

                if node == 'A':
                    my_hostname = network['hostname']
                    remote_hostname = network['hostname_b']
                else:
                    my_hostname = network['hostname_b']
                    remote_hostname = network['hostname']

                tar.add(debug_job.result, f'{my_hostname}.txz')

                tarinfo = tarfile.TarInfo(f'{remote_hostname}.txz')
                tarinfo.size = standby_debug.tell()
                standby_debug.seek(0)
                tar.addfile(tarinfo, fileobj=standby_debug)

            tario.seek(0)
            shutil.copyfileobj(tario, job.pipes.output.w)
        else:
            with open(debug_job.result, 'rb') as f:
                shutil.copyfileobj(f, job.pipes.output.w)
        job.pipes.output.w.close()


class SystemGeneralModel(sa.Model):
    __tablename__ = 'system_settings'

    id = sa.Column(sa.Integer(), primary_key=True)
    stg_guiaddress = sa.Column(sa.JSON(type=list), default=['0.0.0.0'])
    stg_guiv6address = sa.Column(sa.JSON(type=list), default=['::'])
    stg_guiport = sa.Column(sa.Integer(), default=80)
    stg_guihttpsport = sa.Column(sa.Integer(), default=443)
    stg_guihttpsredirect = sa.Column(sa.Boolean(), default=False)
    stg_language = sa.Column(sa.String(120), default="en")
    stg_kbdmap = sa.Column(sa.String(120))
    stg_timezone = sa.Column(sa.String(120), default="America/Los_Angeles")
    stg_wizardshown = sa.Column(sa.Boolean(), default=False)
    stg_pwenc_check = sa.Column(sa.String(100))
    stg_guicertificate_id = sa.Column(sa.ForeignKey('system_certificate.id'), index=True, nullable=True)
    stg_crash_reporting = sa.Column(sa.Boolean(), nullable=True)
    stg_usage_collection = sa.Column(sa.Boolean(), nullable=True)
    stg_guihttpsprotocols = sa.Column(sa.JSON(type=list), default=['TLSv1', 'TLSv1.1', 'TLSv1.2', 'TLSv1.3'])


class SystemGeneralService(ConfigService):
    HTTPS_PROTOCOLS = ['TLSv1', 'TLSv1.1', 'TLSv1.2', 'TLSv1.3']

    class Config:
        namespace = 'system.general'
        datastore = 'system.settings'
        datastore_prefix = 'stg_'
        datastore_extend = 'system.general.general_system_extend'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._language_choices = self._initialize_languages()
        self._timezone_choices = None
        self._kbdmap_choices = None
        self._country_choices = {}

    @private
    async def general_system_extend(self, data):
        for key in list(data.keys()):
            if key.startswith('gui'):
                data['ui_' + key[3:]] = data.pop(key)

        if data['ui_certificate']:
            data['ui_certificate'] = await self.middleware.call(
                'certificate.query',
                [['id', '=', data['ui_certificate']['id']]],
                {'get': True}
            )

        is_core = (await self.middleware.call('system.product_type')) == 'CORE'
        data['crash_reporting_is_set'] = data['crash_reporting'] is not None
        if data['crash_reporting'] is None:
            data['crash_reporting'] = is_core

        data['usage_collection_is_set'] = data['usage_collection'] is not None
        if data['usage_collection'] is None:
            data['usage_collection'] = is_core

        data.pop('pwenc_check')

        return data

    @accepts()
    async def ui_address_choices(self):
        """
        Returns UI ipv4 address choices.
        """
        return {
            d['address']: d['address'] for d in await self.middleware.call(
                'interface.ip_in_use', {'ipv4': True, 'ipv6': False, 'any': True, 'static': True}
            )
        }

    @accepts()
    async def ui_v6address_choices(self):
        """
        Returns UI ipv6 address choices.
        """
        return {
            d['address']: d['address'] for d in await self.middleware.call(
                'interface.ip_in_use', {'ipv4': False, 'ipv6': True, 'any': True, 'static': True}
            )
        }

    @accepts()
    def ui_httpsprotocols_choices(self):
        """
        Returns available HTTPS protocols.
        """
        return dict(zip(self.HTTPS_PROTOCOLS, self.HTTPS_PROTOCOLS))

    @accepts()
    def language_choices(self):
        """
        Returns language choices.
        """
        return self._language_choices

    @private
    def _initialize_languages(self):
        languagues = [
            ('af', 'Afrikaans'),
            ('ar', 'Arabic'),
            ('ast', 'Asturian'),
            ('az', 'Azerbaijani'),
            ('bg', 'Bulgarian'),
            ('be', 'Belarusian'),
            ('bn', 'Bengali'),
            ('br', 'Breton'),
            ('bs', 'Bosnian'),
            ('ca', 'Catalan'),
            ('cs', 'Czech'),
            ('cy', 'Welsh'),
            ('da', 'Danish'),
            ('de', 'German'),
            ('dsb', 'Lower Sorbian'),
            ('el', 'Greek'),
            ('en', 'English'),
            ('en-au', 'Australian English'),
            ('en-gb', 'British English'),
            ('eo', 'Esperanto'),
            ('es', 'Spanish'),
            ('es-ar', 'Argentinian Spanish'),
            ('es-co', 'Colombian Spanish'),
            ('es-mx', 'Mexican Spanish'),
            ('es-ni', 'Nicaraguan Spanish'),
            ('es-ve', 'Venezuelan Spanish'),
            ('et', 'Estonian'),
            ('eu', 'Basque'),
            ('fa', 'Persian'),
            ('fi', 'Finnish'),
            ('fr', 'French'),
            ('fy', 'Frisian'),
            ('ga', 'Irish'),
            ('gd', 'Scottish Gaelic'),
            ('gl', 'Galician'),
            ('he', 'Hebrew'),
            ('hi', 'Hindi'),
            ('hr', 'Croatian'),
            ('hsb', 'Upper Sorbian'),
            ('hu', 'Hungarian'),
            ('ia', 'Interlingua'),
            ('id', 'Indonesian'),
            ('io', 'Ido'),
            ('is', 'Icelandic'),
            ('it', 'Italian'),
            ('ja', 'Japanese'),
            ('ka', 'Georgian'),
            ('kab', 'Kabyle'),
            ('kk', 'Kazakh'),
            ('km', 'Khmer'),
            ('kn', 'Kannada'),
            ('ko', 'Korean'),
            ('lb', 'Luxembourgish'),
            ('lt', 'Lithuanian'),
            ('lv', 'Latvian'),
            ('mk', 'Macedonian'),
            ('ml', 'Malayalam'),
            ('mn', 'Mongolian'),
            ('mr', 'Marathi'),
            ('my', 'Burmese'),
            ('nb', 'Norwegian Bokmål'),
            ('ne', 'Nepali'),
            ('nl', 'Dutch'),
            ('nn', 'Norwegian Nynorsk'),
            ('os', 'Ossetic'),
            ('pa', 'Punjabi'),
            ('pl', 'Polish'),
            ('pt', 'Portuguese'),
            ('pt-br', 'Brazilian Portuguese'),
            ('ro', 'Romanian'),
            ('ru', 'Russian'),
            ('sk', 'Slovak'),
            ('sl', 'Slovenian'),
            ('sq', 'Albanian'),
            ('sr', 'Serbian'),
            ('sr-latn', 'Serbian Latin'),
            ('sv', 'Swedish'),
            ('sw', 'Swahili'),
            ('ta', 'Tamil'),
            ('te', 'Telugu'),
            ('th', 'Thai'),
            ('tr', 'Turkish'),
            ('tt', 'Tatar'),
            ('udm', 'Udmurt'),
            ('uk', 'Ukrainian'),
            ('ur', 'Urdu'),
            ('vi', 'Vietnamese'),
            ('zh-hans', 'Simplified Chinese'),
            ('zh-hant', 'Traditional Chinese'),
        ]
        return dict(languagues)

    @private
    async def _initialize_timezone_choices(self):
        pipe = await Popen(
            'find /usr/share/zoneinfo/ -type f -not -name zone.tab -not -regex \'.*/Etc/GMT.*\'',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True
        )
        self._timezone_choices = (await pipe.communicate())[0].decode().strip().split('\n')
        self._timezone_choices = {x[20:]: x[20:] for x in self._timezone_choices}

    @accepts()
    async def timezone_choices(self):
        """
        Returns time zone choices.
        """
        if not self._timezone_choices:
            await self._initialize_timezone_choices()
        return self._timezone_choices

    @accepts()
    async def country_choices(self):
        """
        Returns country choices.
        """
        if not self._country_choices:
            await self._initialize_country_choices()
        return self._country_choices

    @private
    async def _initialize_country_choices(self):

        def _get_index(country_columns, column):
            index = -1

            i = 0
            for c in country_columns:
                if c.lower() == column.lower():
                    index = i
                    break

                i += 1

            return index

        country_file = '/etc/iso_3166_2_countries.csv'
        cni, two_li = None, None
        with open(country_file, 'r', encoding='utf-8') as csvfile:
            reader = csv.reader(csvfile)

            for index, row in enumerate(reader):
                if index != 0:
                    if row[cni] and row[two_li]:
                        if row[two_li] in self._country_choices:
                            # If two countries in the iso file have the same key, we concatenate their names
                            self._country_choices[row[two_li]] += f' + {row[cni]}'
                        else:
                            self._country_choices[row[two_li]] = row[cni]
                else:
                    # ONLY CNI AND TWO_LI ARE BEING CONSIDERED FROM THE CSV
                    cni = _get_index(row, 'Common Name')
                    two_li = _get_index(row, 'ISO 3166-1 2 Letter Code')

    @private
    async def _initialize_kbdmap_choices(self):
        """Populate choices from /usr/share/vt/keymaps/INDEX.keymaps"""
        index = "/usr/share/vt/keymaps/INDEX.keymaps"

        if not os.path.exists(index):
            return []
        with open(index, 'rb') as f:
            d = f.read().decode('utf8', 'ignore')
        _all = re.findall(r'^(?P<name>[^#\s]+?)\.kbd:en:(?P<desc>.+)$', d, re.M)
        self._kbdmap_choices = {name: desc for name, desc in _all}

    @accepts()
    async def kbdmap_choices(self):
        """
        Returns kbdmap choices.
        """
        if not self._kbdmap_choices:
            await self._initialize_kbdmap_choices()
        return self._kbdmap_choices

    @private
    async def validate_general_settings(self, data, schema):
        verrors = ValidationErrors()

        language = data.get('language')
        system_languages = self.language_choices()
        if language not in system_languages.keys():
            verrors.add(
                f'{schema}.language',
                f'Specified "{language}" language unknown. Please select a valid language.'
            )

        # kbd map needs work

        timezone = data.get('timezone')
        timezones = await self.timezone_choices()
        if timezone not in timezones:
            verrors.add(
                f'{schema}.timezone',
                'Timezone not known. Please select a valid timezone.'
            )

        ip4_addresses_list = await self.ui_address_choices()
        ip6_addresses_list = await self.ui_v6address_choices()

        ip4_addresses = data.get('ui_address')
        for ip4_address in ip4_addresses:
            if ip4_address not in ip4_addresses_list:
                verrors.add(
                    f'{schema}.ui_address',
                    f'{ip4_address} ipv4 address is not associated with this machine'
                )

        ip6_addresses = data.get('ui_v6address')
        for ip6_address in ip6_addresses:
            if ip6_address not in ip6_addresses_list:
                verrors.add(
                    f'{schema}.ui_v6address',
                    f'{ip6_address} ipv6 address is not associated with this machine'
                )

        for key, wildcard, ips in [('ui_address', '0.0.0.0', ip4_addresses), ('ui_v6address', '::', ip6_addresses)]:
            if wildcard in ips and len(ips) > 1:
                verrors.add(
                    f'{schema}.{key}',
                    f'When "{wildcard}" has been selected, selection of other addresses is not allowed'
                )

        certificate_id = data.get('ui_certificate')
        cert = await self.middleware.call(
            'certificate.query',
            [["id", "=", certificate_id]]
        )
        if not cert:
            verrors.add(
                f'{schema}.ui_certificate',
                'Please specify a valid certificate which exists in the system'
            )
        else:
            cert = cert[0]
            verrors.extend(
                await self.middleware.call(
                    'certificate.cert_services_validation', certificate_id, f'{schema}.ui_certificate', False
                )
            )

            if cert['fingerprint']:
                syslog.openlog(logoption=syslog.LOG_PID, facility=syslog.LOG_USER)
                syslog.syslog(syslog.LOG_ERR, 'Fingerprint of the certificate used in UI : ' + cert['fingerprint'])
                syslog.closelog()

        return verrors

    @accepts()
    async def ui_certificate_choices(self):
        """
        Return choices of certificates which can be used for `ui_certificate`.
        """
        return {
            i['id']: i['name']
            for i in await self.middleware.call('certificate.query', [
                ('cert_type_CSR', '=', False)
            ])
        }

    @accepts(
        Dict(
            'general_settings',
            Int('ui_certificate', null=True),
            Int('ui_httpsport', validators=[Range(min=1, max=65535)]),
            Bool('ui_httpsredirect'),
            List('ui_httpsprotocols', items=[Str('protocol', enum=HTTPS_PROTOCOLS)], empty=False, unique=True),
            Int('ui_port', validators=[Range(min=1, max=65535)]),
            List('ui_address', items=[IPAddr('addr')], empty=False),
            List('ui_v6address', items=[IPAddr('addr')], empty=False),
            Str('kbdmap'),
            Str('language', empty=False),
            Str('sysloglevel', enum=['F_EMERG', 'F_ALERT', 'F_CRIT', 'F_ERR', 'F_WARNING', 'F_NOTICE',
                                     'F_INFO', 'F_DEBUG', 'F_IS_DEBUG']),
            Str('syslogserver'),
            Str('timezone', empty=False),
            Bool('crash_reporting', null=True),
            Bool('usage_collection', null=True),
            update=True,
        )
    )
    async def do_update(self, data):
        """
        Update System General Service Configuration.

        `ui_certificate` is used to enable HTTPS access to the system. If `ui_certificate` is not configured on boot,
        it is automatically created by the system.

        `ui_httpsredirect` when set, makes sure that all HTTP requests are converted to HTTPS requests to better
        enhance security.

        `ui_address` and `ui_v6address` are a list of valid ipv4/ipv6 addresses respectively which the system will
        listen on.

        `syslogserver` and `sysloglevel` are deprecated fields as of 11.3
        and will be permanently moved to system.advanced.update for 12.0
        """
        advanced_config = {}
        # fields were moved to Advanced
        for deprecated_field in ('sysloglevel', 'syslogserver'):
            if deprecated_field in data:
                warnings.warn(
                    f"{deprecated_field} has been deprecated and moved to 'system.advanced'",
                    DeprecationWarning
                )
                advanced_config[deprecated_field] = data[deprecated_field]
                del data[deprecated_field]
        if advanced_config:
            await self.middleware.call('system.advanced.update', advanced_config)

        config = await self.config()
        config['ui_certificate'] = config['ui_certificate']['id'] if config['ui_certificate'] else None
        if not config.pop('crash_reporting_is_set'):
            config['crash_reporting'] = None
        if not config.pop('usage_collection_is_set'):
            config['usage_collection'] = None
        new_config = config.copy()
        new_config.update(data)

        verrors = await self.validate_general_settings(new_config, 'general_settings_update')
        if verrors:
            raise verrors

        keys = new_config.keys()
        for key in list(keys):
            if key.startswith('ui_'):
                new_config['gui' + key[3:]] = new_config.pop(key)

        await self.middleware.call(
            'datastore.update',
            self._config.datastore,
            config['id'],
            new_config,
            {'prefix': 'stg_'}
        )

        if config['kbdmap'] != new_config['kbdmap']:
            await self.middleware.call('service.restart', 'syscons')

        if config['timezone'] != new_config['timezone']:
            await self.middleware.call('zettarepl.update_timezone', new_config['timezone'])
            await self.middleware.call('service.reload', 'timeservices')
            await self.middleware.call('service.restart', 'cron')

        if config['language'] != new_config['language']:
            await self.middleware.call('system.general.set_language')

        if config['crash_reporting'] != new_config['crash_reporting']:
            await self.middleware.call('system.general.set_crash_reporting')

        await self.middleware.call('service.start', 'ssl')

        return await self.config()

    @accepts()
    async def ui_restart(self):
        """
        Restart HTTP server to use latest UI settings.
        """
        await self.middleware.call('service.restart', 'http')

    @accepts()
    async def local_url(self):
        """
        Returns configured local url in the format of protocol://host:port
        """
        config = await self.middleware.call('system.general.config')

        if config['ui_certificate']:
            protocol = 'https'
            port = config['ui_httpsport']
        else:
            protocol = 'http'
            port = config['ui_port']

        if '0.0.0.0' in config['ui_address'] or '127.0.0.1' in config['ui_address']:
            hosts = ['127.0.0.1']
        else:
            hosts = config['ui_address']

        errors = []
        for host in hosts:
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(
                    host,
                    port=port,
                ), timeout=5)
                writer.close()

                return f'{protocol}://{host}:{port}'

            except Exception as e:
                errors.append(f'{host}: {e}')

        raise CallError('Unable to connect to any of the specified UI addresses:\n' + '\n'.join(errors))

    def __get_urls(self, aliases, addrs, ipv6=False):

        skip_internal = False
        if self.middleware.call_sync('system.product_type') == 'ENTERPRISE':
            skip_internal = True

        urls = []
        for addr in addrs:
            ip, port = addr.split(':')

            if ip == '*':
                ips = [
                    i["address"]
                    for i in aliases
                    if i['type'] == ('INET6' if ipv6 else 'INET')
                ]
            else:
                ips = [ip]

            for o in ips:
                if skip_internal and o in (
                    '169.254.10.1',
                    '169.254.10.2',
                    '169.254.10.20',
                    '169.254.10.80',
                ):
                    continue

                if ipv6 and '%' in o:
                    o = o.split('%')[0]

                if ipv6:
                    url = f'http://[{o}]'
                else:
                    url = f'http://{o}'
                if port != '80':
                    url = f'{url}:{port}'
                try:
                    r = requests.head(url, timeout=10)
                    assert r.status_code in (200, 302, 301)
                    urls.append(url)
                    continue
                except Exception:
                    pass

                if ipv6:
                    url = f'https://[{o}]'
                else:
                    url = f'https://{o}'
                if port != '443':
                    url = f'{url}:{port}'
                try:
                    r = requests.head(url, timeout=15, verify=False)
                    assert r.status_code in (200, 302)
                    urls.append(url)
                    continue
                except Exception:
                    pass
        return urls

    @private
    def get_ui_urls(self):
        addrsv4 = []
        addrsv6 = []
        aliases = []
        for i in self.middleware.call_sync('interface.query'):
            if not i['state'] or not i['state']['aliases']:
                continue
            aliases += list(filter(lambda x: x['type'].startswith('INET'), i['state']['aliases']))

        cp = subprocess.run(
            'sockstat -46{} tcp |awk \'{{ if ($2 == "nginx" && ${} == "*:*") print ${}","${} }}\' | '
            'sort | uniq'.format(
                # Linux sockstat uses -R for protocol selection
                # FreeBSD uses -P instead.
                *(('R', '6', '4', '5') if IS_LINUX else ('P', '7', '5', '6'))
            ),
            shell=True, capture_output=True, text=True,
        )
        for line in cp.stdout.strip('\n').split('\n'):
            _type, addr = line.split(',')

            if _type == 'tcp4':
                addrsv4.append(addr)
            else:
                addrsv6.append(addr)

        urls = []
        if addrsv4:
            urls += self.__get_urls(aliases, addrsv4)
        if addrsv6:
            urls += self.__get_urls(aliases, addrsv6, ipv6=True)
        return sorted(urls)

    @private
    def set_language(self):
        language = self.middleware.call_sync('system.general.config')['language']
        set_language(language)

    @private
    def set_crash_reporting(self):
        CrashReporting.enabled_in_settings = self.middleware.call_sync('system.general.config')['crash_reporting']


async def _event_system(middleware, event_type, args):
    global SYSTEM_READY
    global SYSTEM_SHUTTING_DOWN
    if args['id'] == 'ready':
        SYSTEM_READY = True
    if args['id'] == 'shutdown':
        SYSTEM_SHUTTING_DOWN = True


class SystemHealthEventSource(EventSource):

    """
    Notifies of current system health which include statistics about consumption of memory and CPU, pools and
    if updates are available. An integer `delay` argument can be specified to determine the delay
    on when the periodic event should be generated.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._check_update = None
        start_daemon_thread(target=self.check_update)

    def check_update(self):
        while not self._cancel.is_set():
            try:
                self._check_update = self.middleware.call_sync('update.check_available')['status']
                self._cancel.wait(timeout=60 * 60 * 24)
            except Exception:
                self.middleware.logger.warn(
                    'Failed to check avaiable update for system.health event', exc_info=True,
                )

    def pools_statuses(self):
        return {
            p['name']: {'status': p['status']}
            for p in self.middleware.call_sync('pool.query')
        }

    def run(self):

        try:
            if self.arg:
                delay = int(self.arg)
            else:
                delay = 10
        except ValueError:
            return

        # Delay too slow
        if delay < 5:
            return

        cp_time = sysctl.filter('kern.cp_time')[0].value
        cp_old = cp_time

        while not self._cancel.is_set():
            time.sleep(delay)

            cp_time = sysctl.filter('kern.cp_time')[0].value
            cp_diff = list(map(lambda x: x[0] - x[1], zip(cp_time, cp_old)))
            cp_old = cp_time

            cpu_percent = round((sum(cp_diff[:3]) / sum(cp_diff)) * 100, 2)

            pools = self.middleware.call_sync(
                'cache.get_or_put',
                CACHE_POOLS_STATUSES,
                1800,
                self.pools_statuses,
            )

            self.send_event('ADDED', fields={
                'cpu_percent': cpu_percent,
                'memory': psutil.virtual_memory()._asdict(),
                'pools': pools,
                'update': self._check_update,
            })


async def firstboot(middleware):
    if os.path.exists(FIRST_INSTALL_SENTINEL):
        # Delete sentinel file before making clone as we
        # we do not want the clone to have the file in it.
        os.unlink(FIRST_INSTALL_SENTINEL)

        # Creating pristine boot environment from the "default"
        middleware.logger.info("Creating 'Initial-Install' boot environment...")
        cp = await run('beadm', 'create', '-e', 'default', 'Initial-Install', check=False)
        if cp.returncode != 0:
            middleware.logger.error(
                'Failed to create initial boot environment: %s', cp.stderr.decode()
            )
        else:
            boot_pool = await middleware.call('boot.pool_name')
            cp = await run('zfs', 'set', 'beadm:keep=True', os.path.join(boot_pool, 'ROOT/Initial-Install'))
            if cp.returncode != 0:
                middleware.logger.error(
                    f'Failed to set "beadm:keep=True" for Initial-Install boot environment: {cp.stderr}'
                )


async def update_timeout_value(middleware, *args):
    if not await middleware.call(
        'tunable.query', [
            ['var', '=', 'kern.init_shutdown_timeout'],
            ['type', '=', 'SYSCTL'],
            ['enabled', '=', True]
        ]
    ):
        # Default 120 seconds is being added to scripts timeout to ensure other
        # system related scripts can execute safely within the default timeout
        initial_timeout_value = 120
        timeout_value = sum(
            list(
                map(
                    lambda i: i['timeout'],
                    await middleware.call(
                        'initshutdownscript.query', [
                            ['enabled', '=', True],
                            ['when', '=', 'SHUTDOWN']
                        ]
                    )
                )
            )
        )

        vm_timeout = (await middleware.call('vm.terminate_timeout'))
        if vm_timeout > timeout_value:
            # VM's and init tasks are executed asynchronously - so if VM timeout is greater then init tasks one,
            # we use that, else init tasks timeout is good enough to ensure VM's cleanly exit
            timeout_value = vm_timeout

        timeout_value += initial_timeout_value

        await middleware.run_in_thread(
            lambda: setattr(
                sysctl.filter('kern.init_shutdown_timeout')[0], 'value', timeout_value
            )
        )


async def setup(middleware):
    global SYSTEM_BOOT_ID, SYSTEM_READY

    SYSTEM_BOOT_ID = str(uuid.uuid4())

    middleware.event_register('system', textwrap.dedent('''\
        Sent on system state changes.

        id=ready -- Finished boot process\n
        id=reboot -- Started reboot process\n
        id=shutdown -- Started shutdown process'''))

    if os.path.exists("/tmp/.bootready"):
        SYSTEM_READY = True
    else:
        autotune_rv = await middleware.call('system.advanced.autotune', 'loader')

        if os.path.exists('/usr/local/sbin/beadm'):
            await firstboot(middleware)

        if autotune_rv == 2:
            await run('shutdown', '-r', 'now', check=False)

    settings = await middleware.call(
        'system.general.config',
    )
    os.environ['TZ'] = settings['timezone']
    time.tzset()

    middleware.logger.debug(f'Timezone set to {settings["timezone"]}')

    await middleware.call('system.general.set_language')
    await middleware.call('system.general.set_crash_reporting')

    asyncio.ensure_future(middleware.call('system.advanced.autotune', 'sysctl'))

    if sysctl:
        await update_timeout_value(middleware)

    for srv in ['initshutdownscript', 'tunable', 'vm']:
        for event in ('create', 'update', 'delete'):
            middleware.register_hook(
                f'{srv}.post_{event}',
                update_timeout_value
            )

    middleware.event_subscribe('system', _event_system)
    middleware.register_event_source('system.health', SystemHealthEventSource)

    # watchdog 38 = ~256 seconds or ~4 minutes, see sys/watchdog.h for explanation
    for command in [
        'ddb script "kdb.enter.break=watchdog 38; capture on"',
        'ddb script "kdb.enter.sysctl=watchdog 38; capture on"',
        'ddb script "kdb.enter.default=write cn_mute 1; watchdog 38; capture on; bt; '
        'show allpcpu; ps; alltrace; write cn_mute 0; textdump dump; reset"',
        'sysctl debug.ddb.textdump.pending=1',
        'sysctl debug.debugger_on_panic=1',
        'sysctl debug.ddb.capture.bufsize=4194304'
    ] if not IS_LINUX else []:  # TODO: See reasonable linux alternative
        ret = await Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        await ret.communicate()

        if ret.returncode:
            middleware.logger.debug(f'Failed to execute: {command}')

    CRASH_DIR = '/data/crash'
    os.makedirs(CRASH_DIR, exist_ok=True)
    os.chmod(CRASH_DIR, 0o775)
