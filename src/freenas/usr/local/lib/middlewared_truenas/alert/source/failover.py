# Copyright (c) 2015 iXsystems, Inc.
# All rights reserved.
# This file is a part of TrueNAS
# and may not be copied and/or distributed
# without the express permission of iXsystems.

import errno
import os
import json
try:
    import sysctl
except ImportError:
    sysctl = None
import subprocess

from middlewared.alert.base import AlertClass, AlertCategory, AlertLevel, Alert, ThreadedAlertSource, UnavailableException
from middlewared.service_exception import CallError

FAILOVER_JSON = '/tmp/failover.json'


class FailoverInterfaceNotFoundAlertClass(AlertClass):
    category = AlertCategory.HA
    level = AlertLevel.CRITICAL
    title = "Failover Internal Interface Not Found"
    text = "Failover internal interface not found. Contact support."

    products = ("ENTERPRISE",)


class TrueNASVersionsMismatchAlertClass(AlertClass):
    category = AlertCategory.HA
    level = AlertLevel.CRITICAL
    title = "TrueNAS Versions Mismatch In Failover"
    text = "TrueNAS versions mismatch in failover. Update both controllers to the same version."

    products = ("ENTERPRISE",)


class FailoverAccessDeniedAlertClass(AlertClass):
    category = AlertCategory.HA
    level = AlertLevel.CRITICAL
    title = "Failover Access Denied"
    text = "Failover access denied. Please reconfigure it."

    products = ("ENTERPRISE",)


class FailoverStatusCheckFailedAlertClass(AlertClass):
    category = AlertCategory.HA
    level = AlertLevel.CRITICAL
    title = "Failed to Check Failover Status with the Other Controller"
    text = "Failed to check failover status with the other controller: %s."

    products = ("ENTERPRISE",)


class FailoverFailedAlertClass(AlertClass):
    category = AlertCategory.HA
    level = AlertLevel.CRITICAL
    title = "Failover Failed"
    text = "Failover failed: %s."

    products = ("ENTERPRISE",)


class ExternalFailoverLinkStatusAlertClass(AlertClass):
    category = AlertCategory.HA
    level = AlertLevel.CRITICAL
    title = "Could not Determine External Failover Link Status"
    text = "Could not determine external failover link status, check cabling."

    products = ("ENTERPRISE",)


class InternalFailoverLinkStatusAlertClass(AlertClass):
    category = AlertCategory.HA
    level = AlertLevel.CRITICAL
    title = "Could not Determine Internal Failover Link Status"
    text = "Could not determine internal failover link status. Automatic failover disabled."

    products = ("ENTERPRISE",)


class CARPStatesDoNotAgreeAlertClass(AlertClass):
    category = AlertCategory.HA
    level = AlertLevel.CRITICAL
    title = "Controllers CARP States Do Not Agree"
    text = "Controllers CARP states do not agree: %(error)s."

    products = ("ENTERPRISE",)


class CTLHALinkAlertClass(AlertClass):
    category = AlertCategory.HA
    level = AlertLevel.CRITICAL
    title = "CTL HA link Is not Connected"
    text = "CTL HA link is not connected."

    products = ("ENTERPRISE",)


class NoFailoverEscrowedPassphraseAlertClass(AlertClass):
    category = AlertCategory.HA
    level = AlertLevel.CRITICAL
    title = "No Escrowed Passphrase for Failover"
    text = "No escrowed passphrase for failover. Automatic failover disabled."

    products = ("ENTERPRISE",)


class FailoverAlertSource(ThreadedAlertSource):
    products = ("ENTERPRISE",)
    failover_related = True
    run_on_backup_node = False

    def check_sync(self):
        alerts = []

        if not self.middleware.call_sync('failover.licensed'):
            return alerts

        if self.middleware.call_sync('failover.internal_interfaces_notfound'):
            alerts.append(Alert(FailoverInterfaceNotFoundAlertClass))
            return alerts

        try:
            self.middleware.call_sync('failover.call_remote', 'core.ping')

            local_version = self.middleware.call_sync('system.version')
            remote_version = self.middleware.call_sync('failover.call_remote', 'system.version')
            if local_version != remote_version:
                return [Alert(TrueNASVersionsMismatchAlertClass)]

            if not self.middleware.call_sync('failover.call_remote', 'system.ready'):
                raise UnavailableException()

            local = self.middleware.call_sync('failover.get_carp_states')
            remote = self.middleware.call_sync('failover.call_remote', 'failover.get_carp_states')

            errors = self.middleware.call_sync('failover.check_carp_states', local, remote)
            for error in errors:
                alerts.append(Alert(
                    CARPStatesDoNotAgreeAlertClass,
                    {"error": error},
                ))

        except CallError as e:
            if e.errno != errno.ECONNREFUSED:
                return [Alert(FailoverStatusCheckFailedAlertClass, [str(e)])]

        status = self.middleware.call_sync('failover.status')

        if status == 'ERROR':
            errmsg = None
            if os.path.exists('/tmp/.failover_failed'):
                with open('/tmp/.failover_failed', 'r') as fh:
                    errmsg = fh.read()
            if not errmsg:
                errmsg = 'Unknown error'

            alerts.append(Alert(FailoverFailedAlertClass, [errmsg]))

        elif status not in ('MASTER', 'BACKUP', 'SINGLE'):
            alerts.append(Alert(ExternalFailoverLinkStatusAlertClass))

        internal_ifaces = self.middleware.call_sync('failover.internal_interfaces')
        if internal_ifaces:
            p1 = subprocess.Popen(
                "/sbin/ifconfig %s|grep -E 'vhid (10|20) '|grep 'carp:'" % internal_ifaces[0],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
                encoding='utf8',
            )
            stdout = p1.communicate()[0].strip()
            if status != "SINGLE" and stdout.count("\n") != 1:
                alerts.append(Alert(InternalFailoverLinkStatusAlertClass))

        if status != "SINGLE":
            try:
                if sysctl.filter('kern.cam.ctl.ha_link')[0].value == 1:
                    alerts.append(Alert(CTLHALinkAlertClass))
            except Exception:
                pass

        if status == 'BACKUP':
            fobj = None
            try:
                with open(FAILOVER_JSON, 'r') as f:
                    fobj = json.loads(f.read())
            except Exception:
                pass
            try:
                if len(fobj['phrasedvolumes']) > 0:
                    if not self.middleware.call_sync('failover.encryption_status'):
                        alerts.append(Alert(NoFailoverEscrowedPassphraseAlertClass))
                        # Kick a syncfrompeer if we don't.
                        passphrase = self.middleware.call_sync(
                            'failover.call_remote', 'failover.encryption_getkey'
                        )
                        if passphrase:
                            self.middleware.call_sync('failover.encryption_setkey', passphrase)
            except Exception:
                pass

        return alerts
