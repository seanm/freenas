import blkid
import glob
import os
import re
import subprocess

from lxml import etree

from .device_info_base import DeviceInfoBase
from middlewared.service import private, Service

RE_DISK_SERIAL = re.compile(r'Unit serial number:\s*(.*)')
RE_SERIAL = re.compile(r'state.*=\s*(\w*).*io (.*)-(\w*)\n.*', re.S | re.A)
RE_UART_TYPE = re.compile(r'is a\s*(\w+)')


class DeviceService(Service, DeviceInfoBase):

    def get_serials(self):
        devices = []
        for tty in map(lambda t: os.path.basename(t), glob.glob('/dev/ttyS*')):
            # We want to filter out platform based serial devices here
            serial_dev = self.serial_port_default.copy()
            tty_sys_path = os.path.join('/sys/class/tty', tty)
            dev_path = os.path.join(tty_sys_path, 'device')
            if (
                os.path.exists(dev_path) and os.path.basename(
                    os.path.realpath(os.path.join(dev_path, 'subsystem'))
                ) == 'platform'
            ) or not os.path.exists(dev_path):
                continue

            cp = subprocess.Popen(
                ['setserial', '-b', os.path.join('/dev', tty)], stderr=subprocess.DEVNULL, stdout=subprocess.PIPE
            )
            stdout, stderr = cp.communicate()
            if not cp.returncode and stdout:
                reg = RE_UART_TYPE.search(stdout.decode())
                if reg:
                    serial_dev['description'] = reg.group(1)
            if not serial_dev['description']:
                continue
            with open(os.path.join(tty_sys_path, 'device/resources'), 'r') as f:
                reg = RE_SERIAL.search(f.read())
                if reg:
                    if reg.group(1).strip() != 'active':
                        continue
                    serial_dev['start'] = reg.group(2)
                    serial_dev['size'] = (int(reg.group(3), 16) - int(reg.group(2), 16)) + 1
            with open(os.path.join(tty_sys_path, 'device/firmware_node/path'), 'r') as f:
                serial_dev['location'] = f'handle={f.read().strip()}'
            serial_dev['name'] = tty
            devices.append(serial_dev)
        return devices

    def get_disks(self):
        disks = {}
        lshw_disks = self.retrieve_lshw_disks_data()

        for block_device in blkid.list_block_devices():
            if block_device.name.startswith(('sr', 'md', 'dm-', 'loop', 'zd')):
                continue
            device_type = os.path.join('/sys/block', block_device.name, 'device/type')
            if os.path.exists(device_type):
                with open(device_type, 'r') as f:
                    if f.read().strip() != '0':
                        continue
            # nvme drives won't have this

            try:
                disks[block_device.name] = self.get_disk_details(block_device, self.disk_default.copy(), lshw_disks)
            except Exception as e:
                self.middleware.logger.debug('Failed to retrieve disk details for %s : %s', block_device.name, str(e))
        return disks

    @private
    def retrieve_lshw_disks_data(self):
        disks_cp = subprocess.Popen(
            ['lshw', '-xml', '-class', 'disk'], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        output, error = disks_cp.communicate()
        lshw_disks = {}
        if output:
            xml = etree.fromstring(output.decode())
            for child in filter(lambda c: c.get('class') == 'disk', xml.getchildren()):
                data = {'rotationrate': None}
                for c in child.getchildren():
                    if not len(c.getchildren()):
                        data[c.tag] = c.text
                    elif c.tag == 'capabilities':
                        for capability in filter(lambda d: d.text.endswith('rotations per minute'), c.getchildren()):
                            data['rotationrate'] = capability.get('id')[:-3]
                lshw_disks[data['logicalname']] = data
        return lshw_disks

    def get_disk(self, name):
        disk = self.disk_default.copy()
        try:
            block_device = blkid.BlockDevice(os.path.join('/dev', name))
        except blkid.BlkidException:
            return None

        return self.get_disk_details(block_device, disk, self.retrieve_lshw_disks_data())

    @private
    def get_disk_details(self, block_device, disk, lshw_disks):
        dev_data = block_device.__getstate__()
        disk_sys_path = os.path.join('/sys/block', block_device.name)
        driver_name = os.path.realpath(os.path.join(disk_sys_path, 'device/driver')).split('/')[-1]
        number = 0
        if driver_name != 'driver':
            number = sum(
                (ord(letter) - ord('a') + 1) * 26 ** i
                for i, letter in enumerate(reversed(dev_data['name'][len(driver_name):]))
            )
        elif dev_data['name'].startswith('nvme'):
            number = int(dev_data['name'].rsplit('n', 1)[-1])
        disk.update({
            'name': dev_data['name'],
            'sectorsize': dev_data['io_limits']['logical_sector_size'],
            'number': number,
            'subsystem': os.path.realpath(os.path.join(disk_sys_path, 'device/subsystem')).split('/')[-1],
        })
        type_path = os.path.join(disk_sys_path, 'queue/rotational')
        if os.path.exists(type_path):
            with open(type_path, 'r') as f:
                disk['type'] = 'SSD' if f.read().strip() == '0' else 'HDD'

        if block_device.path in lshw_disks:
            disk_data = lshw_disks[block_device.path]
            if disk['type'] == 'HDD':
                disk['rotationrate'] = disk_data['rotationrate']

            disk['ident'] = disk['serial'] = disk_data.get('serial', '')
            disk['size'] = disk['mediasize'] = int(disk_data['size']) if 'size' in disk_data else None
            disk['descr'] = disk['model'] = disk_data.get('product')
            if disk['size'] and disk['sectorsize']:
                disk['blocks'] = int(disk['size'] / disk['sectorsize'])

        if not disk['size'] and os.path.exists(os.path.join(disk_sys_path, 'size')):
            with open(os.path.join(disk_sys_path, 'size'), 'r') as f:
                disk['blocks'] = int(f.read().strip())
            disk['size'] = disk['mediasize'] = disk['blocks'] * disk['sectorsize']

        if not disk['serial']:
            serial_cp = subprocess.Popen(
                ['sg_vpd', '--quiet', '--page=0x80', block_device.path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            cp_stdout, cp_stderr = serial_cp.communicate()
            if not serial_cp.returncode:
                reg = RE_DISK_SERIAL.search(cp_stdout.decode().strip())
                if reg:
                    disk['serial'] = disk['ident'] = reg.group(1)

        if not disk['model'] and os.path.exists(os.path.join(disk_sys_path, 'device/model')):
            # For nvme drives, we are unable to retrieve it via lshw
            with open(os.path.join(disk_sys_path, 'device/model'), 'r') as f:
                disk['model'] = disk['descr'] = f.read().strip()

        # We make a device ID query to get DEVICE ID VPD page of the drive if available and then use that identifier
        # as the lunid - FreeBSD does the same, however it defaults to other schemes if this is unavailable
        lun_id_cp = subprocess.Popen(
            ['sg_vpd', '--quiet', '-i', block_device.path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        cp_stdout, cp_stderr = lun_id_cp.communicate()
        if not lun_id_cp.returncode and lun_id_cp.stdout:
            lunid = cp_stdout.decode().strip()
            if lunid:
                disk['lunid'] = lunid.split()[0]
            if lunid and disk['lunid'].startswith('0x'):
                disk['lunid'] = disk['lunid'][2:]

        if disk['serial'] and disk['lunid']:
            disk['serial_lunid'] = f'{disk["serial"]}_{disk["lunid"]}'

        return disk

    def get_storage_devices_topology(self):
        disks = self.get_disks()
        topology = {}
        for disk in filter(lambda d: d['subsystem'] == 'scsi', disks.values()):
            disk_path = os.path.join('/sys/block', disk['name'])
            hctl = os.path.realpath(os.path.join(disk_path, 'device')).split('/')[-1]
            if hctl.count(':') == 3:
                driver = os.path.realpath(os.path.join(disk_path, 'device/driver')).split('/')[-1]
                topology[disk['name']] = {
                    'driver': driver if driver != 'driver' else disk['subsystem'], **{
                        k: int(v) for k, v in zip(
                            ('controller_id', 'channel_no', 'target', 'lun_id'), hctl.split(':')
                        )
                    }
                }
        return topology
