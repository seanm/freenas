import asyncio
import contextlib
import glob
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile

from datetime import datetime

from middlewared.schema import Bool, Dict, accepts
from middlewared.service import CallError, Service, job, private
from middlewared.plugins.pwenc import PWENC_FILE_SECRET
from middlewared.plugins.pool import GELI_KEYPATH
from middlewared.utils import osc
from middlewared.utils.python import get_middlewared_dir

CONFIG_FILES = {
    'pwenc_secret': PWENC_FILE_SECRET,
    'geli': GELI_KEYPATH,
    'root_authorized_keys': '/root/.ssh/authorized_keys',
}
FREENAS_DATABASE = '/data/freenas-v1.db'
NEED_UPDATE_SENTINEL = '/data/need-update'
RE_CONFIG_BACKUP = re.compile(r'.*(\d{4}-\d{2}-\d{2})-(\d+)\.db$')
UPLOADED_DB_PATH = '/data/uploaded.db'


class ConfigService(Service):

    @accepts(Dict(
        'configsave',
        Bool('secretseed', default=False),
        Bool('pool_keys', default=False),
        Bool('root_authorized_keys', default=False),
    ))
    @job(pipes=["output"])
    async def save(self, job, options):
        """
        Create a bundle of security-sensitive information. These options select which information
        is included in the bundle:

        `secretseed`: include password secret seed.

        `pool_keys`: include GELI encryption keys.

        `root_authorized_keys`: include "authorized_keys" file for the root user.

        If none of these options are set, the bundle is not generated and the database file is provided.
        """

        if all(not options[k] for k in options):
            bundle = False
            filename = FREENAS_DATABASE
        else:
            bundle = True
            files = CONFIG_FILES.copy()
            if not options['secretseed']:
                files['pwenc_secret'] = None
            if not options['root_authorized_keys'] or not os.path.exists(files['root_authorized_keys']):
                files['root_authorized_keys'] = None
            if not options['pool_keys'] or not os.path.exists(files['geli']) or not os.listdir(files['geli']):
                files['geli'] = None

            filename = tempfile.mkstemp()[1]
            os.chmod(filename, 0o600)
            with tarfile.open(filename, 'w') as tar:
                tar.add(FREENAS_DATABASE, arcname='freenas-v1.db')
                for arcname, path in files.items():
                    if path:
                        tar.add(path, arcname=arcname)

        with open(filename, 'rb') as f:
            await self.middleware.run_in_thread(shutil.copyfileobj, f, job.pipes.output.w)

        if bundle:
            os.remove(filename)

    @accepts()
    @job(pipes=["input"])
    async def upload(self, job):
        """
        Accepts a configuration file via job pipe.
        """
        filename = tempfile.mktemp(dir='/var/tmp/firmware')

        def read_write():
            nreads = 0
            with open(filename, 'wb') as f_tmp:
                while True:
                    read = job.pipes.input.r.read(1024)
                    if read == b'':
                        break
                    f_tmp.write(read)
                    nreads += 1
                    if nreads > 10240:
                        # FIXME: transfer to a file on disk
                        raise ValueError('File is bigger than 10MiB')
        try:
            await self.middleware.run_in_thread(read_write)
            await self.middleware.run_in_thread(self.__upload, filename)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(filename)
        asyncio.ensure_future(self.middleware.call('system.reboot', {'delay': 10}))

    def __upload(self, config_file_name):
        try:
            """
            First we try to open the file as a tar file.
            We expect the tar file to contain at least the freenas-v1.db.
            It can also contain the pwenc_secret file.
            If we cannot open it as a tar, we try to proceed as it was the
            raw database file.
            """
            try:
                with tarfile.open(config_file_name) as tar:
                    bundle = True
                    tmpdir = tempfile.mkdtemp(dir='/var/tmp/firmware')
                    tar.extractall(path=tmpdir)
                    config_file_name = os.path.join(tmpdir, 'freenas-v1.db')
            except tarfile.ReadError:
                bundle = False
            # Currently we compare only the number of migrations for south and django
            # of new and current installed database.
            # This is not bullet proof as we can eventually have more migrations in a stable
            # release compared to a older nightly and still be considered a downgrade, however
            # this is simple enough and works in most cases.
            alembic_version = None
            conn = sqlite3.connect(config_file_name)
            try:
                cur = conn.cursor()
                try:
                    cur.execute(
                        "SELECT version_num FROM alembic_version"
                    )
                    alembic_version = cur.fetchone()[0]
                except sqlite3.OperationalError as e:
                    if e.args[0] == "no such table: alembic_version":
                        # FN/TN < 12
                        # Let's just ensure it's not a random SQLite file
                        cur.execute("SELECT 1 FROM django_migrations")
                    else:
                        raise
                finally:
                    cur.close()
            finally:
                conn.close()
            if alembic_version is not None:
                for root, dirs, files in os.walk(os.path.join(get_middlewared_dir(), "alembic", "versions")):
                    found = False
                    for name in files:
                        if name.endswith(".py"):
                            with open(os.path.join(root, name)) as f:
                                if any(
                                    line.strip() == f"Revision ID: {alembic_version}"
                                    for line in f.read().splitlines()
                                ):
                                    found = True
                                    break
                    if found:
                        break
                else:
                    raise CallError(
                        'Failed to upload config, version newer than the '
                        'current installed.'
                    )
        except Exception as e:
            os.unlink(config_file_name)
            if isinstance(e, CallError):
                raise
            else:
                raise CallError(f'The uploaded file is not valid: {e}')

        shutil.move(config_file_name, UPLOADED_DB_PATH)
        if bundle:
            for filename, destination in CONFIG_FILES.items():
                file_path = os.path.join(tmpdir, filename)
                if os.path.exists(file_path):
                    if filename == 'geli':
                        # Let's only copy the geli keys and not overwrite the entire directory
                        os.makedirs(CONFIG_FILES['geli'], exist_ok=True)
                        for key_path in os.listdir(file_path):
                            shutil.move(
                                os.path.join(file_path, key_path), os.path.join(destination, key_path)
                            )
                    elif filename == 'pwenc_secret':
                        shutil.move(file_path, '/data/pwenc_secret_uploaded')
                    else:
                        shutil.move(file_path, destination)

        # Now we must run the migrate operation in the case the db is older
        open(NEED_UPDATE_SENTINEL, 'w+').close()

        if osc.IS_LINUX:
            # For SCALE, we have to enable/disable services based on the uploaded database
            enable_disable_units = {'enable': [], 'disable': []}
            conn = sqlite3.connect(UPLOADED_DB_PATH)
            try:
                cursor = conn.cursor()
                for service, enabled in cursor.execute(
                    "SELECT srv_service, srv_enable FROM services_services"
                ).fetchall():
                    try:
                        units = self.middleware.call_sync('service.systemd_units', service)
                    except KeyError:
                        # An old service which we don't have currently
                        continue

                    if enabled:
                        enable_disable_units['enable'].extend(units)
                    else:
                        enable_disable_units['disable'].extend(units)
            finally:
                conn.close()

            for action in filter(lambda k: enable_disable_units[k], enable_disable_units):
                cp = subprocess.Popen(
                    ['systemctl', action] + enable_disable_units[action],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                err = cp.communicate()[1]
                if cp.returncode:
                    self.middleware.logger.error(
                        'Failed to %s %r systemctl units: %s', action,
                        ', '.join(enable_disable_units[action]), err.decode()
                    )

    @accepts(Dict('options', Bool('reboot', default=True)))
    @job(lock='config_reset', logs=True)
    def reset(self, job, options):
        """
        Reset database to configuration defaults.

        If `reboot` is true this job will reboot the system after its completed with a delay of 10
        seconds.
        """
        factorydb = f'{FREENAS_DATABASE}.factory'
        if os.path.exists(factorydb):
            os.unlink(factorydb)

        cp = subprocess.run(
            ['migrate93', '-f', factorydb],
            capture_output=True,
        )
        if cp.returncode != 0:
            job.logs_fd.write(cp.stderr)
            raise CallError('Factory reset has failed.')

        cp = subprocess.run(
            ['migrate113', '-f', factorydb],
            capture_output=True,
        )
        if cp.returncode != 0:
            job.logs_fd.write(cp.stderr)
            raise CallError('Factory reset has failed.')

        cp = subprocess.run(
            ['migrate'],
            env=dict(os.environ, FREENAS_DATABASE=factorydb),
            capture_output=True,
        )
        if cp.returncode != 0:
            job.logs_fd.write(cp.stderr)
            raise CallError('Factory reset has failed.')

        shutil.move(factorydb, FREENAS_DATABASE)

        if options['reboot']:
            self.middleware.run_coroutine(
                self.middleware.call('system.reboot', {'delay': 10}), wait=False,
            )

    @private
    def backup(self):
        systemdataset = self.middleware.call_sync('systemdataset.config')
        if not systemdataset or not systemdataset['path']:
            return

        # Legacy format
        for f in glob.glob(f'{systemdataset["path"]}/*.db'):
            if not RE_CONFIG_BACKUP.match(f):
                continue
            try:
                os.unlink(f)
            except OSError:
                pass

        today = datetime.now().strftime("%Y%m%d")

        newfile = os.path.join(
            systemdataset["path"],
            f'configs-{systemdataset["uuid"]}',
            self.middleware.call_sync('system.version'),
            f'{today}.db',
        )

        dirname = os.path.dirname(newfile)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        shutil.copy(FREENAS_DATABASE, newfile)
