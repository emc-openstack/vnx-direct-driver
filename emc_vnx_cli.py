# vim: tabstop=4 shiftwidth=4 softtabstop=4
#    Copyright (c) 2012 - 2014 EMC Corporation
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
import os
import re
import time

import random

try:
    import json
except ImportError:
    import simplejson as json

from oslo.config import cfg

from cinder import exception
from cinder.openstack.common import lockutils
from cinder.openstack.common import log as logging
from cinder.openstack.common import loopingcall
from cinder.openstack.common import processutils
from cinder import utils
from cinder.volume.drivers.san import san
from cinder.volume import volume_types

LOG = logging.getLogger(__name__)

CONF = cfg.CONF
VERSION = '03.00.01'

LOG = logging.getLogger(__name__)

TIMEOUT_1_MINUTE = 1 * 60
TIMEOUT_2_MINUTE = 2 * 60
TIMEOUT_5_MINUTE = 5 * 60
TIMEOUT_10_MINUTE = 10 * 60

INTERVAL_1_SEC = 1
INTERVAL_5_SEC = 5
INTERVAL_10_SEC = 10
INTERVAL_30_SEC = 30
INTERVAL_60_SEC = 60

loc_opts = [
    cfg.StrOpt('storage_vnx_authentication_type',
               default='global',
               help='VNX authentication scope type'),
    cfg.StrOpt('storage_vnx_security_file_dir',
               default=None,
               help='Directory path that contains the VNX security file.'
                    ' Make sure the security file is generated first'),
    cfg.StrOpt('naviseccli_path',
               default='',
               help='Naviseccli Path'),
    cfg.StrOpt('storage_vnx_pool_name',
               default=None,
               help='ISCSI pool name'),
    cfg.StrOpt('san_secondary_ip',
               default=None,
               help='VNX secondary SP IP Address'),
    cfg.IntOpt('default_timeout',
               default=-1,
               help='Default Time Out For CLI operations'),
    cfg.IntOpt('max_luns_per_storage_group',
               default=256,
               help='Default max number of LUNs in a storage group'),
    cfg.BoolOpt('destroy_empty_storage_group',
                default=False,
                help='To destroy storage group '
                'when the last LUN is removed from it'),
    cfg.StrOpt('iscsi_initiators',
               default='',
               help='Mapping between hostname and '
               'its iSCSI initiator IP addresses'),
    cfg.BoolOpt('initiator_auto_registration',
                default=False,
                help='Automatically register initiators'),
]

CONF.register_opts(loc_opts)


class EMCVnxCLICmdError(exception.VolumeBackendAPIException):
    def __init__(self, cmd, rc, out, description='', **kwargs):
        self.cmd = cmd
        self.rc = rc
        self.out = out
        msg = _("EMCVnxCLICmdError %(description)s: %(cmd)s"
                " (Return Code: %(rc)s)"
                " (Output: %(out)s)") % \
            {'description': description,
             'cmd': cmd,
             'rc': rc,
             'out': out.split('\n')}
        kwargs["data"] = msg
        super(EMCVnxCLICmdError, self).__init__(**kwargs)
        LOG.error(msg)


class PropertyDescriptor(object):
    def __init__(self, option, label, key, converter=None):
        self.option = option
        self.label = label
        self.key = key
        self.converter = converter


