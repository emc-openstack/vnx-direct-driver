# Copyright (c) 2014 EMC Corporation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Fibre Channel Driver for EMC VNX array based on CLI.

"""
from cinder.openstack.common import log as logging
from cinder.volume import driver
from cinder.volume.drivers.emc import emc_vnx_cli

LOG = logging.getLogger(__name__)


class EMCCLIFCDriver(driver.FibreChannelDriver):
    """EMC FC Driver for VNX using CLI."""

    def __init__(self, *args, **kwargs):

        super(EMCCLIFCDriver, self).__init__(*args, **kwargs)
        self.cli = emc_vnx_cli.getEMCVnxCli(
            'FC',
            configuration=self.configuration)

    def check_for_setup_error(self):
        pass

    def create_volume(self, volume):
        """Creates a volume."""
        return self.cli.create_volume(volume)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot."""
        return self.cli.create_volume_from_snapshot(volume, snapshot)

    def create_cloned_volume(self, volume, src_vref):
        """Creates a cloned volume."""
        return self.cli.create_cloned_volume(volume, src_vref)

    def extend_volume(self, volume, new_size):
        """Extend a volume."""
        self.cli.extend_volume(volume, new_size)

    def delete_volume(self, volume):
        """Deletes a volume."""
        self.cli.delete_volume(volume)

    def migrate_volume(self, ctxt, volume, host):
        """Migrate volume via EMC migration functionality."""
        return self.cli.migrate_volume(ctxt, volume, host)

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
        pass

    def remove_export(self, context, volume):
        """Driver entry point to remove an export for a volume."""
        pass

    def check_for_export(self, context, volume_id):
        """Make sure volume is exported."""
        pass

    def initialize_connection(self, volume, connector):
        """Initializes the connection and returns connection info.

        Assign any created volume to a compute node/host so that it can be
        used from that host.

        The  driver returns a driver_volume_type of 'fibre_channel'.
        The target_wwn can be a single entry or a list of wwns that
        correspond to the list of remote wwn(s) that will export the volume.
        Example return values:

            {
                'driver_volume_type': 'fibre_channel'
                'data': {
                    'target_discovered': True,
                    'target_lun': 1,
                    'target_wwn': '1234567890123',
                }
            }

            or

             {
                'driver_volume_type': 'fibre_channel'
                'data': {
                    'target_discovered': True,
                    'target_lun': 1,
                    'target_wwn': ['1234567890123', '0987654321321'],
                }
            }

        """
        device_number = self.cli.initialize_connection(volume,
                                                       connector)
        ports = self.cli.get_target_wwns(connector)

        data = {'driver_volume_type': 'fibre_channel',
                'data': {'target_lun': device_number,
                         'target_discovered': True,
                         'target_wwn': ports}}

        LOG.debug(_('Return FC data: %(data)s.')
                  % {'data': data})

        return data

    def terminate_connection(self, volume, connector, **kwargs):
        """Disallow connection from connector."""
        self.cli.terminate_connection(volume, connector)

    def get_volume_stats(self, refresh=False):
        """Get volume stats.

        If 'refresh' is True, run update the stats first.
        """
        if refresh:
            self.update_volume_stats()

        return self._stats

    def update_volume_stats(self):
        """Retrieve stats info from volume group."""
        LOG.debug(_("Updating volume stats"))
        data = self.cli.update_volume_status()
        backend_name = self.configuration.safe_get('volume_backend_name')
        data['volume_backend_name'] = backend_name or 'EMCCLIFCDriver'
        data['storage_protocol'] = 'FC'
        self._stats = data
