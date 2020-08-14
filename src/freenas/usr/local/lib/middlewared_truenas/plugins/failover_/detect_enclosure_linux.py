import subprocess
import re

from middlewared.service import private, Service
from ..failover import HA_HARDWARE

ENCLOSURES_DIR = '/sys/class/enclosure/'


class EnclosureDetectionService(Service):

    class Config:
        namespace = 'failover.enclosure'

    HARDWARE = NODE = 'MANUAL'

    @private
    def detect(self):

        # Gather the PCI address for all enclosurers
        # detected by the kernel

        enclosures = self.middleware.call_sync("enclosure.list_ses_enclosures")
        if not enclosures:
            # No enclosures detected
            return self.HARDWARE, self.NODE

        for enc in enclosures:
            proc = subprocess.run(
                ['/usr/bin/sg_ses', '-p', 'ed', enc],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            if proc.stdout:
                info = proc.stdout.decode(errors='ignore', encoding='utf8')

                # Identify the Z-series Hardware (Echostream)
                if re.search(HA_HARDWARE.ZSERIES_ENCLOSURE.value, info):
                    self.HARDWARE = 'ECHOSTREAM'
                    reg = re.search(HA_HARDWARE.ZSERIES_NODE.value, info)
                    self.NODE = reg.group(1)
                    if self.NODE:
                        break

                # Identify the X-series Hardware (PUMA)
                # TODO: Verify this works on X-series Hardware
                elif re.search(HA_HARDWARE.XSERIES_ENCLOSURE_LINUX.value, info):
                    self.HARDWARE = 'PUMA'

                    # We need to get the SAS address of the SAS expander first
                    sas_addr_file = ENCLOSURES_DIR + enc.split('/dev/bsg/')[-1] + '/id'
                    with open(sas_addr_file, 'r') as f:
                        sas_addr = f.read().strip()

                    # We then cast the SES address (deduced from SES VPD pages)
                    # to an integer and subtract 1. Then cast it back to hexadecimal.
                    # We then compare if the SAS expander's SAS address
                    # is in the SAS expanders SES address
                    reg = re.search(HA_HARDWARE.XSERIES_NODEA.value, info)
                    if reg:
                        ses_addr = hex(int(reg.group(1), 16) - 1)
                        if ses_addr in sas_addr:
                            self.NODE = 'A'
                            break

                    reg = re.search(HA_HARDWARE.XSERIES_NODEB.value, info)
                    if reg:
                        ses_addr = hex(int(reg.group(1), 16) - 1)
                        if ses_addr in sas_addr:
                            self.NODE = 'B'
                            break

                # Identify the M-series hardware (Echowarp)
                else:
                    reg = re.search(HA_HARDWARE.MSERIES_ENCLOSURE_LINUX.value, info)
                    if reg:
                        self.HARDWARE = 'ECHOWARP'

                        # Identify M-series A slot in chassis
                        if reg.group(2) == 'p':
                            self.NODE = 'A'
                            break

                        # Identify M-series B slot in chassis
                        if reg.group(2) == 's':
                            self.NODE = 'B'
                            break

        return self.HARDWARE, self.NODE
