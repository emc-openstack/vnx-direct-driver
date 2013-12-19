# vim: tabstop=4 shiftwidth=4 softtabstop=4


#    Copyright (c) 2013 EMC Corporation
#    All Rights Reserved
#
#    Licensed under EMC Freeware Software License Agreement
#    You may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        https://github.com/emc-openstack/freeware-eula/
#        blob/master/Freeware_EULA_20131217_modified.md
#

"""
ISCSI Drivers for EMC VNX array based on CLI.

"""

import os
import time

from cinder import exception
from cinder.openstack.common import log as logging
from cinder import utils
from cinder.volume import driver
from cinder.volume.drivers.emc import emc_vnx_cli

LOG = logging.getLogger(__name__)

class EMCCLIISCSIDriver(driver.ISCSIDriver):
    """EMC ISCSI Drivers for VMAX and VNX using SMI-S."""

    def __init__(self, *args, **kwargs):

        super(EMCCLIISCSIDriver, self).__init__(*args, **kwargs)
        self.cli = emc_vnx_cli.EMCVnxCli(
                                  'iSCSI',
                                  configuration=self.configuration)

    def check_for_setup_error(self):
        pass

    def create_volume(self, volume):
        """Creates a EMC(VMAX/VNX) volume."""
        self.cli.create_volume(volume)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot."""
        self.cli.create_volume_from_snapshot(volume, snapshot)

    def create_cloned_volume(self, volume, src_vref):
        """Creates a cloned volume."""
        self.cli.create_cloned_volume(volume, src_vref)

    def delete_volume(self, volume):
        """Deletes an EMC volume."""
        self.cli.delete_volume(volume)

    def create_snapshot(self, snapshot):
        """Creates a snapshot."""
        self.cli.create_snapshot(snapshot)

    def delete_snapshot(self, snapshot):
        """Deletes a snapshot."""
        self.cli.delete_snapshot(snapshot)

    def ensure_export(self, context, volume):
        """Driver entry point to get the export info for an existing volume."""
        pass

    def create_export(self, context, volume):
        """Driver entry point to get the export info for a new volume."""
        self.cli.create_export(context , volume)

    def remove_export(self, context, volume):
        """Driver entry point to remove an export for a volume."""
        pass

    def check_for_export(self, context, volume_id):
        """Make sure volume is exported."""
        pass

    def initialize_connection(self, volume, connector):
        """Initializes the connection and returns connection info.

        The iscsi driver returns a driver_volume_type of 'iscsi'.
        the format of the driver data is defined in _get_iscsi_properties.
        Example return value::

            {
                'driver_volume_type': 'iscsi'
                'data': {
                    'target_discovered': True,
                    'target_iqn': 'iqn.2010-10.org.openstack:volume-00000001',
                    'target_portal': '127.0.0.0.1:3260',
                    'volume_id': 1,
                }
            }

        """
        self.cli.initialize_connection(volume, connector)
		
        iscsi_properties = self._get_iscsi_properties(volume,connector)
        return {
            'driver_volume_type': 'iscsi',
            'data': iscsi_properties
        }

    def _do_iscsi_discovery(self, volume):

        LOG.warn(_("ISCSI provider_location not stored, using discovery"))

        (out, _err) = self._execute('iscsiadm', '-m', 'discovery',
                                    '-t', 'sendtargets', '-p',
                                    self.configuration.iscsi_ip_address,
                                    run_as_root=True)
        targets = []
        for target in out.splitlines():
            targets.append(target)

        return targets

    def _get_iscsi_properties(self, volume,connector):
        """Gets iscsi configuration.

        We ideally get saved information in the volume entity, but fall back
        to discovery if need be. Discovery may be completely removed in future
        The properties are:

        :target_discovered:    boolean indicating whether discovery was used

        :target_iqn:    the IQN of the iSCSI target

        :target_portal:    the portal of the iSCSI target

        :target_lun:    the lun of the iSCSI target

        :volume_id:    the id of the volume (currently used by xen)

        :auth_method:, :auth_username:, :auth_password:

            the authentication details. Right now, either auth_method is not
            present meaning no authentication, or auth_method == `CHAP`
            meaning use CHAP with the specified credentials.
        """
        properties = {}

        location = self._do_iscsi_discovery(volume)
        if not location:
            raise exception.InvalidVolume(_("Could not find iSCSI export "
                                          " for volume %s") %
                                          (volume['name']))

        LOG.debug(_("ISCSI Discovery: Found %s") % (location))
        properties['target_discovered'] = True

        hostname = connector['host']
        storage_group = self.cli.get_storage_group(hostname)
        device_info = self.cli.find_device_details(volume,storage_group)
        if device_info is None or device_info['hostlunid'] is None:
            exception_message = (_("Cannot find device number for volume %s")
                                 % volume['name'])
            raise exception.VolumeBackendAPIException(data=exception_message)
        
        device_number = device_info['hostlunid']
        device_sp = device_info['ownersp']        
        endpoints = []
        
        if device_sp:
            # endpointss example:
            # [iqn.1992-04.com.emc:cx.apm00123907237.a8,
            # iqn.1992-04.com.emc:cx.apm00123907237.a9]
            endpoints = self.cli._find_iscsi_protocol_endpoints(device_sp)
            
        foundEndpoint = False
        for loc in location:
            results = loc.split(" ")
            properties['target_portal'] = results[0].split(",")[0]
            properties['target_iqn'] = results[1]
            # for VNX, find the target_iqn that matches the endpoint
            # target_iqn example: iqn.1992-04.com.emc:cx.apm00123907237.a8
            # or iqn.1992-04.com.emc:cx.apm00123907237.b8
            if not device_sp:
                break            
            for endpoint in endpoints:
                if properties['target_iqn'] == endpoint:
                    LOG.debug(_("Found iSCSI endpoint: %s") % endpoint)
                    foundEndpoint = True
                    break
            if foundEndpoint:
                break

        if device_sp and not foundEndpoint:
            LOG.warn(_("ISCSI endpoint not found for SP %(sp)s ")
                     % {'sp': device_sp})

        properties['target_lun'] = device_number

        properties['volume_id'] = volume['id']

        auth = volume['provider_auth']
        if auth:
            (auth_method, auth_username, auth_secret) = auth.split()

            properties['auth_method'] = auth_method
            properties['auth_username'] = auth_username
            properties['auth_password'] = auth_secret

        LOG.debug(_("ISCSI properties: %s") % (properties))
		
        return properties

    def terminate_connection(self, volume, connector, **kwargs):
        """Disallow connection from connector."""
        self.cli.terminate_connection(volume, connector)

    def get_volume_stats(self, refresh=False):
        """Get volume status.

        If 'refresh' is True, run update the stats first.
        """
        if refresh:
            self.update_volume_status()

        return self._stats

    def update_volume_status(self):
        """Retrieve status info from volume group."""
        LOG.debug(_("Updating volume status"))
        #retrieving the volume update from the VNX
        data = self.cli.update_volume_status()
        backend_name = self.configuration.safe_get('volume_backend_name')
        data['volume_backend_name'] = backend_name or 'EMCCLIISCSIDriver'
        data['storage_protocol'] = 'iSCSI'
        self._stats = data