class CommandLineHelper(object):

    LUN_STATE = PropertyDescriptor('-state',
                                   'Current State:\s*(.*)\s*',
                                   'state')
    LUN_CAPACITY = PropertyDescriptor('-userCap',
                                      'User Capacity \(GBs\):\s*(.*)\s*',
                                      'total_capacity_gb',
                                      float)
    LUN_OWNER = PropertyDescriptor('-owner',
                                   'Current Owner:\s*SP\s*(.*)\s*',
                                   'owner')
    LUN_ATTACHEDSNAP = PropertyDescriptor('-attachedSnapshot',
                                          'Attached Snapshot:\s*(.*)\s*',
                                          'attached_snapshot')
    LUN_NAME = PropertyDescriptor('-name',
                                  'Name:\s*(.*)\s*',
                                  'lun_name')
    LUN_ID = PropertyDescriptor('-id',
                                'LOGICAL UNIT NUMBER\s*(\d+)\s*',
                                'lun_id',
                                int)
    LUN_POOL = PropertyDescriptor('-poolName',
                                  'Pool Name:\s*(.*)\s*',
                                  'pool')

    LUN_ALL = [LUN_STATE, LUN_CAPACITY, LUN_OWNER, LUN_ATTACHEDSNAP]

    POOL_TOTAL_CAPACITY = PropertyDescriptor(
        '-userCap',
        'User Capacity \(GBs\):\s*(.*)\s*',
        'total_capacity_gb',
        float)
    POOL_FREE_CAPACITY = PropertyDescriptor(
        '-availableCap',
        'Available Capacity *\(GBs\) *:\s*(.*)\s*',
        'free_capacity_gb',
        float)
    POOL_NAME = PropertyDescriptor('-name',
                                   'Pool Name:\s*(.*)\s*',
                                   'pool_name')

    POOL_ALL = [POOL_TOTAL_CAPACITY, POOL_FREE_CAPACITY]

    def __init__(self, configuration):
        configuration.append_config_values(san.san_opts)

        self.timeout = configuration.default_timeout * 60
        self.max_luns = configuration.max_luns_per_storage_group

        errormessage = ""

        #Checking for existence of naviseccli tool
        navisecclipath = configuration.naviseccli_path
        if not os.path.exists(navisecclipath):
            errormessage += (_('Could not find NAVISECCLI tool %(path)s.\n')
                             % {'path': navisecclipath})

        self.command = (navisecclipath, '-address')
        self.storage_ip = configuration.san_ip
        self.storage_vnx_ip_address_spa = configuration.san_ip
        self.storage_vnx_ip_address_spb = configuration.san_secondary_ip
        if not configuration.san_ip:
            errormessage += (_('Mandatory field configuration.san_ip \
                is not set.'))

        self.credentials = ()
        storage_username = configuration.san_login
        storage_password = configuration.san_password
        storage_auth_type = configuration.storage_vnx_authentication_type
        storage_vnx_security_file = configuration.storage_vnx_security_file_dir

        if storage_auth_type is None:
            storage_auth_type = 'global'
        elif storage_auth_type.lower() not in ('ldap', 'local', 'global'):
            errormessage += (_('Invalid VNX authentication type!\n'))
        #if there is security file path provided, use this security file
        if storage_vnx_security_file:
            self.credentials = ('-secfilepath', storage_vnx_security_file)
            LOG.info("Security file under location configured by "
                     "storage_vnx_security_file_dir is using for"
                     " authentication")
        #if there is a username/password provided, use those in the cmd line
        elif storage_username is not None and len(storage_username) > 0 and\
                storage_password is not None and len(storage_password) > 0:
            self.credentials = ('-user', storage_username,
                                '-password', storage_password,
                                '-scope', storage_auth_type)
            LOG.info("Plain text credentials are using for authentication")
        else:
            LOG.info("Neither storage_vnx_security_file_dir nor plain text"
                     " credentials is specified, security file under home"
                     " directory will be used if present")

        self.iscsi_initiator_map = None
        if configuration.iscsi_initiators:
            self.iscsi_initiator_map = \
                json.loads(configuration.iscsi_initiators)
            LOG.info(_("iscsi_initiators: %s"), self.iscsi_initiator_map)
        if errormessage.strip() != "":
            LOG.error(errormessage)
            raise exception.VolumeBackendAPIException(data=errormessage)

    def create_lun(self, poolname, name, size, thinness):
        LOG.debug(_('Entering CommandLineHelper.create_lun.'))

        command_create_lun = ['lun', '-create',
                              '-type', thinness,
                              '-capacity', size,
                              '-sq', 'gb',
                              '-poolName', poolname,
                              '-name', name]

        # executing cli command to create volume
        out, rc = self.command_execute(*command_create_lun)
        if rc != 0:
            #Ignore the error that due to retry
            if rc == 4 and out.find('(0x712d8d04)') >= 0:
                LOG.warn(_('LUN already exists, LUN name %(name)s. '
                           'Message: %(msg)s') %
                         {'name': name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_create_lun, rc, out)

        LOG.debug(_('Exiting CommandLineHelper.create_lun.'))

    def delete_lun(self, name):
        LOG.debug(_('Entering CommandLineHelper.delete_lun.'))

        command_delete_lun = ['lun', '-destroy',
                              '-name', name,
                              '-forceDetach',
                              '-o']
        # executing cli command to delete volume
        out, rc = self.command_execute(*command_delete_lun)
        if rc != 0:
            #Ignore the error that due to retry
            if rc == 9 and out.find("not exist") >= 0:
                LOG.warn(_("LUN already deleted, LUN name %(name)s. "
                           "Message: %(msg)s") %
                         {'name': name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_delete_lun, rc, out)

        LOG.debug(_('Exiting CommandLineHelper.delete_lun.'))

    def _wait_for_a_condition(self, testmethod, start_time, timeout=None):
        if timeout is None:
            timeout = self.timeout
        try:
            testValue = testmethod()
        except Exception as ex:
            testValue = False
            LOG.debug(_('CommandLineHelper.'
                        '_wait_for_condition: %(method_name)s '
                        'execution failed for %(exception)s')
                      % {'method_name': testmethod.__name__,
                         'exception': ex})
        if testValue:
            raise loopingcall.LoopingCallDone()

        if int(time.time()) - start_time > timeout:
            msg = (_('CommandLineHelper._wait_for_condition: %s '
                     'timeout') % testmethod.__name__)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def create_lun_and_wait(self, poolname, name, size, thinness):
        LOG.debug(_('Entering CommandLineHelper.create_lun_and_wait.'))

        self.create_lun(poolname, name, size, thinness)

        def lun_is_ready():
            data = self.get_lun_by_name(name)
            return data[self.LUN_STATE.key] == 'Ready'

        timer = loopingcall.FixedIntervalLoopingCall(
            self._wait_for_a_condition,
            lun_is_ready, int(time.time()))
        timer.start(interval=INTERVAL_5_SEC).wait()
        LOG.debug(_('Exiting CommandLineHelper.create_lun_and_wait.'))

    def expand_lun(self, name, new_size):
        LOG.debug(_('Entering CommandLineHelper.expand_lun.'))

        command_expand_lun = ('lun', '-expand',
                              '-name', name,
                              '-capacity', new_size,
                              '-sq', 'gb',
                              '-o',
                              '-ignoreThresholds')
        out, rc = self.command_execute(*command_expand_lun)
        if rc != 0:
            #Ignore the error that due to retry
            if rc == 4 and out.find("(0x712d8e04)") >= 0:
                LOG.warn(_("Size of LUN %(name)s is already expanded."
                           " Message: %(msg)s") %
                         {'name': name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_expand_lun, rc, out)

        LOG.debug(_('Exiting CommandLineHelper.expand_lun.'))

    def expand_lun_and_wait(self, name, new_size):
        LOG.debug(_('Entering CommandLineHelper.expand_lun_and_wait.'))

        self.expand_lun(name, new_size)

        def lun_is_extented():
            data = self.get_lun_by_name(name)
            return new_size == data[self.LUN_CAPACITY.key]

        timer = loopingcall.FixedIntervalLoopingCall(
            self._wait_for_a_condition,
            lun_is_extented, int(time.time()))
        timer.start(interval=INTERVAL_5_SEC).wait()
        LOG.debug(_('Exiting CommandLineHelper.expand_lun_and_wait.'))

    def create_snapshot(self, volume_name, name):
        LOG.debug(_('Entering CommandLineHelper.create_snapshot.'))

        data = self.get_lun_by_name(volume_name)
        if data[self.LUN_ID.key] is not None:
            command_create_snapshot = ('snap', '-create',
                                       '-res', data[self.LUN_ID.key],
                                       '-name', name,
                                       '-allowReadWrite', 'yes',
                                       '-allowAutoDelete', 'no')

            out, rc = self.command_execute(*command_create_snapshot)
            if rc != 0:
                #Ignore the error that due to retry
                if rc == 5 and \
                        out.find("(0x716d8005)") >= 0:
                    LOG.warn(_('Snapshot %(name)s already exists.'
                               ' Message: %(msg)s') %
                             {'name': name, 'msg': out})
                else:
                    raise EMCVnxCLICmdError(command_create_snapshot, rc, out)
        else:
            msg = _('Failed to get LUN ID for volume %s') % volume_name
            raise exception.VolumeBackendAPIException(data=msg)

        LOG.debug(_('Exiting CommandLineHelper.create_snapshot.'))

    def delete_snapshot(self, name):
        LOG.debug(_('Entering CommandLineHelper.delete_snapshot.'))

        def delete_snapshot_success():
            command_delete_snapshot = ('snap', '-destroy',
                                       '-id', name,
                                       '-o')
            out, rc = self.command_execute(*command_delete_snapshot)
            if rc != 0:
                #Ignore the error that due to retry
                if rc == 5 and out.find("not exist") >= 0:
                    LOG.warn(_("Snapshot %(name)s may deleted already."
                               " Message: %(msg)s") %
                             {'name': name, 'msg': out})
                    return True
                #The snapshot cannot be destroyed because it is
                #attached to a snapshot mount point. Wait
                elif rc == 3 and out.find("(0x716d8003)") >= 0:
                    return False
                else:
                    raise EMCVnxCLICmdError(command_delete_snapshot, rc, out)
            else:
                LOG.info(_('Snapshot %s is deleted successfully.') %
                         name)
                return True

        timer = loopingcall.FixedIntervalLoopingCall(
            self._wait_for_a_condition,
            delete_snapshot_success, int(time.time()))
        timer.start(interval=INTERVAL_30_SEC).wait()

        LOG.debug(_('Exiting CommandLineHelper.delete_snapshot.'))

    def create_mount_point(self, primary_lun_name, name):
        LOG.debug(_('Entering CommandLineHelper.create_mount_point.'))

        command_create_mount_point = ('lun', '-create',
                                      '-type', 'snap',
                                      '-primaryLunName', primary_lun_name,
                                      '-name', name)

        out, rc = self.command_execute(*command_create_mount_point)
        if rc != 0:
            #Ignore the error that due to retry
            if rc == 4 and out.find("(0x712d8d04)") >= 0:
                LOG.warn(_("Mount points %(name)s already exists. "
                           "Message: %(msg)s") %
                         {'name': name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_create_mount_point, rc, out)

        LOG.debug(_('Exiting CommandLineHelper.create_mount_point.'))

        return rc

    def attach_mount_point(self, name, snapshot_name):
        LOG.debug(_('Entering CommandLineHelper.attach_mount_point.'))

        command_attach_mount_point = ('lun', '-attach',
                                      '-name', name,
                                      '-snapName', snapshot_name)

        out, rc = self.command_execute(*command_attach_mount_point)
        if rc != 0:
            #Ignore the error that due to retry
            if rc == 85 and out.find('(0x716d8055)') >= 0:
                LOG.warn(_("Snapshot %(snapname)s is attached to snapshot "
                           "mount point %(mpname)s already. "
                           "Message: %(msg)s") %
                         {'snapname': snapshot_name,
                          'mpname': name,
                          'msg': out})
            else:
                raise EMCVnxCLICmdError(command_attach_mount_point, rc, out)

        LOG.debug(_('Exiting CommandLineHelper.attach_mount_point.'))

        return rc

    def check_smp_not_attached(self, smp_name):
        """Ensure a snap mount point with snap become a LUN."""
        LOG.debug(_('Entering CommandLineHelper.check_smp_not_attached.'))

        def _wait_for_sync_status():
            lun_list = ('lun', '-list', '-name', smp_name,
                        '-attachedSnapshot')
            out, rc = self.command_execute(*lun_list)
            if rc == 0:
                vol_details = out.split('\n')
                snap_name = vol_details[2].split(':')[1].strip()
            if (snap_name == 'N/A'):
                return True
            return False

        timer = loopingcall.FixedIntervalLoopingCall(
            self._wait_for_a_condition, _wait_for_sync_status,
            int(time.time()))
        timer.start(interval=INTERVAL_5_SEC).wait()
        LOG.debug(_('Exiting CommandLineHelper.check_smp_not_attached.'))

    def migrate_lun(self, src_id, dst_id):
        LOG.debug(_('Entering CommandLineHelper.migrate_lun.'))
        command_migrate_lun = ('migrate', '-start',
                               '-source', src_id,
                               '-dest', dst_id,
                               '-rate', 'ASAP',
                               '-o')
        #SP HA is not supported by LUN migration
        out, rc = self.command_execute(*command_migrate_lun,
                                       retry_disable=True)

        if 0 != rc:
            raise EMCVnxCLICmdError(command_migrate_lun, rc, out)
        LOG.debug(_('Exiting CommandLineHelper.migrate_lun.'))

        return rc

    def migrate_lun_with_verification(self, src_id,
                                      dst_id=None,
                                      dst_name=None):
        LOG.debug(_('Entering CommandLineHelper.'
                    'migrate_lun_with_verification.'))
        try:
            self.migrate_lun(src_id, dst_id)
        except EMCVnxCLICmdError as ex:
            migration_succeed = False
            if self._is_sp_unavailable_error(ex.out):
                LOG.warn(_("Migration command timeout. Verify migration"
                           " status continuously. Message: %(msg)s") %
                         {'msg': ex.out})
                migrate_lun_with_verification = ('migrate', '-list',
                                                 '-source', src_id)
                out, rc = self.command_execute(*migrate_lun_with_verification)
                if rc == 0:
                    migration_succeed = True

            if not migration_succeed:
                LOG.error("Migration start failed, trying to "
                          "remove the temp LUN")
                LOG.error(_("Start migration failed, trying to delete %s") %
                          dst_name)
                if(dst_name is not None):
                    self.delete_lun(dst_name)
                return 1

        query_interval = INTERVAL_5_SEC
        migrate_timeout = TIMEOUT_10_MINUTE \
            if not self.timeout else self.timeout

        # Set the proper interval to verify the migration status
        def migration_is_ready():
            mig_ready = False
            migrate_lun_with_verification = ('migrate', '-list',
                                             '-source', src_id)
            out, rc = self.command_execute(*migrate_lun_with_verification)
            LOG.debug(_("Migration output: %s") % out)
            if rc == 0:
                #parse the percentage
                out = re.split(r'\n', out)
                log = _("Migration in process %s %%.") % out[7].split(":  ")[1]
                LOG.debug(log)
            else:
                if re.search(r'The specified source LUN '
                             'is not currently migrating', out):
                    LOG.debug(_("Migration is ready."))
                    mig_ready = True
                else:
                    LOG.debug(_("Querying migrating status error."))
                    raise exception.VolumeBackendAPIException(
                        data="Querying migrating status error:"
                        + out)
            return mig_ready

        timer = loopingcall.FixedIntervalLoopingCall(
            self._wait_for_a_condition,
            migration_is_ready,
            int(time.time()), migrate_timeout)
        timer.start(interval=query_interval).wait()

        LOG.debug(_('Exiting CommandLineHelper.'
                    'migrate_lun_with_verification.'))
        return 0

    def get_storage_group(self, name):
        LOG.debug(_('Entering CommandLineHelper.get_storage_group.'))

        # ALU/HLU as key/value map
        lun_map = {}

        data = {'storage_group_name': name,
                'storage_group_uid': None,
                'lunmap': lun_map}

        command_get_storage_group = ('storagegroup', '-list',
                                     '-gname', name)

        out, rc = self.command_execute(*command_get_storage_group)
        if rc != 0:
            raise EMCVnxCLICmdError(command_get_storage_group, rc, out)

        re_stroage_group_id = 'Storage Group UID:\s*(.*)\s*'
        m = re.search(re_stroage_group_id, out)
        if m is not None:
            data['storage_group_uid'] = m.group(1)

        re_HLU_ALU_pair = 'HLU\/ALU Pairs:\s*HLU Number' \
                          '\s*ALU Number\s*[-\s]*(?P<lun_details>(\d+\s*)+)'
        m = re.search(re_HLU_ALU_pair, out)
        if m is not None:
            lun_details = m.group('lun_details').strip()
            values = re.split('\s*', lun_details)
            while (len(values) >= 2):
                key = values.pop()
                value = values.pop()
                lun_map[int(key)] = int(value)

        LOG.debug(_('Exiting CommandLineHelper.get_storage_group.'))

        return data

    def create_storage_group(self, name):
        LOG.debug(_('Entering CommandLineHelper.create_storage_group.'))

        command_create_storage_group = ('storagegroup', '-create',
                                        '-gname', name)

        out, rc = self.command_execute(*command_create_storage_group)
        if rc != 0:
            #Ignore the error that due to retry
            if rc == 66 and out.find("name already in use") >= 0:
                LOG.warn(_('Storage group %(name)s already exsited. '
                           'Message: %(msg)s') %
                         {'name': name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_create_storage_group, rc, out)

        LOG.debug(_('Exiting CommandLineHelper.create_storage_group.'))

    def delete_storage_group(self, name):
        LOG.debug(_('Entering CommandLineHelper.delete_storage_group.'))

        command_delete_storage_group = ('storagegroup', '-destroy',
                                        '-gname', name, '-o')

        out, rc = self.command_execute(*command_delete_storage_group)
        if rc != 0:
            #Ignore the error that due to retry
            if rc == 83 and out.find("group name or UID does not"
                                     " match any storage groups") >= 0:
                LOG.warn(_("Storage group %(name)s has already been deleted."
                           " Message: %(msg)s") %
                         {'name': name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_delete_storage_group, rc, out)

        LOG.debug(_('Exiting CommandLineHelper.delete_storage_group.'))

    def connect_host_to_storage_group(self, hostname, sg_name):
        LOG.debug(_('Entering CommandLineHelper.'
                    'connect_host_to_storage_group.'))

        command_host_connect = ('storagegroup', '-connecthost',
                                '-host', hostname,
                                '-gname', sg_name,
                                '-o')

        out, rc = self.command_execute(*command_host_connect)
        if rc != 0:
            raise EMCVnxCLICmdError(command_host_connect, rc, out)

        LOG.debug(_('Exiting CommandLineHelper.'
                    'connect_host_to_storage_group.'))

    def disconnect_host_from_storage_group(self, hostname, sg_name):
        LOG.debug(_('Entering CommandLineHelper.'
                    'disconnect_host_from_storage_group.'))

        command_host_disconnect = ('storagegroup', '-disconnecthost',
                                   '-host', hostname,
                                   '-gname', sg_name,
                                   '-o')

        out, rc = self.command_execute(*command_host_disconnect)
        if rc != 0:
            #Ignore the error that due to retry
            if rc == 116 and \
                re.search("host is not.*connected to.*storage group",
                          out) is not None:
                LOG.warn(_("Host %(host)s has already disconnected from "
                           "storage group %(sgname)s. Message: %(msg)s") %
                         {'host': hostname, 'sgname': sg_name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_host_disconnect, rc, out)

        LOG.debug(_('Exiting CommandLineHelper.'
                    'disconnect_host_from_storage_group.'))

    def add_hlu_to_storage_group(self, hlu, alu, sg_name):
        LOG.debug(_('Entering CommandLineHelper.add_hlu_to_storage_group.'))

        command_add_hlu = ('storagegroup', '-addhlu',
                           '-hlu', hlu,
                           '-alu', alu,
                           '-gname', sg_name)

        out, rc = self.command_execute(*command_add_hlu)
        if rc != 0:
            #Ignore the error that due to retry
            if rc == 66 and \
                    re.search("LUN.*already.*added to.*Storage Group",
                              out) is not None:
                LOG.warn(_("LUN %(lun)s has already added to "
                           "Storage Group %(sgname)s."
                           " Message: %(msg)s") %
                         {'lun': alu, 'sgname': sg_name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_add_hlu, rc, out)

        LOG.debug(_('Exiting CommandLineHelper.add_hlu_to_storage_group.'))

    def remove_hlu_from_storagegroup(self, hlu, sg_name):
        LOG.debug(_('Entering CommandLineHelper.'
                    'remove_hlu_from_storagegroup.'))

        command_remove_hlu = ('storagegroup', '-removehlu',
                              '-hlu', hlu,
                              '-gname', sg_name,
                              '-o')

        out, rc = self.command_execute(*command_remove_hlu)
        if rc != 0:
            #Ignore the error that due to retry
            if rc == 66 and\
                    out.find("No such Host LUN in this Storage Group") >= 0:
                LOG.warn(_("HLU %(hlu)s has already removed from %(sgname)s. "
                           "Message: %(msg)s") %
                         {'hlu': hlu, 'sgname': sg_name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_remove_hlu, rc, out)

        LOG.debug(_('Exiting CommandLineHelper.remove_hlu_from_storagegroup.'))

    def get_iscsi_protocol_endpoints(self, device_sp):
        LOG.debug(_('Entering CommandLineHelper.'
                    'get_iscsi_protocol_endpoints.'))

        command_get_port = ('connection', '-getport',
                            '-sp', device_sp)

        out, rc = self.command_execute(*command_get_port)
        if rc != 0:
            raise EMCVnxCLICmdError(command_get_port, rc, out)

        re_port_wwn = 'Port WWN:\s*(.*)\s*'
        initiator_address = re.findall(re_port_wwn, out)

        LOG.debug(_('Exiting CommandLineHelper.get_iscsi_protocol_endpoints.'))

        return initiator_address

    def get_lun_by_name(self, name, properties=LUN_ALL):
        LOG.debug(_('Entering CommandLineHelper.get_lun_by_name.'))

        data = self.get_lun_properties(('-name', name), properties)
        LOG.debug(_('Exiting CommandLineHelper.get_lun_by_name.'))
        return data

    def get_lun_by_id(self, lunid, properties=LUN_ALL):
        LOG.debug(_('Entering CommandLineHelper.get_lun_by_id.'))
        data = self.get_lun_properties(('-l', lunid), properties)
        LOG.debug(_('Exiting CommandLineHelper.get_lun_by_id.'))
        return data

    def get_pool(self, name):
        LOG.debug(_('Entering CommandLineHelper.get_pool.'))
        data = self.get_pool_properties(('-name', name))
        LOG.debug(_('Exiting CommandLineHelper.get_pool.'))

        return data

    def get_pool_properties(self, filter_option, properties=POOL_ALL):
        module_list = ('storagepool', '-list')
        return self.get_lun_or_pool_properties(
            module_list, filter_option,
            base_properties=[self.POOL_NAME],
            adv_properties=properties)

    def get_lun_properties(self, filter_option, properties=LUN_ALL):
        module_list = ('lun', '-list')
        return self.get_lun_or_pool_properties(
            module_list, filter_option,
            base_properties=[self.LUN_NAME, self.LUN_ID],
            adv_properties=properties)

    def get_lun_or_pool_properties(self, module_list,
                                   filter_option,
                                   base_properties=[],
                                   adv_properties=[]):
        # to do instance check
        command_get_lun = module_list + filter_option
        for prop in adv_properties:
            command_get_lun += (prop.option, )
        out, rc = self.command_execute(*command_get_lun)

        if rc != 0:
            raise EMCVnxCLICmdError(command_get_lun, rc, out)

        data = {}
        for baseprop in base_properties:
            data[baseprop.key] = self._get_property_value(out, baseprop)

        for prop in adv_properties:
            data[prop.key] = self._get_property_value(out, prop)

        LOG.debug(data)
        LOG.debug('Exit get lun propeties')
        return data

    def _get_property_value(self, out, propertyDescriptor):
        label = propertyDescriptor.label
        m = re.search(label, out)
        if m:
            if (propertyDescriptor.converter is not None):
                try:
                    return propertyDescriptor.converter(m.group(1))
                except ValueError:
                    LOG.error("Invalid value for %(key)s, value is %(value)s" %
                              {'key': propertyDescriptor.key,
                               'value': m.group(1)})
                    return None
            else:
                return m.group(1)
        else:
            LOG.debug('%s value is not found in the output'
                      % propertyDescriptor.label)
            return None

    #Return a pool list
    def get_pool_list(self):
        temp_cache = []
        cmd = ('storagepool', '-list', '-availableCap', '-state')
        out, rc = self.command_execute(*cmd)
        if rc != 0:
            raise EMCVnxCLICmdError(cmd, rc, out)

        try:
            for pool in out.split('\n\n'):
                if len(pool.strip()) == 0:
                    continue
                obj = {}
                obj['name'] = self._get_property_value(pool, self.POOL_NAME)
                obj['free_space'] = self._get_property_value(
                    pool, self.POOL_FREE_CAPACITY)
                temp_cache.append(obj)
        except Exception as ex:
            LOG.error("Error happens during storage pool querying, %s"
                      % ex)
            #NOTE: Do not want to continue raise the exception
            #as the pools may temporarly unavailable
            pass
        return temp_cache

    def get_array_serial(self):
        """return array Serial No for pool backend."""
        LOG.debug(_('Entering CommandLineHelper.get_array_serial.'))
        stats = {'array_serial': 'unknown'}

        command_get_array_serial = ('getagent', '-serial')
        # Set the property timeout to get array serial
        out, rc = self.command_execute(*command_get_array_serial)
        if 0 == rc:
            m = re.search(r'Serial No:\s+(\w+)', out)
            if m:
                stats['array_serial'] = m.group(1)
            else:
                LOG.warn("No array serial returned, set as unknown")
        else:
            raise EMCVnxCLICmdError(command_get_array_serial, rc, out)

        LOG.debug(_('Exiting CommandLineHelper.get_array_serial.'))
        return stats

    def get_target_wwns(self, storage_group_name):
        """Function to get target port wwns."""

        LOG.debug(_('Entering CommandLineHelper.get_target_wwns.'))
        cmd_get_hba = ('storagegroup', '-list', '-gname', storage_group_name)
        out, rc = self.command_execute(*cmd_get_hba)
        wwns = []
        if 0 == rc:
            _re_hba_sp_pair = re.compile('((\w\w:){15}(\w\w)\s*' +
                                         '(SP\s[A-B]){1}\s*(\d*)\s*\n)')
            _all_hba_sp_pairs = re.findall(_re_hba_sp_pair, out)
            sps = [each[3] for each in _all_hba_sp_pairs]
            portid = [each[4] for each in _all_hba_sp_pairs]
            cmd_get_port = ('port', '-list', '-sp')
            out, rc = self.command_execute(*cmd_get_port)
            if 0 != rc:
                raise EMCVnxCLICmdError(cmd_get_port, rc, out)
            for i, sp in enumerate(sps):
                wwn = self.get_port_wwn(sp, portid[i], out)
                if (wwn is not None) and (wwn not in wwns):
                    LOG.debug(_('Add wwn:%(wwn)s for sg:%(sg)s.')
                              % {'wwn': wwn,
                                 'sg': storage_group_name})
                    wwns.append(wwn)
        else:
            raise EMCVnxCLICmdError(cmd_get_hba, rc, out)
        LOG.debug(_('Exiting Command.get_target_wwns.'))
        return wwns

    def get_port_wwn(self, sp, port_id, allports=None):
        LOG.debug(_('Entering CommandLineHelper.get_port_wwn.'))
        wwn = None
        if allports is None:
            cmd_get_port = ('port', '-list', '-sp')
            out, rc = self.command_execute(*cmd_get_port)
            if 0 != rc:
                raise EMCVnxCLICmdError(cmd_get_port, rc, out)
            else:
                allports = out
        _re_port_wwn = re.compile('SP Name:\s*' + sp +
                                  '\nSP Port ID:\s*' + port_id +
                                  '\nSP UID:\s*((\w\w:){15}(\w\w))' +
                                  '\nLink Status:         Up' +
                                  '\nPort Status:         Online')
        _obj_search = re.search(_re_port_wwn, allports)
        if _obj_search is not None:
            wwn = _obj_search.group(1).replace(':', '')[16:]
        LOG.debug(_('Exiting Command.get_port_wwn.'))
        return wwn

    def get_fc_targets(self):
        fc_getport = ('port', '-list', '-sp')
        out, rc = self.command_execute(*fc_getport)
        if rc != 0:
            raise EMCVnxCLICmdError(fc_getport, rc, out)
        else:
            fc_target_dict = {'A': [], 'B': []}

            _fcport_pat = (r'SP Name:             SP\s(\w)\s*'
                           r'SP Port ID:\s*(\w*)\n'
                           r'SP UID:\s*((\w\w:){15}(\w\w))\s*'
                           r'Link Status:         Up\n'
                           r'Port Status:         Online\n')

            for m in re.finditer(_fcport_pat, out):
                sp = m.groups()[0]
                sp_port_id = m.groups()[1]
                fc_target_dict[sp].append({'SP': sp,
                                           'Port ID': sp_port_id})
            return fc_target_dict

    def get_iscsi_targets(self):
        cmd_getport = ('connection', '-getport', '-address', '-vlanid')
        out, rc = self.command_execute(*cmd_getport)
        if rc != 0:
            raise EMCVnxCLICmdError(cmd_getport, rc, out)
        else:
            iscsi_target_dict = {'A': [], 'B': []}
            iscsi_spport_pat = r'(A|B)\s*' + \
                               r'Port ID:\s+(\d+)\s*' + \
                               r'Port WWN:\s+(iqn\S+)'
            iscsi_vport_pat = r'Virtual Port ID:\s+(\d+)\s*' + \
                              r'VLAN ID:\s*\S*\s*' + \
                              r'IP Address:\s+(\S+)'
            for spport_content in re.split(r'^SP:\s+|\nSP:\s*', out):
                m_spport = re.match(iscsi_spport_pat, spport_content,
                                    flags=re.IGNORECASE)
                if not m_spport:
                    continue
                sp = m_spport.group(1)
                port_id = int(m_spport.group(2))
                iqn = m_spport.group(3)
                for m_vport in re.finditer(iscsi_vport_pat, spport_content):
                    vport_id = int(m_vport.group(1))
                    ip_addr = m_vport.group(2)
                    if ip_addr.find('N/A') != -1:
                        LOG.info(_("Skip port without IP Address: %s"),
                                 m_spport.group(0) + m_vport.group(0))
                        continue
                    iscsi_target_dict[sp].append({'SP': sp,
                                                  'Port ID': port_id,
                                                  'Port WWN': iqn,
                                                  'Virtual Port ID': vport_id,
                                                  'IP Address': ip_addr})
            return iscsi_target_dict

    def get_registered_spport_set(self, initiator_iqn, sgname):
        sg_list = ('storagegroup', '-list', '-gname', sgname)
        out, rc = self.command_execute(*sg_list)
        spport_set = set()
        if rc == 0:
            for m_spport in re.finditer(r'\n\s+%s\s+SP\s(A|B)\s+(\d+)' %
                                        initiator_iqn,
                                        out,
                                        flags=re.IGNORECASE):
                spport_set.add((m_spport.group(1), int(m_spport.group(2))))
                LOG.debug(_('See path %(path)s in %(sg)s')
                          % ({'path': m_spport.group(0),
                              'sg': sgname}))
        else:
            raise EMCVnxCLICmdError(sg_list, rc, out)
        return spport_set

    def ping_node(self, target_portal, initiator_ip):
        connection_pingnode = ('connection', '-pingnode', '-sp',
                               target_portal['SP'], '-portid',
                               target_portal['Port ID'], '-vportid',
                               target_portal['Virtual Port ID'],
                               '-address', initiator_ip)
        out, rc = self.command_execute(*connection_pingnode)
        if rc == 0:
            ping_ok = re.compile(r'Reply from %s' % initiator_ip)
            if re.match(ping_ok, out) is not None:
                LOG.debug(_("See available iSCSI target: %s"),
                          connection_pingnode)
                return True
        LOG.warn(_("See unavailable iSCSI target: %s"), connection_pingnode)
        return False

    def find_avaialable_iscsi_target_one(self, hostname,
                                         preferred_sp,
                                         registered_spport_set):
        if self.iscsi_initiator_map and hostname in self.iscsi_initiator_map:
            iscsi_initiator_ips = list(self.iscsi_initiator_map[hostname])
            random.shuffle(iscsi_initiator_ips)
        else:
            iscsi_initiator_ips = None
        # Check the targets on the owner first
        if preferred_sp == 'A':
            target_sps = ('A', 'B')
        else:
            target_sps = ('B', 'A')

        for target_sp in target_sps:
            iscsi_targets = self.get_iscsi_targets()
            target_portals = list(iscsi_targets[target_sp])
            random.shuffle(target_portals)
            for target_portal in target_portals:
                spport = (target_portal['SP'], target_portal['Port ID'])
                if spport not in registered_spport_set:
                    LOG.debug(_("Skip SP Port %(port)s since "
                                "no path from %(host)s is through it")
                              % {'port': spport,
                                 'host': hostname})
                    continue
                if iscsi_initiator_ips is not None:
                    for initiator_ip in iscsi_initiator_ips:
                        if self.ping_node(target_portal, initiator_ip):
                            return target_portal
                else:
                    LOG.debug(_("No iSCSI IP addresses of %s is known"),
                              hostname)
                    LOG.debug(_("Return a random iSCSI target portal %s"),
                              target_portal)
                    return target_portal

    def _is_sp_unavailable_error(self, out):
        error_pattern = '(^Error.*Message.*End of data stream.*)|'\
                        '(.*Message.*connection refused.*)|'\
                        '(^Error.*Message.*Service Unavailable.*)'
        pattern = re.compile(error_pattern)
        return pattern.match(out)

    def command_execute(self, *command, **kwargv):
        if "check_exit_code" not in kwargv:
            kwargv["check_exit_code"] = True
        #NOTE: retry_disable need to be removed from kwargv
        #before it pass to utils.execute, otherwise exception will thrown
        retry_disable = kwargv.pop('retry_disable', False)
        try_times = 1 if retry_disable else 2

        rc = 0
        out = ""
        is_sp_alive = self._determine_if_sp_alive(**kwargv)
        if not is_sp_alive:
            rc = 255
            out = "Server unreachable"
            return out, rc

        num_of_tries = 0
        while num_of_tries < try_times:
            try:
                rc = 0
                need_terminated = False
                new_sp = (self.storage_ip,)
                out, err = utils.execute(
                    *(self.command + new_sp + self.credentials + command),
                    **kwargv)
                if not need_terminated:
                    break
            except processutils.ProcessExecutionError as pe:
                rc = pe.exit_code
                out = pe.stdout
                out = out.replace('\n', ' ')
                LOG.debug(_('EMC: Exception out trace: %s') % out)
                if self._is_sp_unavailable_error(out):
                    is_sp_toggled = self._toggle_ip()
                    if not is_sp_toggled:
                        need_terminated = True
                else:
                    need_terminated = True
            finally:
                if need_terminated:
                    break
                num_of_tries += 1

        LOG.debug(_('EMC: Command: %(command)s')
                  % {'command': self.command + self.credentials + command})
        LOG.debug(_('EMC: Command Result: %(result)s') % {'result': out})

        return out, rc

    def _determine_if_sp_alive(self, **kwargv):
        """This function determine if the sp is alive before
        issuing the command, otherwise toggle to the other sp
        """
        if "check_exit_code" not in kwargv:
            kwargv["check_exit_code"] = True

        rc = 0
        out = ""
        ping_cmd = ('ping', '-c', 1, self.storage_ip)

        try:
            out, err = utils.execute(*ping_cmd, **kwargv)
        except processutils.ProcessExecutionError as pe:
            out = pe.stdout
            rc = pe.exit_code
            if rc != 0:
                return self._toggle_ip()

        LOG.debug(_('EMC: Command Result: %(result)s') % {'result': out})
        return True

    def _toggle_ip(self):
        """This function toggels the storage IP
        Address between SPA and SPB, if no SP IP address has
        exchanged, return False, otherwise True will be returned.
        """
        old_sp = self.storage_ip
        if self.storage_ip != self.storage_vnx_ip_address_spa\
                and self.storage_vnx_ip_address_spa:
            self.storage_ip = self.storage_vnx_ip_address_spa
        elif self.storage_ip != self.storage_vnx_ip_address_spb\
                and self.storage_vnx_ip_address_spb:
            self.storage_ip = self.storage_vnx_ip_address_spb

        new_sp = self.storage_ip
        if(old_sp != new_sp):
            LOG.info(_('Toggle storage_vnx_ip_adress from %(old)s to '
                       '%(new)s')
                     % {'old': old_sp,
                        'new': new_sp})
            return True
        else:
            return False


class EMCVnxCliBase(object):
    """This class defines the functions to use the native CLI functionality."""

    stats = {'driver_version': VERSION,
             'free_capacity_gb': 'unknown',
             'reserved_percentage': 0,
             'storage_protocol': None,
             'total_capacity_gb': 'unknown',
             'vendor_name': 'EMC',
             'volume_backend_name': None}

    def __init__(self, prtcl, configuration=None):
        self.protocol = prtcl
        self.configuration = configuration
        self.timeout = self.configuration.default_timeout
        self.max_luns_per_sg = self.configuration.max_luns_per_storage_group
        self.destroy_empty_sg = self.configuration.destroy_empty_storage_group
        self.itor_auto_reg = self.configuration.initiator_auto_registration
        self.max_retries = 5
        if self.destroy_empty_sg:
            LOG.warn(_("destroy_empty_storage_group=True"))
        if not self.itor_auto_reg:
            LOG.warn(_("initiator auto registration not enabled"))
        self.hlu_set = set(xrange(1, self.max_luns_per_sg + 1))
        self._client = CommandLineHelper(self.configuration)

    def get_target_storagepool(self, volume, source_volume_name=None):
        raise NotImplementedError

    def create_volume(self, volume):
        """Creates a EMC volume."""
        LOG.debug(_('Entering create_volume.'))
        volumesize = volume['size']
        volumename = volume['name']

        LOG.info(_('Create Volume: %(volume)s  Size: %(size)s')
                 % {'volume': volumename,
                    'size': volumesize})

        #defining CLI command
        thinness = self._get_provisioning_by_volume(volume)
        storagepool = self.get_target_storagepool(volume)

        self._client.create_lun_and_wait(
            storagepool, volumename, volumesize, thinness)

    def delete_volume(self, volume):
        """Deletes an EMC volume."""

        LOG.debug(_('Entering delete_volume.'))
        self._client.delete_lun(volume['name'])

    def extend_volume(self, volume, new_size):
        """Extends an EMC volume."""

        LOG.debug(_('Entering extend_volume.'))

        self._client.expand_lun_and_wait(volume['name'], new_size)

    def migrate_volume(self, ctxt, volume, host):
        """leverage the VNX on-array migration functionality, \
           here is entry in source Backend.
        """
        false_ret = (False, None)
        if 'location_info' not in host['capabilities']:
            return false_ret

        info = host['capabilities']['location_info']
        try:
            info_detail = info.split('|')
            target_pool_name = info_detail[0]
            target_array_serial = info_detail[1]
        except AttributeError:
            return false_ret
        #target should not be a array backend
        if len(target_pool_name) == 0:
            return false_ret
        #source and destination should be on same array
        array_serial = self._client.get_array_serial()
        if target_array_serial != array_serial['array_serial']:
            LOG.warn(_('Not on same array, '
                       'skipping storage-assisted migration.'))
            return false_ret
        if host['capabilities']['storage_protocol'] != self.protocol \
           and volume['status'] != 'available':
            LOG.debug(_('only available volume '
                        'can be migrate between diff protocol'))
            return false_ret
        LOG.debug(_("Starting real storage-assisted migration..."))
        #first create a new volume with same name and size of source volume
        new_volume_name = "%(src)s-%(ts)s" % {'src': volume['name'],
                                              'ts': int(time.time())}
        src_id = self.get_lun_id(volume['name'])

        thinness = self._get_provisioning_by_volume(volume)
        self._client.create_lun_and_wait(
            target_pool_name,
            new_volume_name, volume['size'], thinness)
        dst_id = self.get_lun_id(new_volume_name)
        rc = self._client.migrate_lun_with_verification(
            src_id, dst_id, new_volume_name)
        moved = False
        if rc == 0:
            moved = True
        return moved, {}

    def update_volume_status(self):
        return self.stats

    def create_export(self, context, volume):
        """Driver entry point to get the export info for a new volume."""
        volumename = volume['name']

        data = self._client.get_lun_by_name(volumename)

        device_id = data['lun_id']

        LOG.debug(_('create_export: Volume: %(volume)s  Device ID: '
                  '%(device_id)s')
                  % {'volume': volumename,
                     'device_id': device_id})

        return {'provider_location': device_id}

    def create_snapshot(self, snapshot):
        """Creates a snapshot."""
        LOG.debug(_('Entering create_snapshot.'))

        snapshotname = snapshot['name']
        volumename = snapshot['volume_name']

        LOG.info(_('Create snapshot: %(snapshot)s: volume: %(volume)s')
                 % {'snapshot': snapshotname,
                    'volume': volumename})

        self._client.create_snapshot(volumename, snapshotname)

    def delete_snapshot(self, snapshot):
        """Deletes a snapshot."""
        LOG.debug(_('Entering delete_snapshot.'))

        snapshotname = snapshot['name']

        LOG.info(_('Delete Snapshot: %(snapshot)s')
                 % {'snapshot': snapshotname})

        self._client.delete_snapshot(snapshotname)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot."""
        LOG.debug(_('Entering create_volume_from_snapshot.'))
        snapshot_name = snapshot['name']
        source_volume_name = snapshot['volume_name']
        volume_name = volume['name']
        volume_size = snapshot['volume_size']

        #defining CLI command
        self._client.create_mount_point(source_volume_name, volume_name)

        #defining CLI command
        self._client.attach_mount_point(volume_name, snapshot_name)

        dest_volume_name = volume_name + '_dest'

        LOG.info(_('Creating Temporary Volume : %s ') % (dest_volume_name))
        poolname = self.get_target_storagepool(volume, source_volume_name)
        thinness = self._get_provisioning_by_volume(volume)

        try:
            self._client.create_lun_and_wait(
                poolname, dest_volume_name, volume_size, thinness)
        except exception.VolumeBackendAPIException as ex:
            msg = (_(' Command to create the temporary Volume failed'))
            LOG.error(msg)
            raise ex

        source_vol_lun_id = self.get_lun_id(volume['name'])
        temp_vol_lun_id = self.get_lun_id(dest_volume_name)

        LOG.info(_('Migrating Mount Point Volume: %s ') % (volume_name))
        self._client.migrate_lun_with_verification(source_vol_lun_id,
                                                   temp_vol_lun_id, None)
        self._client.check_smp_not_attached(volume_name)

    def create_cloned_volume(self, volume, src_vref):
        """Creates a clone of the specified volume."""

        source_volume_name = src_vref['name']
        volume_size = src_vref['size']
        snapshot_name = 'tmp-snap-%s' % volume['id']

        snapshot = {
            'name': snapshot_name,
            'volume_name': source_volume_name,
            'volume_size': volume_size,
        }
        #Create temp Snapshot
        self.create_snapshot(snapshot)
        #Create volume
        self.create_volume_from_snapshot(volume, snapshot)
        #Delete temp Snapshot
        self.delete_snapshot(snapshot)

    def get_lun_id(self, volume_name):
        data = self._client.get_lun_by_name(volume_name)
        return data['lun_id']

    def get_lun_map(self, storage_group):
        data = self._client.get_storage_group(storage_group)
        return data['lunmap']

    def get_storage_group_uid(self, name):
        data = self._client.get_storage_group(name)
        return data['storage_group_uid']

    def assure_storage_group(self, storage_group):
        try:
            self._client.create_storage_group(storage_group)
        except EMCVnxCLICmdError as ex:
            if ex.out.find("Storage Group name already in use") == -1:
                raise ex

    def assure_host_in_storage_group(self, hostname, storage_group):
        try:
            self._client.connect_host_to_storage_group(hostname, storage_group)
        except EMCVnxCLICmdError as ex:
            if ex.rc == 83:
                # SG was not created or was destroyed by another concurrent
                # operation before connected.
                # Create SG and try to connect again
                LOG.warn(_('Storage Group %s is not found.'), storage_group)
                self.assure_storage_group(storage_group)
                self._client.connect_host_to_storage_group(
                    hostname, storage_group)
            else:
                raise ex
        return hostname

    def find_device_details(self, volume, storage_group):
        """Returns the Host Device number for the volume."""

        host_lun_id = -1

        data = self._client.get_storage_group(storage_group)
        lun_map = data['lunmap']
        data = self._client.get_lun_by_name(volume['name'])
        allocated_lun_id = data['lun_id']
        owner_sp = data['owner']

        for lun in lun_map.iterkeys():
            if lun == int(allocated_lun_id):
                host_lun_id = lun_map[lun]
                LOG.debug(_('Host Lun Id : %s') % (host_lun_id))
                break

        LOG.debug(_('Owner SP : %s') % (owner_sp))

        device = {
            'hostlunid': host_lun_id,
            'ownersp': owner_sp,
            'lunmap': lun_map,
        }
        return device

    def filter_available_hlu_set(self, used_hlus):
        used_hlu_set = set(used_hlus)
        return self.hlu_set - used_hlu_set

    def _extract_iscsi_uids(self, connector):
        if 'initiator' not in connector:
            if self.protocol == 'iSCSI':
                msg = _('Host %s has no iSCSI initiator') % connector['host']
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)
            else:
                return ()
        return [connector['initiator']]

    def _extract_fc_uids(self, connector):
        if 'wwnns' not in connector or 'wwpns' not in connector:
            if self.protocol == 'FC':
                msg = _('Host %s has no FC initiators') % connector['host']
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)
            else:
                return ()
        wwnns = connector['wwnns']
        wwpns = connector['wwpns']
        wwns = [(node + port).upper() for node, port in zip(wwnns, wwpns)]
        return map(lambda wwn: re.sub(r'\S\S',
                                      lambda m: m.group(0) + ':',
                                      wwn,
                                      len(wwn) / 2 - 1),
                   wwns)

    def _exec_command_setpath(self, initiator_uid, sp, port_id,
                              ip, host, vport_id=None):
        gname = host
        if vport_id is not None:
            cmd_iscsi_setpath = ('storagegroup', '-gname', gname, '-setpath',
                                 '-hbauid', initiator_uid, '-sp', sp,
                                 '-spport', port_id, '-spvport', vport_id,
                                 '-ip', ip, '-host', host, '-o')
            out, rc = self._client.command_execute(*cmd_iscsi_setpath)
            if rc != 0:
                raise EMCVnxCLICmdError(cmd_iscsi_setpath, rc, out)
        else:
            cmd_fc_setpath = ('storagegroup', '-gname', gname, '-setpath',
                              '-hbauid', initiator_uid, '-sp', sp,
                              '-spport', port_id,
                              '-ip', ip, '-host', host, '-o')
            out, rc = self._client.command_execute(*cmd_fc_setpath)
            if rc != 0:
                raise EMCVnxCLICmdError(cmd_fc_setpath, rc, out)

    def _register_iscsi_initiator(self, ip, host, initiator_uids):
        for initiator_uid in initiator_uids:
            iscsi_targets = self._client.get_iscsi_targets()
            LOG.info(_('Get ISCSI targets %(tg)s to register initiator %(in)s')
                     % ({'tg': iscsi_targets,
                         'in': initiator_uid}))

            target_portals_SPA = list(iscsi_targets['A'])
            target_portals_SPB = list(iscsi_targets['B'])

            for pa in target_portals_SPA:
                sp = 'A'
                port_id = pa['Port ID']
                vport_id = pa['Virtual Port ID']
                self._exec_command_setpath(initiator_uid, sp, port_id,
                                           ip, host, vport_id)

            for pb in target_portals_SPB:
                sp = 'B'
                port_id = pb['Port ID']
                vport_id = pb['Virtual Port ID']
                self._exec_command_setpath(initiator_uid, sp, port_id,
                                           ip, host, vport_id)

    def _register_fc_initiator(self, ip, host, initiator_uids):
        for initiator_uid in initiator_uids:
            fc_targets = self._client.get_fc_targets()
            LOG.info(_('Get FC targets %(tg)s to register initiator %(in)s')
                     % ({'tg': fc_targets,
                         'in': initiator_uid}))

            target_portals_SPA = list(fc_targets['A'])
            target_portals_SPB = list(fc_targets['B'])

            for pa in target_portals_SPA:
                sp = 'A'
                port_id = pa['Port ID']
                self._exec_command_setpath(initiator_uid, sp, port_id,
                                           ip, host)

            for pb in target_portals_SPB:
                sp = 'B'
                port_id = pb['Port ID']
                self._exec_command_setpath(initiator_uid, sp, port_id,
                                           ip, host)

    def _filter_unregistered_initiators(self, initiator_uids=[]):
        unregistered_initiators = []
        if not initiator_uids:
            return unregistered_initiators

        command_get_storage_group = ('storagegroup', '-list')
        out, rc = self._client.command_execute(*command_get_storage_group)

        if rc != 0:
            raise EMCVnxCLICmdError(command_get_storage_group, rc, out)

        for initiator_uid in initiator_uids:
            m = re.search(initiator_uid, out)
            if m is None:
                unregistered_initiators.append(initiator_uid)
        return unregistered_initiators

    def auto_register_initiator(self, connector):
        """Automatically register available initiators."""
        initiator_uids = []
        ip = connector['ip']
        host = connector['host']
        if self.protocol == 'iSCSI':
            initiator_uids = self._extract_iscsi_uids(connector)
            itors_toReg = self._filter_unregistered_initiators(initiator_uids)
            LOG.info(_('iSCSI Initiators %(in)s of %(ins)s need registration')
                     % ({'in': itors_toReg,
                         'ins': initiator_uids}))
            if not itors_toReg:
                LOG.info(_('Initiators %s are already registered')
                         % initiator_uids)
                return
            self._register_iscsi_initiator(ip, host, itors_toReg)

        elif self.protocol == 'FC':
            initiator_uids = self._extract_fc_uids(connector)
            itors_toReg = self._filter_unregistered_initiators(initiator_uids)
            LOG.info(_('FC Initiators %(in)s of %(ins)s need registration')
                     % ({'in': itors_toReg,
                         'ins': initiator_uids}))
            if not itors_toReg:
                LOG.info(_('Initiators %s are already registered')
                         % initiator_uids)
                return
            self._register_fc_initiator(ip, host, itors_toReg)

    def assure_host_access(self, volumename, connector):
        hostname = connector['host']
        auto_registration_done = False
        try:
            self.get_storage_group_uid(hostname)
        except EMCVnxCLICmdError as ex:
            if ex.rc != 83:
                raise ex
            # Storage Group has not existed yet
            self.assure_storage_group(hostname)
            if self.itor_auto_reg:
                self.auto_register_initiator(connector)
                auto_registration_done = True
            else:
                self._client.connect_host_to_storage_group(hostname, hostname)

        if self.itor_auto_reg and not auto_registration_done:
            self.auto_register_initiator(connector)
            auto_registration_done = True

        lun_id = self.get_lun_id(volumename)
        lun_map = self.get_lun_map(hostname)
        if lun_id in lun_map:
            return lun_map[lun_id]
        used_hlus = lun_map.values()
        if len(used_hlus) >= self.max_luns_per_sg:
            msg = _('Reach limitation set by configuration '
                    'option max_luns_per_storage_group. '
                    'Operation to add %(vol)s into '
                    'Storage Group %(sg)s is rejected') % \
                {'vol': volumename,
                 'sg': hostname}
            LOG.error(msg)
            raise exception.CinderException(msg)

        candidate_hlus = self.filter_available_hlu_set(used_hlus)
        for i, hlu in enumerate(candidate_hlus):
            if i >= self.max_retries:
                break
            try:
                self._client.add_hlu_to_storage_group(
                    hlu,
                    lun_id,
                    hostname)
                return hlu
            except EMCVnxCLICmdError as ex:
                # Retry
                continue

        msg = _("Failed to add %(vol)s into %(sg)s "
                "after %(retries)s tries") % \
            {'vol': volumename,
             'sg': hostname,
             'retries': min(self.max_retries, len(candidate_hlus))}
        LOG.error(msg)
        raise exception.VolumeBackendAPIException(data=msg)

    def _get_iscsi_properties(self, volume, connector):
        storage_group = connector['host']
        device_info = self.find_device_details(volume, storage_group)
        owner_sp = device_info['ownersp']
        registered_spports = self._client.get_registered_spport_set(
            connector['initiator'],
            storage_group)
        target = self._client.find_avaialable_iscsi_target_one(
            storage_group, owner_sp,
            registered_spports)
        properties = {'target_discovered': True,
                      'target_iqn': 'unknown',
                      'target_portal': 'unknown',
                      'target_lun': 'unknown'}
        if target:
            properties = {'target_discovered': True,
                          'target_iqn': target['Port WWN'],
                          'target_portal': "%s:3260" % target['IP Address'],
                          'target_lun': device_info['hostlunid']}
            LOG.debug(_("iSCSI Properties: %s"), properties)
            auth = volume['provider_auth']
            if auth:
                (auth_method, auth_username, auth_secret) = auth.split()
                properties['auth_method'] = auth_method
                properties['auth_username'] = auth_username
                properties['auth_password'] = auth_secret
        else:
            LOG.error(_('Failed to find an available iSCSI targets for %s'),
                      storage_group)

        return properties

    def initialize_connection(self, volume, connector):
        """Initializes the connection and returns connection info."""
        @lockutils.synchronized('emc-connection-' + connector['host'],
                                "emc-connection-", True)
        def do_initialize_connection():
            device_number = self.assure_host_access(
                volume['name'], connector)
            return device_number
        if self.protocol == 'iSCSI':
            do_initialize_connection()
            iscsi_properties = self._get_iscsi_properties(volume, connector)
            return {'driver_volume_type': 'iscsi',
                    'data': iscsi_properties}
        elif self.protocol == 'FC':
            return do_initialize_connection()

    def terminate_connection(self, volume, connector):
        """Disallow connection from connector."""
        @lockutils.synchronized('emc-connection-' + connector['host'],
                                "emc-connection-", True)
        def do_terminate_connection():
            hostname = connector['host']
            volume_name = volume['name']
            try:
#                 storage_group_uid = self.get_storage_group_uid(hostname)
                lun_map = self.get_lun_map(hostname)
            except EMCVnxCLICmdError as ex:
                if ex.rc == 83:
                    LOG.warn(_("Storage Group %s is not found. "
                             "terminate_connection() is unnecessary. "),
                             hostname)
                    return
            try:
                lun_id = self.get_lun_id(volume_name)
            except EMCVnxCLICmdError as ex:
                if ex.rc == 9:
                    LOG.warn(_("Volume %s has probably been removed in VNX"),
                             volume_name)
                    return

            if lun_id in lun_map:
                self._client.remove_hlu_from_storagegroup(
                    lun_map[lun_id], hostname)
            else:
                LOG.warn(_("Volume %(vol)s was not in Storatge Group %(sg)s")
                         % {'vol': volume_name, 'sg': hostname})
            if self.destroy_empty_sg:
                try:
                    lun_map = self.get_lun_map(hostname)
                    if not lun_map:
                        LOG.info(_("Storage Group %s was empty"), hostname)
                        self._client.disconnect_host_from_storage_group(
                            hostname, hostname)
                        self._client.delete_storage_group(hostname)
                except Exception:
                    LOG.warn(_("Failed to destroy Storage Group %s"),
                             hostname)
        return do_terminate_connection()

    def find_iscsi_protocol_endpoints(self, device_sp):
        """Returns the iSCSI initiators for a SP."""
        return self._client.get_iscsi_protocol_endpoints(device_sp)

    def get_target_wwns(self, connector):
        return self._client.get_target_wwns(connector['host'])

    def get_volumetype_extraspecs(self, volume):
        specs = {}

        type_id = volume['volume_type_id']
        if type_id is not None:
            specs = volume_types.get_volume_type_extra_specs(type_id)

        return specs

    def _get_provisioning_by_volume(self, volume):
        # By default, the user can not create thin LUN without thin
        # provisioning enabler.
        thinness = 'NonThin'
        spec_id = 'storagetype:provisioning'

        specs = self.get_volumetype_extraspecs(volume)
        if specs and spec_id in specs:
            provisioning = specs[spec_id].lower()
            if 'thin' == provisioning:
                thinness = 'Thin'
            elif 'thick' != provisioning:
                msg = _('Invalid value of extra spec '
                        '\'storagetype:provisioning\': %(provisioning)s') %\
                    {'provisioning': specs[spec_id]}
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)
        else:
            LOG.info(_('No extra spec \'storagetype:provisioning\' exist'))

        return thinness


class EMCVnxCliPool(EMCVnxCliBase):

    def __init__(self, prtcl, configuration):
        super(EMCVnxCliPool, self).__init__(prtcl, configuration=configuration)
        self.storage_pool = configuration.storage_vnx_pool_name.strip()
        self._client.get_pool(self.storage_pool)

    def get_target_storagepool(self,
                               volume=None,
                               source_volume_name=None):
        pool_spec_id = "storagetype:pool"
        if volume is not None:
            specs = self.get_volumetype_extraspecs(volume)
            if specs and pool_spec_id in specs:
                expect_pool = specs[pool_spec_id].strip()
                if expect_pool != self.storage_pool:
                    msg = _("Storage pool %s is not supported"
                            " by this Cinder Volume") % expect_pool
                    LOG.error(msg)
                    raise exception.VolumeBackendAPIException(data=msg)
        return self.storage_pool

    def update_volume_status(self):
        """Retrieve status info."""
        LOG.debug(_("Updating volume status"))
        self.stats = super(EMCVnxCliPool, self).update_volume_status()
        data = self._client.get_pool(self.get_target_storagepool())
        self.stats['total_capacity_gb'] = data['total_capacity_gb']
        self.stats['free_capacity_gb'] = data['free_capacity_gb']

        array_serial = self._client.get_array_serial()
        self.stats['location_info'] = ('%(pool_name)s|%(array_serial)s' %
                                       {'pool_name': self.storage_pool,
                                        'array_serial':
                                           array_serial['array_serial']})
        return self.stats


class EMCVnxCliArray(EMCVnxCliBase):

    def __init__(self, prtcl, configuration):
        super(EMCVnxCliArray, self).__init__(prtcl,
                                             configuration=configuration)
        self._update_pool_cache()

    def _update_pool_cache(self):
        LOG.debug(_("Updating Pool Cache"))
        self.pool_cache = self._client.get_pool_list()

    def get_target_storagepool(self, volume, source_volume_name=None):
        """Find the storage pool for given volume."""
        pool_spec_id = "storagetype:pool"
        specs = self.get_volumetype_extraspecs(volume)
        if specs and pool_spec_id in specs:
            return specs[pool_spec_id]
        elif source_volume_name:
            data = self._client.get_lun_by_name(source_volume_name,
                                                [self._client.LUN_POOL])
            if data is None:
                msg = _("Failed to find storage pool for source volume %s") \
                    % source_volume_name
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)
            return data[self._client.LUN_POOL.key]
        else:
            if len(self.pool_cache) > 0:
                pools = sorted(self.pool_cache,
                               key=lambda po: po['free_space'],
                               reverse=True)
                return pools[0]['name']
        raise exception.VolumeBackendAPIException(
            data="No storage pool found!")

    def update_volume_status(self):
        """Retrieve status info."""
        self.stats = super(EMCVnxCliArray, self).update_volume_status()
        self._update_pool_cache()
        self.stats['total_capacity_gb'] = 'unknown'
        self.stats['free_capacity_gb'] = 'unknown'
        array_serial = self._client.get_array_serial()
        self.stats['location_info'] = ('%(pool_name)s|%(array_serial)s' %
                                       {'pool_name': '',
                                        'array_serial':
                                        array_serial['array_serial']})
        return self.stats


def getEMCVnxCli(prtcl, configuration=None):
    configuration.append_config_values(loc_opts)
    pool_name = configuration.safe_get("storage_vnx_pool_name")

    if pool_name is None or len(pool_name.strip()) == 0:
        return EMCVnxCliArray(prtcl, configuration=configuration)
    else:
        return EMCVnxCliPool(prtcl, configuration=configuration)
