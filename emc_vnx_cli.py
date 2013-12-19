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
VNX CLI on iSCSI 
"""

import time
import os
import sys
import commands

from oslo.config import cfg
from xml.dom.minidom import parseString

from cinder import exception
from cinder.openstack.common import log as logging

LOG = logging.getLogger(__name__)

CONF = cfg.CONF
VERSION = '01.01.00'

class EMCVnxCli():
    """This class defines the functions to use the native CLI functionality"""
	
    stats = {'driver_version': VERSION,
             'free_capacity_gb': 'unknown',
             'reserved_percentage': 0,
             'storage_protocol': None,
             'total_capacity_gb': 'unknown',
             'vendor_name': 'EMC',
             'volume_backend_name': None}

    def __init__(self, prtcl, configuration=None):

		
        loc_opts = [
            cfg.StrOpt('storage_vnx_ip_address',
                         default='none',
                         help='VNX ip address'),
            cfg.StrOpt('storage_vnx_username',
                         default='',
                         help='VNX username'),
            cfg.StrOpt('storage_vnx_password',
                         default='',
                         help='VNX password'),
            cfg.StrOpt('naviseccli_path',
                         default='',
                         help='Naviseccli Path'),                         
            cfg.IntOpt('iscsi_pool_id',
                         default=-1,
                         help='ISCSI pool ID'),
            cfg.IntOpt('default_timeout',
                         default=-1,
                         help='Default Time Out For CLI operations'),
        ]

        CONF.register_opts(loc_opts)
        self.protocol = prtcl
        self.configuration = configuration
        self.configuration.append_config_values(loc_opts)
        self.storage_ip = self.configuration.storage_vnx_ip_address
        self.storage_username = self.configuration.storage_vnx_username
        self.storage_password = self.configuration.storage_vnx_password
        self.pool_id = self.configuration.iscsi_pool_id
        self.timeout = self.configuration.default_timeout
        self.navisecclipath = self.configuration.naviseccli_path
        self.navisecclicmd = (_('%s -address %s -user %s -password %s '
        '-scope 0 ')% (self.navisecclipath,self.storage_ip,
        self.storage_username,self.storage_password))

        #Checking for existence of naviseccli tool
        if not os.path.exists(self.navisecclipath):
            LOG.error(_('Could not find NAVISECCLI tool '))
            raise exception.Error()
		
        #Testing the naviseccli setup		
        test_command = (_("%(navisecclicmd)s storagepool -list "
        "-id %(poolid)s -state") 
            % {'navisecclicmd':self.navisecclicmd,
            'poolid':self.pool_id})

        test_command_rc = os.system(test_command)

        if test_command_rc !=0:
            LOG.error(_('Command to test Naviseccli Failed'))
            raise exception.Error()		


    def create_volume(self,volume):
        """Creates a EMC volume."""

        LOG.debug(_('Entering create_volume.'))
        volumesize = volume['size']
        volumename = volume['name']

        LOG.info(_('Create Volume: %(volume)s  Size: %(size)s')
                 % {'volume': volumename,
                   'size': volumesize})

        #defining CLI command
		
        command_to_execute = (_("%(navisecclicmd)s lun -create -capacity "
        "%(volumesize)s -sq gb -poolId %(poolid)s -name %(volumename)s")
                % {'navisecclicmd':self.navisecclicmd,
                   'volumesize':volumesize,
                   'poolid':self.pool_id,
                   'volumename':volumename})

        #executing CLI command to create volume
        command_rc = os.system(command_to_execute)

        LOG.debug(_('Create Volume: %(volumename)s  Return code: %(rc)lu')
                  % {'volumename': volumename,
                     'rc': command_rc})
        if command_rc == 1024:
            LOG.warn(_('Volume already exists'))
        elif command_rc !=0:
            LOG.error(_('Command to create the specified volume failed'))
            raise exception.Error()

    def delete_volume(self,volume):
        """Deletes an EMC volume."""
		
        LOG.debug(_('Entering delete_volume.'))
        volumename = volume['name']

        #defining CLI command
        command_to_execute = (_('%(navisecclicmd)s lun -destroy -name '
        '%(volumename)s -forceDetach -o ') 
            % {'navisecclicmd':self.navisecclicmd,
               'volumename':volumename})

        #executing CLI command to delete volume
        command_rc = os.system(command_to_execute)

        LOG.debug(_('Delete Volume: %(volumename)s  Return code: %(rc)lu')
                  % {'volumename': volumename,
                     'rc': command_rc})
        if command_rc not in [0,2304]:
            LOG.error(_('Command to delete the specified volume failed'))
            raise exception.Error()


    def update_volume_status(self):
        """Retrieve status info."""
        LOG.debug(_("Updating volume status"))

        command_to_execute = (_('%(navisecclicmd)s storagepool -list -id '
        '%(poolid)s -userCap -availableCap')
            % {'navisecclicmd':self.navisecclicmd,
               'poolid':self.pool_id})

        pool_details = commands.getoutput(command_to_execute).split('\n')
        print pool_details
		
		#this output structure is confined with naviseccli commands only.
		#need to update the steps if the output format for command changes.
		#command ::  storagepool -list
		
        self.stats['total_capacity_gb'] = float(
                pool_details[3].split(':')[1].strip())
        self.stats['free_capacity_gb'] = float(
                pool_details[5].split(':')[1].strip())

        return self.stats

    def create_export(self, context, volume):
        """Driver entry point to get the export info for a new volume."""
        volumename = volume['name']

        device_id = self._find_lun_id(volumename)

        LOG.debug(_('create_export: Volume: %(volume)s  Device ID: '
                    '%(device_id)s')
                     % {'volume': volumename,
                         'device_id': device_id})

        return {'provider_location': device_id}

    def _find_lun_id(self,volumename):
        """Returns the LUN of a volume"""
        
        command_to_execute = (_('%(navisecclicmd)s lun -list -name '
        '%(volumename)s') 
                % {'navisecclicmd':self.navisecclicmd,
                   'volumename':volumename})

        vol_details = commands.getoutput(command_to_execute).split('\n')
        lun = vol_details[0].split(' ')[3]

        return lun


    def create_snapshot(self, snapshot):
        """Creates a snapshot."""
        LOG.debug(_('Entering create_snapshot.'))
        snapshotname = snapshot['name']
        volumename = snapshot['volume_name']
        LOG.info(_('Create snapshot: %(snapshot)s: volume: %(volume)s')
                % {'snapshot': snapshotname,
                    'volume': volumename})
        
        volume_lun = self._find_lun_id(volumename)
        
        #defining CLI command
        command_to_execute = (_("%(navisecclicmd)s snap -create -res "
        "%(volumelun)s -name %(snapshotname)s -allowReadWrite yes") 
                % {'navisecclicmd':self.navisecclicmd,
                   'volumelun':volume_lun,
                   'snapshotname':snapshotname})

        #executing CLI command to create snapshot
        command_rc = os.system(command_to_execute)

        LOG.debug(_('Create Snapshot: %(snapshotname)s  Return code: %(rc)lu')
                  % {'snapshotname': snapshotname,
                     'rc': command_rc})
        if command_rc !=0:
            LOG.error(_('Command to create the specified Snapshot failed'))
            raise exception.Error()

    def delete_snapshot(self, snapshot):
        """Deletes a snapshot."""
        LOG.debug(_('Entering delete_snapshot.'))

        snapshotname = snapshot['name']
        volumename = snapshot['volume_name']
        LOG.info(_('Delete Snapshot: %(snapshot)s: volume: %(volume)s')
                % {'snapshot': snapshotname,
                    'volume': volumename})

        #defining CLI command
        command_to_execute = (_('%(navisecclicmd)s snap -destroy -id '
        '%(snapshotname)s -o')
            % {'navisecclicmd':self.navisecclicmd,
               'snapshotname':snapshotname})

        #executing CLI command
        command_rc = os.system(command_to_execute)

        LOG.debug(_('Delete Snapshot: Volume: %(volumename)s  Snapshot: '
                '%(snapshotname)s  Return code: %(rc)lu')
                % {'volumename': volumename,
                    'snapshotname': snapshotname,
                    'rc': command_rc})

        if command_rc not in [0,2304,1280]:
            if command_rc == 768:
                LOG.info(_('Snapshot is in use'))
                time.sleep(90)
                self.delete_snapshot(snapshot)
            else:
                LOG.error(_('Command to delete the specified snapshot failed'))
                raise exception.Error()

    def _verify_sync_status(self,volumename):
        """Returns True if sync is complete else False"""

        total_default_timeout = int(self.timeout)*60
        default_sleep = 60
        loop_count = int(total_default_timeout/default_sleep)
        sync_status = False
        counter = 0

        command_to_execute = (_("%(navisecclicmd)s lun -list -name "
        "%(volumename)s -attachedSnapshot")
             % {'navisecclicmd':self.navisecclicmd,
                'volumename':volumename})

        while not sync_status:            
            try:
                vol_details = commands.getoutput(command_to_execute).split('\n')
                snapshotname = vol_details[2].split(':')[1].strip()
            except Exception:
                break
                
            if (snapshotname == 'N/A'):
                sync_status = True
                break
            else:
                LOG.info(_('Waiting to get the update on Sync status .....'))
                if (counter < loop_count):
                    counter += 1
                    time.sleep(default_sleep)
                    continue
                else :
                    break

        return sync_status

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot."""
        LOG.debug(_('Entering create_volume_from_snapshot.'))

        snapshotname = snapshot['name']
        source_volume_name = snapshot['volume_name']
        volumename = volume['name']
        volumesize = snapshot['volume_size']


        #defining CLI command
        command_to_execute = (_("%(navisecclicmd)s lun -create -type Snap "
        "-primaryLunName %(sourcelun)s -sp A -name %(volumename)s") 
             % {'navisecclicmd':self.navisecclicmd,
                'sourcelun':source_volume_name,
                'volumename':volumename})

        #executing CLI command
        command_rc = os.system(command_to_execute)
        LOG.debug(_('Create mount point : Volume: %(volumename)s  '
                    'Source Volume: %(sourcevolumename)s  Return code: %(rc)lu')
                    % {'volumename': volumename,
                        'sourcevolumename': source_volume_name,
                        'rc': command_rc})
        
        if command_rc !=0:
            LOG.error(_('Command to create the specified mount point failed'))
            raise exception.Error()

        #defining CLI command
        command_to_execute = (_("%(navisecclicmd)s lun -attach -name "
        "%(volumename)s -snapName %(snapshotname)s")
                % {'navisecclicmd':self.navisecclicmd,
                   'volumename':volumename,
                   'snapshotname':snapshotname})

        #executing CLI command
        command_rc = os.system(command_to_execute)
        LOG.debug(_('Attaching mount point Volume: %(volumename)s  '
                    'with  Snapshot: %(snapshotname)s  Return code: %(rc)lu')
                    % {'volumename': volumename,
                        'snapshotname': snapshotname,
                        'rc': command_rc})

        if command_rc !=0:
            LOG.error(_('Command to attach the specified mount point Volume '
                        'with Snapshot failed'))
            raise exception.Error()

        
        tempvolumename = 'openstack-temp-volume'

        #deleting any existing volume with the same name as of tempvolume
        
        LOG.info(_('Deleting Existing Temporary Volume IF present : %s ') 
                %(tempvolumename))        
        command_to_execute = (_("%(navisecclicmd)s lun -destroy -name "
        "%(tempvolumename)s -forceDetach -o")
                % {'navisecclicmd':self.navisecclicmd,
                   'tempvolumename':tempvolumename})

        os.system(command_to_execute)
        
        LOG.info(_('Creating Temporary Volume : %s ') %(tempvolumename))

        #defining CLI command
        command_to_execute = (_("%(navisecclicmd)s lun -create -capacity "
        "%(volumesize)s -sq gb -poolId %(poolid)s -sp A -name "
        "%(tempvolumename)s") 
                % {'navisecclicmd':self.navisecclicmd,
                   'poolid':self.pool_id,
                   'volumesize':volumesize,
                   'tempvolumename':tempvolumename})
        #executing CLI command
        command_rc = os.system(command_to_execute)

        LOG.debug(_('Create temporary Volume: %(volumename)s  '
                    'Return code : %(rc)lu')
                    %{'volumename': tempvolumename,
                       'rc': command_rc})

        if command_rc !=0:
            LOG.error(_('Command to create the temporary Volume failed'))
            raise exception.Error()

        source_vol_lun = self._find_lun_id(volumename)
        temp_vol_lun = self._find_lun_id(tempvolumename)

        LOG.info(_('Migrating Mount Point Volume: %s ') %(volumename))

        #defining CLI command
        command_to_execute = (_("%(navisecclicmd)s migrate -start -source "
        "%(source)s -dest %(destination)s -rate ASAP -o") 
            % {'navisecclicmd':self.navisecclicmd,
               'source':source_vol_lun,
               'destination':temp_vol_lun})

        #executing CLI command
        command_rc = os.system(command_to_execute)
        
        LOG.debug(_('Migrate Mount Point  Volume: %(volumename)s  '
                    'Return code : %(rc)lu')
                    %{'volumename': volumename,
                       'rc': command_rc})
        
        if command_rc !=0:
            LOG.error(_('Command to migrate mount point Volume failed'))
            raise exception.Error()

        sync_status = self._verify_sync_status(volumename)
        
        if not sync_status:
            LOG.error(_('Synchronisation after migration failed.'))
            raise exception.Error()

        
    def create_cloned_volume(self, volume, src_vref):
        """Creates a clone of the specified volume."""

        source_volume_name = src_vref['name']
        volumename = volume['name']
        volumesize = src_vref['size']
        snapshotname = source_volume_name+'-temp-snapshot'
		
        snapshot = {
            'name' : snapshotname,
            'volume_name' : source_volume_name,
            'volume_size' : volumesize,
            }
		
        #Create temp Snapshot
        self.create_snapshot(snapshot)

        #Create volume
        self.create_volume_from_snapshot(volume, snapshot)

        #Delete temp Snapshot
        self.delete_snapshot(snapshot)
	
    def get_storage_group(self,hostname):
        """Returns the storage group for the host node"""
        
        sg_name = 'None'
        hostname_to_search = hostname+'\n'
        
        command_to_execute = (_('%(navisecclicmd)s storagegroup -list -host')
                % {'navisecclicmd':self.navisecclicmd})

        output = commands.getoutput(command_to_execute)
        sg_details = output.split('Storage Group Name')
        for sg in sg_details:
             if sg.find(hostname_to_search)>=0:
                 sg_data = sg.split('\n')
                 sg_name = sg_data[0].strip(': ')
                 break
        if sg_name != 'None':
            LOG.info(_('Storage group for host : %s is : %s')
                %(hostname,sg_name))

            return sg_name
        else:
            LOG.debug(_('creating new storage group'))
            timestamp = str(time.time()).split('.')[0]
            default_storage_groupname = 'EMC_CLI_storagegroup-%s'%timestamp

            command_to_execute = (_('%(navisecclicmd)s storagegroup -create '
            '-gname %(storagegrpname)s')
                    % {'navisecclicmd':self.navisecclicmd,
                       'storagegrpname':default_storage_groupname})

            command_rc = os.system(command_to_execute)
            LOG.debug(_('Create new storage group : %s , Return Code : %s')
                    %(default_storage_groupname,command_rc))
        
            if command_rc !=0:
                LOG.error(_('Command to create the storage group failed'))
                raise exception.Error()

            #connecting the new storagegroup to the host
            command_to_execute = (_('%(navisecclicmd)s storagegroup '
            '-connecthost -host %(hostname)s -gname %(storagegrpname)s -o')
                    % {'navisecclicmd':self.navisecclicmd,
                       'hostname':hostname,
                       'storagegrpname':default_storage_groupname})
            
            command_rc = os.system(command_to_execute)
            LOG.debug(_('Connect storage group : %s ,To Host : %s  '
                'Return Code : %s') %(default_storage_groupname,hostname,
                    command_rc))
        
            if command_rc !=0:
                LOG.error(_('Command to connect with storage group failed'))
                raise exception.Error()

            return default_storage_groupname

    def find_device_details(self,volume,storage_group):
        """Returns the Host Device number for the volume"""

        volumename = volume['name']
        allocated_lun_id = self._find_lun_id(volumename)
        host_lun_id = False
        lun_map = {}

        command_to_execute = (_('%(navisecclicmd)s storagegroup -list '
        '-gname %(storagegrpname)s') 
                % {'navisecclicmd':self.navisecclicmd,
                   'storagegrpname':storage_group})

        output = commands.getoutput(command_to_execute)
        
        if output.find('HLU/ALU Pairs')== -1:
            LOG.info(_('NO LUNs in the storagegroup : %s ')
                    %(storage_group))
        else:
            sg_details = output.split('HLU/ALU Pairs:')[1]
            sg_lun_details = sg_details.split('Shareable')[0]
            lun_details = sg_lun_details.split('\n')

            for data in lun_details:
                if data not in ['','  HLU Number     ALU Number',
                        '  ----------     ----------']:
                    data = data.strip()
                    items = data.split(' ')
                    lun_map[items[len(items)-1]]=items[0]
            for lun in lun_map.iterkeys():
                if lun == allocated_lun_id:
                    host_lun_id = lun_map[lun]
                    LOG.debug(_('Host Lun Id : %s') %(host_lun_id))
                    break

        #finding the owner SP for the LUN
        command_to_execute = (_('%(navisecclicmd)s lun -list -l '
        '%(lunid)s -owner')
                % {'navisecclicmd':self.navisecclicmd,
                   'lunid':allocated_lun_id})

        output = commands.getoutput(command_to_execute).split('\n')
        owner_sp = output[2].split('Current Owner:  SP ')[1]
        LOG.debug(_('Owner SP : %s') %(owner_sp))
        
        device = {
                'hostlunid' : host_lun_id,
                'ownersp' : owner_sp,
                'lunmap' : lun_map,
                }
        return device

    def _add_lun_to_storagegroup(self,lun_map,volume,storage_group):
            
        volumename = volume['name']
        allocated_lun_id = self._find_lun_id(volumename)

        if lun_map:
            host_lun_id_list = lun_map.values()
            host_lun_id_list.sort()
            host_lun_id = int(host_lun_id_list[len(host_lun_id_list)-1])+1
        else:
            host_lun_id = 0

        command_to_execute = (_('%(navisecclicmd)s storagegroup -addhlu -o '
        '-gname %(storagegrpname)s -hlu %(hostlunid)s -alu %(lunid)s') 
                % {'navisecclicmd':self.navisecclicmd,
                   'storagegrpname':storage_group,
                   'hostlunid':host_lun_id,
                   'lunid':allocated_lun_id})

        command_rc = os.system(command_to_execute)

        LOG.debug(_('Add LUN to storagegroup . Return Code : %s')
                %(command_rc))
        if command_rc !=0:
            LOG.error(_('Command to add LUN in storagegroup failed'))
            raise exception.Error()

        return host_lun_id

    def _remove_lun_from_storagegroup(self,device_number,storage_group):
        
        command_to_execute = (_('%(navisecclicmd)s storagegroup -removehlu '
        '-gname %(storagegrpname)s -hlu %(hostlunid)s -o') 
                % {'navisecclicmd':self.navisecclicmd,
                   'storagegrpname':storage_group,
                   'hostlunid':device_number})

        command_rc = os.system(command_to_execute)

        LOG.debug(_('Remove LUN from storagegroup . Return Code : %s')
                %(command_rc))
        if command_rc !=0:
            LOG.error(_('Command to remove LUN from storagegroup failed'))
            raise exception.Error()


    def initialize_connection(self,volume, connector):
        """Initializes the connection and returns connection info."""
        
        hostname = connector['host']
        storage_group = self.get_storage_group(hostname)

        device_info = self.find_device_details(volume,storage_group)
        device_number = device_info['hostlunid']
        if not device_number:
            #adding the volume to the storagegroup
            lun_map = device_info['lunmap']
            device_number = self._add_lun_to_storagegroup(
                    lun_map,volume,storage_group)
        return device_number

    def terminate_connection(self, volume, connector):
        """Disallow connection from connector."""
        hostname = connector['host']
        storage_group = self.get_storage_group(hostname)
        device_info = self.find_device_details(volume,storage_group)
        device_number = device_info['hostlunid']
        if not device_number:
            LOG.error(_('Could not locate the attached volume.'))
        else:
            self._remove_lun_from_storagegroup(device_number,storage_group)

    def _find_iscsi_protocol_endpoints(self,device_sp):
        """Returns the iSCSI initiators for a SP"""

        initiator_address = []

        command_to_execute = (_('%(navisecclicmd)s connection -getport '
        '-sp %(devicesp)s') 
                % {'navisecclicmd':self.navisecclicmd,
                   'devicesp':device_sp})

        output = commands.getoutput(command_to_execute).split('SP:  ')
        for port in output:
            port_info = port.split('\n')
            if port_info[0] == device_sp:
                port_wwn = port_info[2].split('Port WWN:')[1].strip()
                initiator_address.append(port_wwn)

        LOG.debug(_('WWNs found for SP %s are : %s') %
                    (device_sp,initiator_address))

        return initiator_address


