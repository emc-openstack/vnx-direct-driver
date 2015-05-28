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
VNX CLI on iSCSI
"""
import os
import random
import re
import time

import eventlet
try:
    import json
except ImportError:
    import simplejson as json

from oslo.config import cfg

from cinder import exception
from cinder.openstack.common import excutils
from cinder.openstack.common import lockutils
from cinder.openstack.common import log as logging
from cinder.openstack.common import loopingcall
from cinder.openstack.common import processutils
from cinder.openstack.common import timeutils
from cinder import utils
from cinder.volume.drivers.emc import queueworker
from cinder.volume.drivers.san import san
from cinder.volume import volume_types


LOG = logging.getLogger(__name__)

CONF = cfg.CONF
VERSION = '03.00.05'

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
               default=60 * 24 * 365,
               help='Default Time Out For CLI operations'),
    cfg.IntOpt('max_luns_per_storage_group',
               default=255,
               help='Default max number of LUNs in a storage group'),
    cfg.BoolOpt('destroy_empty_storage_group',
                default=False,
                help='To destroy storage group '
                'when the last LUN is removed from it'),
    cfg.StrOpt('iscsi_initiators',
               default='',
               help='Mapping between hostname and '
               'its iSCSI initiator IP addresses'),
    cfg.StrOpt('io_port_list',
               default='*',
               help='Comma separated iSCSI or FC ports '
               'to be used in Nova or Cinder'),
    cfg.BoolOpt('initiator_auto_registration',
                default=False,
                help='Automatically register initiators'),
    cfg.IntOpt('attach_detach_batch_interval',
               default=-1,
               help='Interval by seconds between the '
               'attach detach work. Set it to -1 will disable'
               'the batch feature'),
    cfg.BoolOpt('use_multi_iscsi_portals',
                default=False,
                help="Return multiple iSCSI target portals"),
    cfg.BoolOpt('force_delete_lun_in_storagegroup',
                default=False,
                help='Delete a LUN even if it is in Storage Groups'),
]

CONF.register_opts(loc_opts)


def log_enter_exit(func):
    if not CONF.debug:
        return func

    def inner(self, *args, **kwargs):
        LOG.debug("Entering %(cls)s.%(method)s" %
                  {'cls': self.__class__.__name__,
                   'method': func.__name__})
        start = timeutils.utcnow()
        ret = func(self, *args, **kwargs)
        end = timeutils.utcnow()
        LOG.debug("Exiting %(cls)s.%(method)s. "
                  "Spent %(duration)s sec. "
                  "Return %(return)s" %
                  {'cls': self.__class__.__name__,
                   'duration': timeutils.delta_seconds(start, end),
                   'method': func.__name__,
                   'return': ret})
        return ret
    return inner


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


class PropertyDescriptor(object):
    def __init__(self, option, label, key, converter=None):
        self.option = option
        self.label = label
        self.key = key
        self.converter = converter


class AddHluStatus(queueworker.Status):
    NO_HLU_LEFT = -2


class RemoveHluStatus(queueworker.Status):
    HLU_NOT_IN_SG = -2


class BatchOrderType(object):
    ADD = 0
    REMOVE = 1


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
        self.active_storage_ip = configuration.san_ip
        self.primary_storage_ip = self.active_storage_ip
        self.secondary_storage_ip = configuration.san_secondary_ip
        if not configuration.san_ip:
            errormessage += (_('Mandatory field configuration.san_ip \
                is not set.'))
        if self.secondary_storage_ip == self.primary_storage_ip:
            LOG.warn(_("san_secondary_ip is configured as "
                       "the same value as san_ip."))
            self.secondary_storage_ip = None
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
        command_create_lun = ['lun', '-create',
                              '-type', thinness,
                              '-capacity', size,
                              '-sq', 'gb',
                              '-poolName', poolname,
                              '-name', name]

        # executing cli command to create volume
        out, rc = self.command_execute(*command_create_lun, poll=False)
        if rc != 0:
            #Ignore the error that due to retry
            if rc == 4 and out.find('(0x712d8d04)') >= 0:
                LOG.warn(_('LUN already exists, LUN name %(name)s. '
                           'Message: %(msg)s') %
                         {'name': name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_create_lun, rc, out)

    def delete_lun(self, name):
        command_delete_lun = ['lun', '-destroy',
                              '-name', name,
                              '-forceDetach',
                              '-o']
        # executing cli command to delete volume
        out, rc = self.command_execute(*command_delete_lun)
        if rc != 0 or out.strip():
            #Ignore the error that due to retry
            if rc == 9 and out.find("not exist") >= 0:
                LOG.warn(_("LUN already deleted, LUN name %(name)s. "
                           "Message: %(msg)s") %
                         {'name': name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_delete_lun, rc, out)

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

        self.create_lun(poolname, name, size, thinness)

        def lun_is_ready():
            try:
                data = self.get_lun_by_name(name, poll=False)
                return data[self.LUN_STATE.key] == 'Ready'
            except EMCVnxCLICmdError as ex:
                if ex.out.find('The (pool lun) may not exist') >= 0:
                    return False
                else:
                    raise ex

        timer = loopingcall.FixedIntervalLoopingCall(
            self._wait_for_a_condition,
            lun_is_ready, int(time.time()))
        timer.start(interval=INTERVAL_5_SEC).wait()

    def expand_lun(self, name, new_size):
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

    def expand_lun_and_wait(self, name, new_size):

        self.expand_lun(name, new_size)

        def lun_is_extented():
            data = self.get_lun_by_name(name)
            return new_size == data[self.LUN_CAPACITY.key]

        timer = loopingcall.FixedIntervalLoopingCall(
            self._wait_for_a_condition,
            lun_is_extented, int(time.time()))
        timer.start(interval=INTERVAL_5_SEC).wait()

    def create_snapshot(self, volume_name, name):

        data = self.get_lun_by_name(volume_name)
        if data[self.LUN_ID.key] is not None:
            command_create_snapshot = ('snap', '-create',
                                       '-res', data[self.LUN_ID.key],
                                       '-name', name,
                                       '-allowReadWrite', 'yes',
                                       '-allowAutoDelete', 'no')

            out, rc = self.command_execute(*command_create_snapshot,
                                           poll=False)
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

    def delete_snapshot(self, name):

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

    def create_mount_point(self, primary_lun_name, name):
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

        return rc

    def attach_mount_point(self, name, snapshot_name):
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

        return rc

    def check_smp_not_attached(self, smp_name):
        """Ensure a snap mount point with snap become a LUN."""

        def _wait_for_sync_status():
            lun_list = ('lun', '-list', '-name', smp_name,
                        '-attachedSnapshot')
            out, rc = self.command_execute(*lun_list, poll=True)
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

    def migrate_lun(self, src_id, dst_id):
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
        return rc

    def migrate_lun_with_verification(self, src_id,
                                      dst_id=None,
                                      dst_name=None):
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
                out, rc = self.command_execute(*migrate_lun_with_verification,
                                               poll=True)
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
            out, rc = self.command_execute(*migrate_lun_with_verification,
                                           poll=True)
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

        return 0

    def get_storage_group(self, name, poll=True):
        # ALU/HLU as key/value map
        lun_map = {}

        data = {'storage_group_name': name,
                'storage_group_uid': None,
                'lunmap': lun_map,
                'raw_output': ''}

        command_get_storage_group = ('storagegroup', '-list',
                                     '-gname', name)
        out, rc = self.command_execute(*command_get_storage_group,
                                       poll=poll)
        if rc != 0:
            raise EMCVnxCLICmdError(command_get_storage_group, rc, out)

        data['raw_output'] = out
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

        return data

    def get_hlus(self, lun_id, poll=True):
        hlus = list()
        command_storage_group_list = ('storagegroup', '-list')
        out, rc = self.command_execute(*command_storage_group_list,
                                       poll=poll)
        if rc != 0:
            raise EMCVnxCLICmdError(command_storage_group_list, rc, out)
        sg_name_p = re.compile(r'^\s*(?P<sg_name>[^\n\r]+)')
        hlu_alu_p = re.compile(r'HLU/ALU Pairs:'
                               r'\s*HLU Number\s*ALU Number'
                               r'\s*[-\s]*'
                               r'(\d|\s)*'
                               r'\s+(?P<hlu>\d+)( |\t)+%s' % lun_id)
        for sg_info in out.split('Storage Group Name:'):
            hlu_alu_m = hlu_alu_p.search(sg_info)
            if hlu_alu_m is None:
                continue
            sg_name_m = sg_name_p.search(sg_info)
            if sg_name_m:
                hlus.append((hlu_alu_m.group('hlu'),
                             sg_name_m.group('sg_name')))
        return hlus

    def create_storage_group(self, name):

        command_create_storage_group = ('storagegroup', '-create',
                                        '-gname', name)

        out, rc = self.command_execute(*command_create_storage_group)
        if rc != 0:
            # Ignore the error that due to retry
            if rc == 66 and out.find("name already in use") >= 0:
                LOG.warn(_('Storage group %(name)s already exsited. '
                           'Message: %(msg)s') %
                         {'name': name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_create_storage_group, rc, out)

    def delete_storage_group(self, name):

        command_delete_storage_group = ('storagegroup', '-destroy',
                                        '-gname', name, '-o')

        out, rc = self.command_execute(*command_delete_storage_group)
        if rc != 0:
            # Ignore the error that due to retry
            if rc == 83 and out.find("group name or UID does not"
                                     " match any storage groups") >= 0:
                LOG.warn(_("Storage group %(name)s has already been deleted."
                           " Message: %(msg)s") %
                         {'name': name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_delete_storage_group, rc, out)

    def connect_host_to_storage_group(self, hostname, sg_name):

        command_host_connect = ('storagegroup', '-connecthost',
                                '-host', hostname,
                                '-gname', sg_name,
                                '-o')

        out, rc = self.command_execute(*command_host_connect)
        if rc != 0:
            raise EMCVnxCLICmdError(command_host_connect, rc, out)

    def disconnect_host_from_storage_group(self, hostname, sg_name):
        command_host_disconnect = ('storagegroup', '-disconnecthost',
                                   '-host', hostname,
                                   '-gname', sg_name,
                                   '-o')

        out, rc = self.command_execute(*command_host_disconnect)
        if rc != 0:
            # Ignore the error that due to retry
            if rc == 116 and \
                re.search("host is not.*connected to.*storage group",
                          out) is not None:
                LOG.warn(_("Host %(host)s has already disconnected from "
                           "storage group %(sgname)s. Message: %(msg)s") %
                         {'host': hostname, 'sgname': sg_name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_host_disconnect, rc, out)

    def add_hlu_to_storage_group(self, hlu, alu, sg_name):
        """Add a lun into storage group as specified hlu number
        Return True if the hlu is as specified, otherwise False
        """

        command_add_hlu = ('storagegroup', '-addhlu',
                           '-hlu', hlu,
                           '-alu', alu,
                           '-gname', sg_name)

        out, rc = self.command_execute(*command_add_hlu, poll=False)
        if rc != 0:
            # Do not need to consider the retry for add hlu
            # Retry is handled in the caller
            raise EMCVnxCLICmdError(command_add_hlu, rc, out)

        return True

    def add_hlus_to_storage_group(self, hlulist, alulist, sg_name):
        """Add a lun into storage group as specified hlu number
        Return True if the hlu is as specified, otherwise False
        """
        hlusstr = ",".join(map(lambda a: str(a), hlulist))
        alusstr = ",".join(map(lambda a: str(a), alulist))
        command_add_hlu = ('storagegroup', '-addhlu',
                           '-gname', sg_name,
                           '-hlus', hlusstr, '-verboseStatus',
                           '-alus', alusstr, '-verboseStatus',
                           '-o')

        out, rc = self.command_execute(*command_add_hlu,
                                       poll=False,
                                       retry_disable=True)
        added_alus_pattern = 'LUs Added:\s*([0-9,]*).'
        searchout = re.search(added_alus_pattern, out)
        if searchout is not None:
            added_alus_str = searchout.group(1).strip()
            if len(added_alus_str) != 0:
                added_alus = map(lambda item: int(item),
                                 added_alus_str.split(","))
            else:
                added_alus = []
        else:
            added_alus = []

        added_alus = set(added_alus)
        not_added_alus = set(alulist) - added_alus
        return added_alus, not_added_alus

    def remove_hlu_from_storagegroup(self, hlu, sg_name, poll=False):

        command_remove_hlu = ('storagegroup', '-removehlu',
                              '-hlu', hlu,
                              '-gname', sg_name,
                              '-o')
        out, rc = self.command_execute(*command_remove_hlu, poll=poll)
        if rc != 0:
            # Ignore the error that due to retry
            if rc == 66 and\
                    out.find("No such Host LUN in this Storage Group") >= 0:
                LOG.warn(_("HLU %(hlu)s has already removed from %(sgname)s. "
                           "Message: %(msg)s") %
                         {'hlu': hlu, 'sgname': sg_name, 'msg': out})
            else:
                raise EMCVnxCLICmdError(command_remove_hlu, rc, out)

    def remove_hlus_from_storagegroup(self, hlus, sg_name):
        hlusstr = ",".join(map(lambda a: str(a), hlus))
        command_remove_hlu = ('storagegroup', '-removehlu',
                              '-gname', sg_name,
                              '-hlus', hlusstr, '-verboseStatus',
                              '-o')

        out, rc = self.command_execute(*command_remove_hlu,
                                       poll=False,
                                       retry_disable=True)
        removed_alus_pattern = 'LUs Removed:\s*([0-9,]*).'
        searchout = re.search(removed_alus_pattern, out)
        if searchout is not None:
            removed_alus_str = searchout.group(1).strip()
            if len(removed_alus_str) != 0:
                removed_hlus = map(lambda item: int(item),
                                   removed_alus_str.split(","))
            else:
                removed_hlus = []
        else:
            removed_hlus = []

        removed_hlus = set(removed_hlus)
        not_removed_hlus = set(hlus) - removed_hlus

        return removed_hlus, not_removed_hlus

    def get_iscsi_protocol_endpoints(self, device_sp):

        command_get_port = ('connection', '-getport',
                            '-sp', device_sp)

        out, rc = self.command_execute(*command_get_port)
        if rc != 0:
            raise EMCVnxCLICmdError(command_get_port, rc, out)

        re_port_wwn = 'Port WWN:\s*(.*)\s*'
        initiator_address = re.findall(re_port_wwn, out)

        return initiator_address

    def get_lun_by_name(self, name, properties=LUN_ALL, poll=True):

        data = self.get_lun_properties(('-name', name),
                                       properties,
                                       poll=poll)
        return data

    def get_lun_by_id(self, lunid, properties=LUN_ALL, poll=True):
        data = self.get_lun_properties(('-l', lunid),
                                       properties,
                                       poll=poll)
        return data

    def get_pool(self, name, poll=True):
        data = self.get_pool_properties(('-name', name),
                                        poll=poll)
        return data

    def get_pool_properties(self, filter_option, properties=POOL_ALL,
                            poll=True):
        module_list = ('storagepool', '-list')
        return self.get_lun_or_pool_properties(
            module_list, filter_option,
            base_properties=[self.POOL_NAME],
            adv_properties=properties,
            poll=poll)

    def get_lun_properties(self, filter_option, properties=LUN_ALL,
                           poll=True):
        module_list = ('lun', '-list')
        return self.get_lun_or_pool_properties(
            module_list, filter_option,
            base_properties=[self.LUN_NAME, self.LUN_ID],
            adv_properties=properties,
            poll=poll)

    def get_lun_or_pool_properties(self, module_list,
                                   filter_option,
                                   base_properties=[],
                                   adv_properties=[],
                                   poll=True):
        # to do instance check
        command_get_lun = module_list + filter_option
        for prop in adv_properties:
            command_get_lun += (prop.option, )
        out, rc = self.command_execute(*command_get_lun, poll=poll)

        if rc != 0:
            raise EMCVnxCLICmdError(command_get_lun, rc, out)

        data = {}
        for baseprop in base_properties:
            data[baseprop.key] = self._get_property_value(out, baseprop)

        for prop in adv_properties:
            data[prop.key] = self._get_property_value(out, prop)

        LOG.debug('Return LUN or Pool properties. Data: %s' % data)
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
    def get_pool_list(self, poll=True):
        temp_cache = []
        cmd = ('storagepool', '-list', '-availableCap', '-state')
        out, rc = self.command_execute(*cmd, poll=poll)
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

    def get_array_serial(self, poll=False):
        """return array Serial No for pool backend."""
        stats = {'array_serial': 'unknown'}

        command_get_array_serial = ('getagent', '-serial')
        # Set the property timeout to get array serial
        out, rc = self.command_execute(*command_get_array_serial,
                                       poll=poll)
        if 0 == rc:
            m = re.search(r'Serial No:\s+(\w+)', out)
            if m:
                stats['array_serial'] = m.group(1)
            else:
                LOG.warn("No array serial returned, set as unknown")
        else:
            raise EMCVnxCLICmdError(command_get_array_serial, rc, out)

        return stats

    def get_target_wwns(self, storage_group_name, poll=True):
        """Function to get target port wwns."""
        cmd_get_hba = ('storagegroup', '-list', '-gname', storage_group_name)
        out, rc = self.command_execute(*cmd_get_hba, poll=poll)
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
        return wwns

    def get_white_list_wwns(self, while_list):
        """Gets wwns via specific SP ports."""
        wwns = []
        cmd_get_port = ('port', '-list', '-sp')
        out, rc = self.command_execute(*cmd_get_port)
        if 0 != rc:
            raise EMCVnxCLICmdError(cmd_get_port, rc, out)
        for port in while_list:
            wwn = self.get_port_wwn('SP ' + port[0], port[1],
                                    out)
            if wwn is not None and wwn not in wwns:
                wwns.append(wwn)
        return wwns

    def get_port_wwn(self, sp, port_id, allports=None):
        wwn = None
        if allports is None:
            cmd_get_port = ('port', '-list', '-sp')
            out, rc = self.command_execute(*cmd_get_port)
            if 0 != rc:
                raise EMCVnxCLICmdError(cmd_get_port, rc, out)
            else:
                allports = out
        _re_port_wwn = re.compile('SP Name:\s*' + sp +
                                  '\nSP Port ID:\s*' + str(port_id) +
                                  '\nSP UID:\s*((\w\w:){15}(\w\w))' +
                                  '\nLink Status:         Up' +
                                  '\nPort Status:         Online')
        _obj_search = re.search(_re_port_wwn, allports)
        if _obj_search is not None:
            wwn = _obj_search.group(1).replace(':', '')[16:]
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

    def _filter_iscsi_ports(self, all_ports, io_ports):
        """Filter white list from all iSCSI ports."""
        new_iscsi_ports = {'A': [], 'B': []}
        valid_ports = []
        for sp in all_ports:
            for port in all_ports[sp]:
                port_tuple = (port['SP'],
                              port['Port ID'],
                              port['Virtual Port ID'])
                if port_tuple in io_ports:
                    new_iscsi_ports[sp].append(port)
                    valid_ports.append(port_tuple)
        if len(io_ports) != len(valid_ports):
            invalid_port_set = set(io_ports) - set(valid_ports)
            for invalid in invalid_port_set:
                LOG.warn(_('Invalid iSCSI port %(sp)s-%(port)s-%(vlan)s found'
                           ' in io_port_list, will be ignored.'),
                         {'sp': invalid[0], 'port': invalid[1],
                          'vlan': invalid[2]})
        return new_iscsi_ports

    def get_iscsi_targets(self, poll=False, io_ports=None):
        cmd_getport = ('connection', '-getport', '-address', '-vlanid')
        out, rc = self.command_execute(*cmd_getport, poll=poll)
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
            if io_ports:
                return self._filter_iscsi_ports(iscsi_target_dict, io_ports)
            return iscsi_target_dict

    def get_registered_spport_set(self, initiator_iqn, sgname, sg_raw_out):
        spport_set = set()
        for m_spport in re.finditer(r'\n\s+%s\s+SP\s(A|B)\s+(\d+)' %
                                    initiator_iqn,
                                    sg_raw_out,
                                    flags=re.IGNORECASE):
            spport_set.add((m_spport.group(1), int(m_spport.group(2))))
            LOG.debug(_('See path %(path)s in %(sg)s')
                      % ({'path': m_spport.group(0),
                          'sg': sgname}))
        return spport_set

    def ping_node(self, target_portal, initiator_ip):
        connection_pingnode = ('connection', '-pingnode', '-sp',
                               target_portal['SP'], '-portid',
                               target_portal['Port ID'], '-vportid',
                               target_portal['Virtual Port ID'],
                               '-address', initiator_ip,
                               '-count', '1')
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
                                         registered_spport_set,
                                         all_iscsi_targets):
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
            target_portals = list(all_iscsi_targets[target_sp])
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
                    LOG.debug("No iSCSI IP address of %(hostname)s is known. "
                              "Return a random iSCSI target portal %(portal)s."
                              %
                              {'hostname': hostname, 'portal': target_portal})
                    return target_portal

    def _is_sp_unavailable_error(self, out):
        error_pattern = '(^Error.*Message.*End of data stream.*)|'\
                        '(.*Message.*connection refused.*)|'\
                        '(^Error.*Message.*Service Unavailable.*)'
        pattern = re.compile(error_pattern)
        return pattern.match(out)

    def command_execute(self, *command, **kwargv):
        """Execute command on the VNX array, when there is
        named parameter poll=False, the command will be sent
        alone with np option
        """
        # NOTE: retry_disable need to be removed from kwargv
        # before it pass to utils.execute, otherwise exception will thrown
        retry_disable = kwargv.pop('retry_disable', False)
        # TODO(Tina): Do not do the SP alive check every time
        if self._is_sp_alive(self.active_storage_ip):
            out, rc = self._command_execute_on_active_ip(*command, **kwargv)
            if not retry_disable and self._is_sp_unavailable_error(out):
                # When active sp is unavailble, swith to another sp
                # and set it to active and force a poll
                if self._toggle_sp():
                    LOG.debug('EMC: Command Exception: %(rc) %(result)s. '
                              'Retry on another SP.' % {'rc': rc,
                                                        'result': out})
                    kwargv['poll'] = True
                    out, rc = self._command_execute_on_active_ip(*command,
                                                                 **kwargv)
        elif self._toggle_sp():
            # If active ip is not accessible, toggled to another sp
            kwargv['poll'] = True
            out, rc = self._command_execute_on_active_ip(*command, **kwargv)
        else:
            # Active IP is inaccessible, and cannot toggle to another SP,
            # return Error
            out, rc = "Server Unavailable", 255
            LOG.debug('EMC: Command: %(command)s. Result: '
                      'Server Unavailable. Command is not executed '
                      'because SPs are inaccessible.' %
                      {'command': self.command + command})

        return out, rc

    def _command_execute_on_active_ip(self, *command, **kwargv):
        if "check_exit_code" not in kwargv:
            kwargv["check_exit_code"] = True
        rc = 0
        out = ""
        need_poll = kwargv.pop('poll', True)
        if "-np" not in command and not need_poll:
            command = ("-np",) + command

        try:
            active_ip = (self.active_storage_ip,)
            out, err = utils.execute(
                *(self.command
                  + active_ip
                  + self.credentials
                  + command),
                **kwargv)
        except processutils.ProcessExecutionError as pe:
            rc = pe.exit_code
            out = pe.stdout
            out = out.replace('\n', '\\n')

        LOG.debug('EMC: Command: %(command)s. Result: %(result)s.'
                  % {'command': self.command + active_ip + command,
                     'result': out.replace('\n', '\\n')})

        return out, rc

    def _is_sp_alive(self, ipaddr):
        ping_cmd = ('ping', '-c', 1, ipaddr)
        try:
            out, err = utils.execute(*ping_cmd,
                                     check_exit_code=True)
        except processutils.ProcessExecutionError as pe:
            out = pe.stdout
            rc = pe.exit_code
            if rc != 0:
                LOG.debug('%s is unavaialbe' % ipaddr)
                return False
        LOG.debug('Ping SP %(spip)s Command Result: %(result)s.' %
                  {'spip': self.active_storage_ip, 'result': out})
        return True

    def _toggle_sp(self):
        """This function toggles the storage IP
        Address between primary IP and secondary IP, if no SP IP address has
        exchanged, return False, otherwise True will be returned.
        """
        if self.secondary_storage_ip is None:
            return False
        old_ip = self.active_storage_ip
        self.active_storage_ip = self.secondary_storage_ip if\
            self.active_storage_ip == self.primary_storage_ip else\
            self.primary_storage_ip

        LOG.info(_('Toggle storage_vnx_ip_adress from %(old)s to '
                   '%(new)s.') %
                 {'old': old_ip,
                  'new': self.active_storage_ip})
        return True


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
        self.multi_portals = self.configuration.use_multi_iscsi_portals
        self.max_retries = 5
        self.io_ports = self._parse_ports(self.configuration.io_port_list,
                                          self.protocol)
        self.attach_detach_batch_interval = \
            self.configuration.attach_detach_batch_interval
        if self.destroy_empty_sg:
            LOG.warn(_("destroy_empty_storage_group=True"))
        if not self.itor_auto_reg:
            LOG.warn(_("initiator auto registration not enabled"))
        self.hlu_set = set(xrange(1, self.max_luns_per_sg + 1))
        self._client = CommandLineHelper(self.configuration)
        self.array_serial = None
        if self.protocol == 'iSCSI':
            self.iscsi_targets = self._client.get_iscsi_targets(
                poll=True, io_ports=self.io_ports)

        if self.attach_detach_batch_interval < 0:
            self.hlu_cache = {}
            self.do_initialize_connection = self._do_initialize_connection
            self.do_terminate_connection = self._do_terminate_connection
        else:
            self.addhlu_workers = {}
            self.do_initialize_connection =\
                self._do_initialize_connection_in_batch
            self.do_terminate_connection =\
                self._do_terminate_connection_in_batch
        self.force_delete_lun_in_sg = \
            self.configuration.force_delete_lun_in_storagegroup
        if self.force_delete_lun_in_sg:
            LOG.warn(_("force_delete_lun_in_storagegroup=True"))

    def _parse_ports(self, io_port_list, protocol):
        """Validates IO port format, supported format is a-1, b-3, a-3-0."""
        if not io_port_list or io_port_list == '*':
            return None
        ports = re.split('\s*,\s*', io_port_list)
        all_ports = []
        for port in ports:
            if not port:
                continue
            if 'iSCSI' == protocol and re.match('[abAB]-\d+-\d+', port):
                port_tuple = port.split('-')
                all_ports.append(
                    (port_tuple[0].upper(), int(port_tuple[1]),
                     int(port_tuple[2])))
            elif "FC" == protocol and re.match('[abAB]-\d+', port):
                port_tuple = port.split('-')
                all_ports.append(
                    (port_tuple[0].upper(), int(port_tuple[1])))
            else:
                msg = _('Invalid IO port %(port)s specified '
                        'for %(protocol)s.') % {'port': port,
                                                'protocol': protocol}
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)

        return all_ports

    def get_target_storagepool(self, volume, source_volume_name=None):
        raise NotImplementedError

    def dumps_provider_location(self, pl_dict):
        return '|'.join([k + '^' + pl_dict[k] for k in pl_dict])

    def get_array_serial(self):
        if not self.array_serial:
            self.array_serial = self._client.get_array_serial()
        return self.array_serial['array_serial']

    @log_enter_exit
    def create_volume(self, volume):
        """Creates a EMC volume."""
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
        lun = self._client.get_lun_by_name(volumename, poll=False)
        pl_dict = {'system': self.get_array_serial(),
                   'type': 'lun',
                   'id': str(lun['lun_id'])}
        model_update = {'provider_location':
                        self.dumps_provider_location(pl_dict)}
        volume['provider_location'] = model_update['provider_location']
        return model_update

    @log_enter_exit
    def delete_volume(self, volume):
        """Deletes an EMC volume."""

        try:
            self._client.delete_lun(volume['name'])
        except EMCVnxCLICmdError as ex:
            if (self.force_delete_lun_in_sg and
                    ('contained in a Storage Group' in ex.out or
                     'Host LUN/LUN mapping still exists' in ex.out)):
                LOG.warn(_('LUN corresponding to %s is still '
                           'in some Storage Groups.'
                           'Try to bring the LUN out of Storage Groups '
                           'and retry the deletion.'),
                         volume['name'])
                lun_id = self.get_lun_id(volume)
                for hlu, sg in self._client.get_hlus(lun_id):
                    self._client.remove_hlu_from_storagegroup(hlu, sg)
                self._client.delete_lun(volume['name'])
            else:
                with excutils.save_and_reraise_exception():
                    # Reraise the original exceiption
                    pass

    @log_enter_exit
    def extend_volume(self, volume, new_size):
        """Extends an EMC volume."""

        self._client.expand_lun_and_wait(volume['name'], new_size)

    @log_enter_exit
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
        array_serial = self.get_array_serial()
        if target_array_serial != array_serial:
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
        src_id = self.get_lun_id(volume)

        thinness = self._get_provisioning_by_volume(volume)
        self._client.create_lun_and_wait(
            target_pool_name,
            new_volume_name, volume['size'], thinness)
        dst_id = self.get_lun_id_by_name(new_volume_name)
        rc = self._client.migrate_lun_with_verification(
            src_id, dst_id, new_volume_name)
        moved = False
        if rc == 0:
            moved = True
        return moved, {}

    def update_volume_status(self):
        if self.protocol == 'iSCSI':
            self.iscsi_targets = self._client.get_iscsi_targets(
                io_ports=self.io_ports)
        return self.stats

    @log_enter_exit
    def create_snapshot(self, snapshot):
        """Creates a snapshot."""

        snapshotname = snapshot['name']
        volumename = snapshot['volume_name']

        LOG.info(_('Create snapshot: %(snapshot)s: volume: %(volume)s')
                 % {'snapshot': snapshotname,
                    'volume': volumename})

        self._client.create_snapshot(volumename, snapshotname)

    @log_enter_exit
    def delete_snapshot(self, snapshot):
        """Deletes a snapshot."""

        snapshotname = snapshot['name']

        LOG.info(_('Delete Snapshot: %(snapshot)s')
                 % {'snapshot': snapshotname})

        self._client.delete_snapshot(snapshotname)

    @log_enter_exit
    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot."""

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

        source_vol_lun_id = self.get_lun_id(volume)
        temp_vol_lun_id = self.get_lun_id_by_name(dest_volume_name)

        LOG.info(_('Migrating Mount Point Volume: %s ') % (volume_name))
        self._client.migrate_lun_with_verification(source_vol_lun_id,
                                                   temp_vol_lun_id, None)
        self._client.check_smp_not_attached(volume_name)

        data = self._client.get_lun_by_name(volume_name)
        pl_dict = {'system': self.get_array_serial(),
                   'type': 'lun',
                   'id': str(data['lun_id'])}
        model_update = {'provider_location':
                        self.dumps_provider_location(pl_dict)}
        volume['provider_location'] = model_update['provider_location']
        return model_update

    @log_enter_exit
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
        model_update = self.create_volume_from_snapshot(volume, snapshot)
        #Delete temp Snapshot
        self.delete_snapshot(snapshot)
        return model_update

    def get_lun_id_by_name(self, volume_name):
        data = self._client.get_lun_by_name(volume_name)
        return data['lun_id']

    def get_lun_id(self, volume):
        lun_id = None
        try:
            if volume.get('provider_location') is not None:
                lun_id = int(
                    volume['provider_location'].split('|')[2].split('^')[1])
            if not lun_id:
                LOG.debug('Lun id is not stored in provider location, '
                          'query it.')
                lun_id = self._client.get_lun_by_name(volume['name'])['lun_id']
        except Exception as ex:
            LOG.debug('Exception when getting lun id: %s.' % (ex))
            lun_id = self._client.get_lun_by_name(volume['name'])['lun_id']
        LOG.debug('Get lun_id: %s.' % (lun_id))
        return lun_id

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
            cmd_iscsi_setpath = ('storagegroup', '-gname', gname,
                                 '-setpath', '-hbauid', initiator_uid,
                                 '-sp', sp, '-spport', port_id, '-spvport',
                                 vport_id, '-ip', ip, '-host', host, '-o')
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

    def auto_register_with_io_port_filter(self, connector, sgdata,
                                          io_port_filter):
        """Automatically register specific IO ports to storage group."""
        initiator = connector['initiator']
        ip = connector['ip']
        host = connector['host']
        new_white = {'A': [], 'B': []}
        if self.protocol == 'iSCSI':
            if sgdata:
                sp_ports = self._client.get_registered_spport_set(
                    initiator, host, sgdata['raw_output'])
                # Normalize io_ports
                for sp in ('A', 'B'):
                    for port in self.iscsi_targets[sp]:
                        if(port['SP'], port['Port ID']) not in sp_ports:
                            new_white[sp].append({'SP': port['SP'],
                                                  'Port ID': port['Port ID'],
                                                  'Virtual Port ID': 0})
            else:
                new_white = self.iscsi_targets
            self._register_iscsi_initiator(ip, host, [initiator], new_white)

        elif self.protocol == 'FC':
            wwns = self._extract_fc_uids(connector)
            ports_list = []
            if sgdata:
                for wwn in wwns:
                    for port in io_port_filter:
                        if (port not in ports_list) and \
                                (not re.search(wwn + '\s+SP\s+' +
                                               port[0] + '\s+' + str(port[1]),
                                               sgdata['raw_output'],
                                               re.IGNORECASE)):
                            # Record ports to be added
                            ports_list.append(port)
                            new_white[port[0]].append({
                                'SP': port[0],
                                'Port ID': port[1]})
            else:
                # Need to translate to dict format
                for fc_port in io_port_filter:
                    new_white[fc_port[0]].append({'SP': fc_port[0],
                                                  'Port ID': fc_port[1]})
            self._register_fc_initiator(ip, host, wwns, new_white)
        return new_white['A'] or new_white['B']

    def _register_iscsi_initiator(self, ip, host, initiator_uids,
                                  port_to_register=None):
        iscsi_targets = (port_to_register if port_to_register else
                         self.iscsi_targets)
        for initiator_uid in initiator_uids:
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

    def _register_fc_initiator(self, ip, host, initiator_uids,
                               ports_to_register=None):
        fc_targets = (ports_to_register if ports_to_register else
                      self._client.get_fc_targets())
        for initiator_uid in initiator_uids:
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

    def _filter_unregistered_initiators(self, initiator_uids, sgdata):
        unregistered_initiators = []
        if not initiator_uids:
            return unregistered_initiators

        out = sgdata['raw_output']

        for initiator_uid in initiator_uids:
            m = re.search(initiator_uid, out)
            if m is None:
                unregistered_initiators.append(initiator_uid)
        return unregistered_initiators

    def auto_register_initiator_to_all(self, connector, sgdata):
        ip = connector['ip']
        host = connector['host']
        if self.protocol == 'iSCSI':
            initiator_uids = self._extract_iscsi_uids(connector)
            if sgdata is not None:
                itors_toReg = self._filter_unregistered_initiators(
                    initiator_uids,
                    sgdata)
            else:
                itors_toReg = initiator_uids

            if len(itors_toReg) == 0:
                return False

            LOG.info(_('iSCSI Initiators %(in)s of %(ins)s need registration')
                     % ({'in': itors_toReg,
                         'ins': initiator_uids}))
            self._register_iscsi_initiator(ip, host, itors_toReg)
            return True

        elif self.protocol == 'FC':
            initiator_uids = self._extract_fc_uids(connector)
            if sgdata is not None:
                itors_toReg = self._filter_unregistered_initiators(
                    initiator_uids,
                    sgdata)
            else:
                itors_toReg = initiator_uids

            if len(itors_toReg) == 0:
                return False

            LOG.info(_('FC Initiators %(in)s of %(ins)s need registration')
                     % ({'in': itors_toReg,
                         'ins': initiator_uids}))
            self._register_fc_initiator(ip, host, itors_toReg)
            return True

    def auto_register_initiator(self, connector, sgdata, io_ports_filter=None):
        """Automatically register available initiators.
        Return True if has registerred initiator otherwise return False
        """
        if io_ports_filter:
            return self.auto_register_with_io_port_filter(connector, sgdata,
                                                          io_ports_filter)
        else:
            return self.auto_register_initiator_to_all(connector, sgdata)

    def assure_host_access(self, volume, connector):
        hostname = connector['host']
        volumename = volume['name']
        auto_registration_done = False
        try:
            sgdata = self._client.get_storage_group(hostname,
                                                    poll=False)
        except EMCVnxCLICmdError as ex:
            if ex.rc != 83:
                raise ex
            # Storage Group has not existed yet
            self.assure_storage_group(hostname)
            if self.itor_auto_reg:
                self.auto_register_initiator(connector, None, self.io_ports)
                auto_registration_done = True
            else:
                self._client.connect_host_to_storage_group(hostname, hostname)

            sgdata = self._client.get_storage_group(hostname,
                                                    poll=True)

        if self.itor_auto_reg and not auto_registration_done:
            new_registerred = self.auto_register_initiator(connector, sgdata,
                                                           self.io_ports)
            if new_registerred:
                sgdata = self._client.get_storage_group(hostname,
                                                        poll=True)

        lun_id = self.get_lun_id(volume)
        tried = 0
        while tried < self.max_retries:
            tried += 1
            lun_map = sgdata['lunmap']
            used_hlus = lun_map.values()
            candidate_hlus = self.filter_available_hlu_set(used_hlus)
            candidate_hlus = list(candidate_hlus)

            if len(candidate_hlus) != 0:
                hlu = candidate_hlus[random.randint(0,
                                                    len(candidate_hlus) - 1)]
                try:
                    self._client.add_hlu_to_storage_group(
                        hlu,
                        lun_id,
                        hostname)
                    if hostname not in self.hlu_cache:
                        self.hlu_cache[hostname] = {}
                    self.hlu_cache[hostname][lun_id] = hlu
                    return hlu, sgdata
                except EMCVnxCLICmdError as ex:
                    LOG.debug("Add HLU to storagegroup failed, retry %s" %
                              tried)
            elif tried == 1:
                # The first tried didn't get the in time data,
                # so we need a retry
                LOG.debug("Add HLU to storagegroup failed, retry %s" %
                          tried)
            else:
                msg = (_('Reach limitation set by configuration '
                         'option max_luns_per_storage_group. '
                         'Operation to add %(vol)s into '
                         'Storage Group %(sg)s is rejected.')
                       % {'vol': volumename, 'sg': hostname})
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)

            # Query storage group with poll for retry
            # we need a full poll to get the real in time data
            sgdata = self._client.get_storage_group(hostname, poll=True)
            self.hlu_cache[hostname] = sgdata['lunmap']
            if lun_id in sgdata['lunmap']:
                hlu = sgdata['lunmap'][lun_id]
                return hlu, sgdata

        msg = _("Failed to add %(vol)s into %(sg)s "
                "after %(retries)s tries.") % \
            {'vol': volumename,
             'sg': hostname,
             'retries': tried}
        LOG.error(msg)
        raise exception.VolumeBackendAPIException(data=msg)

    def _get_iscsi_properties(self, volume, connector, hlu, sg_raw_output):
        storage_group = connector['host']
        owner_sp = self._client.get_lun_by_name(volume['name'],
                                                poll=False)['owner']
        registered_spports = self._client.get_registered_spport_set(
            connector['initiator'],
            storage_group,
            sg_raw_output)

        properties = {'target_discovered': True,
                      'target_iqn': 'unknown',
                      'target_portal': 'unknown',
                      'target_lun': 'unknown',
                      'volume_id': volume['id']}
        target = self._client.find_avaialable_iscsi_target_one(
            storage_group, owner_sp,
            registered_spports,
            self.iscsi_targets)

        if target:
            properties = {'target_discovered': True,
                          'target_iqn': target['Port WWN'],
                          'target_portal': "%s:3260" % target['IP Address'],
                          'target_lun': hlu}
            LOG.debug("iSCSI Properties: %s", properties)
        else:
            LOG.error(_('Failed to find an available iSCSI targets for %s.'),
                      storage_group)
        if self.multi_portals:
            portals = []
            iqns = []
            for sp in ('A', 'B'):
                for one_target in self.iscsi_targets[sp]:
                    portals.append("%s:3260" % one_target['IP Address'])
                    iqns.append(one_target['Port WWN'])
            properties['target_iqns'] = iqns
            properties['target_portals'] = portals
        return properties

    @log_enter_exit
    def initialize_connection(self, volume, connector):
        """Initializes the connection and returns connection info."""

        if self.protocol == 'iSCSI':
            (device_number, sg_data) = \
                self.do_initialize_connection(volume, connector)
            iscsi_properties = self._get_iscsi_properties(
                volume,
                connector,
                device_number,
                sg_data['raw_output']
            )
            return {'driver_volume_type': 'iscsi',
                    'data': iscsi_properties}
        elif self.protocol == 'FC':
            return self.do_initialize_connection(volume,
                                                 connector)[0]

    @log_enter_exit
    def terminate_connection(self, volume, connector):
        """Disallow connection from connector."""
        self.do_terminate_connection(volume, connector)

    @log_enter_exit
    def _do_initialize_connection(self, volume, connector):
        """Initializes the connection and returns connection info."""
        @lockutils.synchronized('emc-connection-' + connector['host'],
                                "emc-connection-", True)
        def inner():
            return self.assure_host_access(
                volume, connector)
        return inner()

    @log_enter_exit
    def _do_terminate_connection(self, volume, connector):
        """Disallow connection from connector."""
        @lockutils.synchronized('emc-connection-' + connector['host'],
                                "emc-connection-", True)
        def do_terminate_connection():
            hostname = connector['host']
            volume_name = volume['name']
            lun_id = self.get_lun_id(volume)
            if (hostname in self.hlu_cache and
                    lun_id in self.hlu_cache[hostname] and not
                    self.destroy_empty_sg):
                hlu = self.hlu_cache[hostname][lun_id]
                self._client.remove_hlu_from_storagegroup(hlu, hostname,
                                                          poll=True)
                self.hlu_cache[hostname].pop(lun_id)
                return False
            else:
                try:
                    lun_map = self.get_lun_map(hostname)
                    self.hlu_cache[hostname] = lun_map
                except EMCVnxCLICmdError as ex:
                    if ex.rc == 83:
                        LOG.warn(_("Storage Group %s is not found. "
                                   "terminate_connection() is unnecessary."),
                                 hostname)
                        return True

                if lun_id in lun_map:
                    self._client.remove_hlu_from_storagegroup(
                        lun_map[lun_id], hostname)
                    lun_map.pop(lun_id)
                else:
                    LOG.warn(_("Volume %(vol)s was not in Storage Group"
                               " %(sg)s.")
                             % {'vol': volume_name, 'sg': hostname})
            if self.destroy_empty_sg and not lun_map:
                try:
                    LOG.info(_("Storage Group %s was empty"), hostname)
                    self._client.disconnect_host_from_storage_group(
                        hostname, hostname)
                    self._client.delete_storage_group(hostname)
                except Exception:
                    LOG.warn(_("Failed to destroy Storage Group %s"),
                             hostname)
                    try:
                        self._client.connect_host_to_storage_group(hostname,
                                                                   hostname)
                    except Exception:
                        LOG.warn(_("Connect host back to storage group"
                                   " failed %s"),
                                 hostname)
        return do_terminate_connection()

    @log_enter_exit
    def _do_initialize_connection_in_batch(self, volume, connector):
        hostname = connector['host']
        alu = self.get_lun_id(volume)
        order = {'alu': alu,
                 'type': BatchOrderType.ADD,
                 'status': AddHluStatus.NEW,
                 'tried': 0,
                 'hlu': None,
                 'msg': '',
                 'payload': None}
        worker = self.get_batch_worker(connector)
        worker.submit(order)
        while (order['status'] == AddHluStatus.NEW):
            LOG.debug("waiting ...........")
            eventlet.sleep(5)
        LOG.debug("A add HLU order for %s is processed"
                  % volume['name'])
        if order['status'] == AddHluStatus.OK:
            return order['hlu'], order['payload']
        elif order['status'] == AddHluStatus.NO_HLU_LEFT:
            msg = (_('Reach limitation set by configuration '
                     'option max_luns_per_storage_group. '
                     'Operation to add %(vol)s into '
                     'Storage Group %(sg)s is rejected.')
                   % {'vol': volume['name'], 'sg': hostname})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        elif order['status'] == AddHluStatus.ABANDON:
            msg = (_("Didn't add %(vol)s into %(sg)s because there is "
                     "new request to remove %(vol)s from %(sg)s")
                   % {'vol': volume['name'],
                      'sg': hostname})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        else:
            msg = (_("Failed to add %(vol)s into %(sg)s ")
                   % {'vol': volume['name'],
                      'sg': hostname})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    @log_enter_exit
    def _do_terminate_connection_in_batch(self, volume, connector):
        hostname = connector['host']
        alu = self.get_lun_id(volume)
        worker = self.get_batch_worker(connector)
        order = {'alu': alu,
                 'type': BatchOrderType.REMOVE,
                 'status': RemoveHluStatus.NEW,
                 'tried': 0,
                 'hlu': None,
                 'msg': '',
                 'payload': None}
        worker.submit(order)
        while (order['status'] == RemoveHluStatus.NEW):
            LOG.debug("waiting ...........")
            eventlet.sleep(5)
        LOG.debug("Remove HLU order for %s is processed"
                  % volume['name'])
        if order['status'] == RemoveHluStatus.OK:
            return
        elif (order['status'] == RemoveHluStatus.HLU_NOT_IN_SG):
            LOG.debug("Volume %(volname)s is not in storagegroup"
                      " %(sgname)s"
                      % {'volname': volume['name'],
                         'sgname': hostname})
            return
        elif order['status'] == RemoveHluStatus.ABANDON:
            msg = (_("Didn't remove %(vol)s from %(sg)s because there is "
                     "new request to add %(vol)s into %(sg)s")
                   % {'vol': volume['name'],
                      'sg': hostname})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        else:
            msg = (_("Failed to remove %(vol)s from %(sg)s ")
                   % {'vol': volume['name'],
                      'sg': hostname})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def get_batch_worker(self, connector):
        hostname = connector['host']

        @lockutils.synchronized('emc-get-addhlu-worker' + hostname,
                                'emc-get-addhlu-worker', False)
        def get_worker():
            if hostname not in self.addhlu_workers:
                self.addhlu_workers[hostname] =\
                    queueworker.BatchWorkerBase(
                        self.get_attach_detach_batch_executor(connector),
                        self.attach_detach_batch_interval)
            return self.addhlu_workers[hostname]
        return get_worker()

    def get_attach_detach_batch_executor(self, connector):
        def executor(orders):
            hostname = connector['host']
            try:
                sgdata = self._prepare_storage_group(connector)
            except Exception as ex:
                LOG.error("Failed to prepare storage group. %s" % ex)
                for order in orders:
                    order['status'] = queueworker.Status.FAILURE
                return

            lunmap = sgdata['lunmap']
            used_hlus = lunmap.values()
            candidate_hlus = self.filter_available_hlu_set(used_hlus)
            candidate_hlus = list(candidate_hlus)
            random.shuffle(candidate_hlus)
            addhlus = []
            addalus = []
            add_ordermap = {}
            removehlus = []
            remove_ordermap = {}
            effective_ordermap = {}
            LOG.debug("Get %s orders" % len(orders))
            for order in orders:
                alu = order['alu']
                if alu not in effective_ordermap:
                    effective_ordermap[alu] = [order]
                elif order['type'] == effective_ordermap[alu][0]['type']:
                    # Duplicated orders which may happen in the retry
                    effective_ordermap[alu].append(order)
                else:
                    # This may happen when there is rollback after timeout
                    LOG.debug("There is different order type for a same "
                              "alu, the orders in old type will be abandon")
                    for item in effective_ordermap[alu]:
                        item['status'] = queueworker.Status.ABANDON

                    effective_ordermap[alu] = [order]

            for alu in effective_ordermap:
                orders = effective_ordermap[alu]
                order = orders[0]
                if order['type'] == BatchOrderType.ADD:
                    if alu in lunmap:
                        LOG.debug("LUN %s already in SG" % alu)
                        for order in orders:
                            order['status'] = AddHluStatus.OK
                            order['hlu'] = lunmap[alu]
                            order['payload'] = sgdata

                    elif candidate_hlus:
                        addalus.append(alu)
                        hlu = candidate_hlus.pop()
                        addhlus.append(hlu)
                        add_ordermap[alu] = {'orders': orders,
                                             'hlu': hlu}
                    else:
                        # No hlu left
                        for order in orders:
                            order['status'] = AddHluStatus.NO_HLU_LEFT
                else:
                    if alu not in lunmap:
                        LOG.debug("LUN %(alu)s is not in the storage group")
                        for order in orders:
                            order['status'] = RemoveHluStatus.HLU_NOT_IN_SG
                    else:
                        hlu = lunmap[alu]
                        removehlus.append(hlu)
                        remove_ordermap[hlu] = {'orders': orders}

            self._process_addhlu_in_batch(addalus, addhlus, hostname,
                                          add_ordermap, connector,
                                          sgdata)
            self._process_removehlu_in_batch(removehlus, hostname,
                                             remove_ordermap, connector)
            if self.destroy_empty_sg and not addalus:
                @lockutils.synchronized('emc-remove-storagegroup-' + hostname,
                                        'emc-remove-storagegroup-', True)
                def _delete_empty_storage_group(sgname):
                    lun_map = self.get_lun_map(sgname)
                    try:
                        if not lun_map:
                            LOG.info(_("Storage Group %s was empty"), sgname)
                            self._client.disconnect_host_from_storage_group(
                                sgname, sgname)
                            self._client.delete_storage_group(sgname)
                    except Exception:
                        LOG.warn(_("Failed to destroy Storage Group %s"),
                                 sgname)
                        try:
                            self._client.connect_host_to_storage_group(sgname,
                                                                       sgname)
                        except Exception:
                            LOG.warn(_("Connect host back to storage group"
                                       " failed %s"),
                                     sgname)
                _delete_empty_storage_group(hostname)
        return executor

    def _prepare_storage_group(self, connector):
        hostname = connector['host']
        auto_registration_done = False
        try:
            sgdata = self._client.get_storage_group(hostname,
                                                    poll=True)
        except EMCVnxCLICmdError as ex:
            if ex.rc != 83:
                raise ex
                # Storage Group has not existed yet
            self.assure_storage_group(hostname)
            if self.itor_auto_reg:
                self.auto_register_initiator(connector, None, self.io_ports)
                auto_registration_done = True
            else:
                self._client.connect_host_to_storage_group(hostname,
                                                           hostname)

            sgdata = self._client.get_storage_group(hostname,
                                                    poll=True)

        if self.itor_auto_reg and not auto_registration_done:
            new_registerred = self.auto_register_initiator(connector, sgdata,
                                                           self.io_ports)
            if new_registerred:
                sgdata = self._client.get_storage_group(hostname,
                                                        poll=True)
        return sgdata

    def _process_addhlu_in_batch(self, alus, hlus, hostname,
                                 alu_ordermap, connector, sgdata):
        if len(alus) == 0:
            return

        (succeed_list, failed_list) = \
            self._client.add_hlus_to_storage_group(hlus,
                                                   alus,
                                                   hostname)
        for alu in succeed_list:
            map_item = alu_ordermap.pop(alu)
            for order in map_item['orders']:
                order['hlu'] = map_item['hlu']
                order['status'] = AddHluStatus.OK
                order['payload'] = sgdata

        worker = self.get_batch_worker(connector)
        for alu in failed_list:
            map_item = alu_ordermap.pop(alu)
            for order in map_item['orders']:
                if order['tried'] + 1 >= self.max_retries:
                    # Excced Max Retry, mark status to Failure
                    order['status'] = AddHluStatus.FAILURE
                else:
                    order['tried'] = 1 + order['tried']
                    worker.submit(order)

    def _process_removehlu_in_batch(self, hlus, hostname,
                                    hlu_ordermap, connector):
        if len(hlus) == 0:
            return

        (succeed_list, failed_list) = \
            self._client.remove_hlus_from_storagegroup(hlus,
                                                       hostname)
        for hlu in succeed_list:
            map_item = hlu_ordermap.pop(hlu)
            for order in map_item['orders']:
                order['status'] = RemoveHluStatus.OK

        worker = self.get_batch_worker(connector)
        for hlu in failed_list:
            map_item = hlu_ordermap.pop(hlu)
            for order in map_item['orders']:
                if order['tried'] + 1 >= 2:
                    # Mark status to Failure in retry
                    order['status'] = RemoveHluStatus.FAILURE
                else:
                    order['tried'] = 1 + order['tried']
                    worker.submit(order)

    def find_iscsi_protocol_endpoints(self, device_sp):
        """Returns the iSCSI initiators for a SP."""
        return self._client.get_iscsi_protocol_endpoints(device_sp)

    def get_target_wwns(self, connector):
        if self.io_ports:
            registered_wwns = self._client.get_white_list_wwns(self.io_ports)
        else:
            registered_wwns = self._client.get_target_wwns(connector['host'])
        return registered_wwns

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
        self.pool_cache = self._client.get_pool_list(poll=False)

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
