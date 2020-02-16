from middlewared.schema import Bool, Dict, Ref, Str
from middlewared.service import Service, accepts
from middlewared.plugins.smb import SMBCmd
from middlewared.utils import run, filter_list
import enum
import json


class InfoLevel(enum.Enum):
    ALL = ''
    SESSIONS = 'p'
    SHARES = 'S'
    LOCKS = 'L'
    BYTERANGE = 'B'
    NOTIFICATIONS = 'N'


class SMBService(Service):

    class Config:
        service = 'cifs'
        service_verb = 'restart'

    @accepts(
        Str('info_level', enum=[x.name for x in InfoLevel], default=InfoLevel.ALL.name),
        Ref('query-filters'),
        Ref('query-options'),
        Dict('status_options',
             Bool('verbose', default=True),
             Bool('fast', default=False),
             Str('restrict_user', default='')
             )
    )
    async def status(self, info_level, filters, options, status_options):
        """
        Returns SMB server status (sessions, open files, locks, notifications).

        `info_level` type of information requests. Defaults to ALL.

        `status_options` additional options to filter query results. Supported
        values are as follows: `verbose` gives more verbose status output
        `fast` causes smbstatus to not check if the status data is valid by
        checking if the processes that the status data refer to all still
        exist. This speeds up execution on busy systems and clusters but
        might display stale data of processes that died without cleaning up
        properly. `restrict_user` specifies the limits results to the specified
        user.
        """
        flags = '-j'
        flags = flags + InfoLevel[info_level].value
        flags = flags + 'v' if status_options['verbose'] else flags
        flags = flags + 'f' if status_options['fast'] else flags

        statuscmd = [SMBCmd.STATUS.value, '-d' '0', flags]

        if status_options['restrict_user']:
            statuscmd.extend(['-U', status_options['restrict_user']])

        smbstatus = await run(statuscmd, check=False)

        if smbstatus.returncode != 0:
            self.logger.debug('smbstatus [{%s}] failed with error: ({%s})',
                              flags, smbstatus.stderr.decode().strip())

        return filter_list(json.loads(smbstatus.stdout.decode()), filters, options)
