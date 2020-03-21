import re

try:
    import cam
except ImportError:
    cam = None

from middlewared.common.smart.smartctl import SMARTCTL_POWERMODES
from middlewared.service import accepts, List, private, Service, Str
from middlewared.utils.asyncio_ import asyncio_map


def get_temperature(stdout):
    # ataprint.cpp

    data = {}
    for s in re.findall(r'^((190|194) .+)', stdout, re.M):
        s = s[0].split()
        try:
            data[s[1]] = int(s[9])
        except (IndexError, ValueError):
            pass
    for k in ['Temperature_Celsius', 'Temperature_Internal', 'Drive_Temperature',
              'Temperature_Case', 'Case_Temperature', 'Airflow_Temperature_Cel']:
        if k in data:
            return data[k]

    reg = re.search(r'194\s+Temperature_Celsius[^\n]*', stdout, re.M)
    if reg:
        return int(reg.group(0).split()[9])

    # nvmeprint.cpp

    reg = re.search(r'Temperature:\s+([0-9]+) Celsius', stdout, re.M)
    if reg:
        return int(reg.group(1))

    reg = re.search(r'Temperature Sensor [0-9]+:\s+([0-9]+) Celsius', stdout, re.M)
    if reg:
        return int(reg.group(1))

    # scsiprint.cpp

    reg = re.search(r'Current Drive Temperature:\s+([0-9]+) C', stdout, re.M)
    if reg:
        return int(reg.group(1))


class DiskService(Service):
    @private
    async def disks_for_temperature_monitoring(self):
        return [
            disk['devname']
            for disk in await self.middleware.call(
                'disk.query',
                [
                    ['devname', '!=', None],
                    ['togglesmart', '=', True],
                    # Polling for disk temperature does not allow them to go to sleep automatically unless
                    # hddstandby_force is used
                    [
                        'OR', [
                            ['hddstandby', '=', 'ALWAYS ON'],
                            ['hddstandby_force', '=', True],
                        ],
                    ]
                ]
            )
        ]

    @accepts(
        Str('name'),
        Str('powermode', enum=SMARTCTL_POWERMODES, default=SMARTCTL_POWERMODES[0]),
    )
    async def temperature(self, name, powermode):
        """
        Returns temperature for device `name` using specified S.M.A.R.T. `powermode`.
        """
        if name.startswith('da') and False:
            smartctl_args = await self.middleware.call('disk.smartctl_args', name) or []
            if not any(s.startswith('/dev/arcmsr') for s in smartctl_args):
                try:
                    return await self.middleware.run_in_thread(lambda: cam.CamDevice(name).get_temperature())
                except Exception:
                    pass

        output = await self.middleware.call('disk.smartctl', name, ['-a', '-n', powermode.lower()],
                                            {'silent': True})
        if output is None:
            return None

        return get_temperature(output)

    @accepts(
        List('names', items=[Str('name')]),
        Str('powermode', enum=SMARTCTL_POWERMODES, default=SMARTCTL_POWERMODES[0]),
    )
    async def temperatures(self, names, powermode):
        """
        Returns temperatures for a list of devices (runs in parallel).
        See `disk.temperature` documentation for more details.
        """
        if len(names) == 0:
            names = await self.disks_for_temperature_monitoring()

        result = dict(zip(
            names,
            await asyncio_map(lambda name: self.middleware.call('disk.temperature', name, powermode), names, 8),
        ))

        return result
