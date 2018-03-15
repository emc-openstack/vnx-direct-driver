# Copyright (c) 2012 - 2015 EMC Corporation, Inc.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""
VNX CLI
"""
import copy
import functools
import math
import os
import random
import re
import time
import types

import eventlet
from oslo_concurrency import lockutils
from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils as json
from oslo_service import loopingcall
from oslo_utils import excutils
from oslo_utils import timeutils
import six
from six.moves import range
import taskflow.engines
from taskflow.patterns import linear_flow
from taskflow import task
from taskflow.types import failure

from cinder import exception
from cinder.i18n import _, _LE, _LI, _LW
from cinder.objects import fields
from cinder import utils
from cinder.volume import configuration as config
from cinder.volume.drivers.san import san
from cinder.volume import manager
from cinder.volume import utils as vol_utils
from cinder.volume import volume_types

CONF = cfg.CONF

LOG = logging.getLogger(__name__)


INTERVAL_5_SEC = 5
INTERVAL_20_SEC = 20
INTERVAL_30_SEC = 30
INTERVAL_60_SEC = 60
SNAP_EXPIRATION_HOUR = 1
ENABLE_TRACE = False

loc_opts = [
    cfg.StrOpt('storage_vnx_authentication_type',
               default='global',
               help='VNX authentication scope type.'),
    cfg.StrOpt('storage_vnx_security_file_dir',
               help='Directory path that contains the VNX security file. '
               'Make sure the security file is generated first.'),
    cfg.StrOpt('naviseccli_path',
               default='',
               help='Naviseccli Path.'),
    cfg.StrOpt('storage_vnx_pool_names',
               deprecated_name='storage_vnx_pool_name',
               help='Comma-separated list of storage pool names to be used.'),
    cfg.StrOpt('san_secondary_ip',
               help='VNX secondary SP IP Address.'),
    cfg.IntOpt('default_timeout',
               default=60 * 24 * 365,
               help='Default timeout for CLI operations in minutes. '
               'For example, LUN migration is a typical long '
               'running operation, which depends on the LUN size and '
               'the load of the array. '
               'An upper bound in the specific deployment can be set to '
               'avoid unnecessary long wait. '
               'By default, it is 365 days long.'),
    cfg.IntOpt('max_luns_per_storage_group',
               default=255,
               help='Default max number of LUNs in a storage group.'
               ' By default, the value is 255.'),
    cfg.BoolOpt('destroy_empty_storage_group',
                default=False,
                help='To destroy storage group '
                'when the last LUN is removed from it. '
                'By default, the value is False.'),
    cfg.StrOpt('iscsi_initiators',
               default='',
               help='Mapping between hostname and '
               'its iSCSI initiator IP addresses.'),
    cfg.StrOpt('io_port_list',
               default='*',
               help='Comma separated iSCSI or FC ports '
               'to be used in Nova or Cinder.'),
    cfg.BoolOpt('initiator_auto_registration',
                default=False,
                help='Automatically register initiators. '
                'By default, the value is False.'),
    cfg.BoolOpt('initiator_auto_deregistration',
                default=False,
                help='Automatically deregister initiators after the related '
                'storage group is destroyed. '
                'By default, the value is False.'),
    cfg.BoolOpt('check_max_pool_luns_threshold',
                default=False,
                help='Report free_capacity_gb as 0 when the limit to '
                'maximum number of pool LUNs is reached. '
                'By default, the value is False.'),
    cfg.BoolOpt('force_delete_lun_in_storagegroup',
                default=False,
                help='Delete a LUN even if it is in Storage Groups.'),
    cfg.BoolOpt('ignore_pool_full_threshold',
                default=False,
                help='Force LUN creation even if '
                'the full threshold of pool is reached.')
]

CONF.register_opts(loc_opts)


def decorate_all_methods(method_decorator):
    """Applies decorator on the methods of a class.

    This is a class decorator, which will apply method decorator referred
    by method_decorator to all the public methods (without underscore as
    the prefix) in a class.
    """
    if not ENABLE_TRACE:
        return lambda cls: cls

    def _decorate_all_methods(cls):
        for attr_name, attr_val in cls.__dict__.items():
            if (isinstance(attr_val, types.FunctionType) and
                    not attr_name.startswith("_")):
                setattr(cls, attr_name, method_decorator(attr_val))
        return cls

    return _decorate_all_methods


def log_enter_exit(func):
    if not CONF.debug:
        return func

    def inner(self, *args, **kwargs):
        LOG.debug("Entering %(cls)s.%(method)s",
                  {'cls': self.__class__.__name__,
                   'method': func.__name__})
        start = timeutils.utcnow()
        ret = func(self, *args, **kwargs)
        end = timeutils.utcnow()
        LOG.debug("Exiting %(cls)s.%(method)s. "
                  "Spent %(duration)s sec. "
                  "Return %(return)s",
                  {'cls': self.__class__.__name__,
                   'duration': timeutils.delta_seconds(start, end),
                   'method': func.__name__,
                   'return': ret})
        return ret
    return inner


class EMCLunUsedByFeature(exception.EMCVnxCLICmdError):
    message = ("LUN is used by a feature of the storage system: %(cmd)s "
               "(Return Code: %(rc)s) (Output: %(out)s).")


class EMCLunInSG(exception.EMCVnxCLICmdError):
    message = ("LUN is still in some storage group(s): %(cmd)s "
               "(Return Code: %(rc)s) (Output: %(out)s).")


class PropertyDescriptor(object):
    def __init__(self, option, label, key=None, converter=None):
        self.option = option
        self._label = None
        self._key = key
        self.converter = converter

        self.label = label

    @property
    def label(self):
        return self._label

    @label.setter
    def label(self, value):
        value = value.strip()
        if value[-1] == ':':
            value = value[:-1]
        self._label = value

    @property
    def key(self):
        if self._key is None:
            self._key = '_'.join(self.label.lower().split())
        return self._key


class _Enum(object):
    @classmethod
    def get_all(cls):
        return [getattr(cls, member) for member in dir(cls)
                if cls._is_enum(member)]

    @classmethod
    def _is_enum(cls, name):
        return (isinstance(name, str)
                and hasattr(cls, name)
                and name.isupper())

    @classmethod
    def get_opt(cls, tier):
        option_map = getattr(cls, '_map', None)
        if option_map is None:
            raise NotImplementedError(
                _('Option map (cls._map) is not defined.'))

        ret = option_map.get(tier)
        if ret is None:
            raise ValueError(_("{} is not a valid option.").format(tier))
        return ret


class VNXError(_Enum):

    GENERAL_NOT_FOUND = 'cannot find|may not exist|does not exist'

    SG_NAME_IN_USE = 'Storage Group name already in use'

    LUN_ALREADY_EXPANDED = 0x712d8e04
    LUN_EXISTED = 0x712d8d04
    LUN_IS_PREPARING = 0x712d8e0e
    LUN_IN_SG = 'contained in a Storage Group|LUN mapping still exists'
    LUN_NOT_MIGRATING = ('The specified source LUN is '
                         'not currently migrating')
    LUN_IS_NOT_SMP = 'it is not a snapshot mount point'

    CG_IS_DELETING = 0x712d8801
    CG_EXISTED = 0x716d8021
    CG_SNAP_NAME_EXISTED = 0x716d8005

    SNAP_NAME_EXISTED = 0x716d8005
    SNAP_NAME_IN_USE = 0x716d8003
    SNAP_ALREADY_MOUNTED = 0x716d8055
    SNAP_NOT_ATTACHED = ('The specified Snapshot mount point '
                         'is not currently attached.')
    MIRROR_NOT_FOUND = 'Mirror not found'
    MIRROR_IN_USE = 'Mirror name already in use'

    LUN_USED_BY_FEATURE = (
        "Cannot unbind LUN because it's being used by"
        " a feature of the Storage System")

    @staticmethod
    def _match(output, error_code):
        is_match = False
        if VNXError._is_enum(error_code):
            error_code = getattr(VNXError, error_code)

        if isinstance(error_code, int):
            error_code = hex(error_code)

        if isinstance(error_code, str):
            error_code = error_code.strip()
            found = re.findall(error_code, output,
                               flags=re.IGNORECASE)
            is_match = len(found) > 0
        return is_match

    @classmethod
    def has_error(cls, output, *error_codes):
        if error_codes is None or len(error_codes) == 0:
            error_codes = VNXError.get_all()
        return any([cls._match(output, error_code)
                    for error_code in error_codes])


class VNXMigrationRate(_Enum):
    LOW = 'low'
    MEDIUM = 'medium'
    HIGH = 'high'
    ASAP = 'asap'


class VNXProvisionEnum(_Enum):
    THIN = 'thin'
    THICK = 'thick'
    COMPRESSED = 'compressed'
    DEDUPED = 'deduplicated'

    _map = {
        THIN: ['-type', 'Thin'],
        THICK: ['-type', 'NonThin'],
        COMPRESSED: ['-type', 'Thin'],
        DEDUPED: ['-type', 'Thin', '-deduplication', 'on']}


class VNXTieringEnum(_Enum):
    NONE = 'none'
    HIGH_AUTO = 'starthighthenauto'
    AUTO = 'auto'
    HIGH = 'highestavailable'
    LOW = 'lowestavailable'
    NO_MOVE = 'nomovement'

    _map = {
        NONE: ['', ''],
        HIGH_AUTO: [
            '-initialTier', 'highestAvailable',
            '-tieringPolicy', 'autoTier'],
        AUTO: [
            '-initialTier', 'optimizePool',
            '-tieringPolicy', 'autoTier'],
        HIGH: [
            '-initialTier', 'highestAvailable',
            '-tieringPolicy', 'highestAvailable'],
        LOW: [
            '-initialTier', 'lowestAvailable',
            '-tieringPolicy', 'lowestAvailable'],
        NO_MOVE: [
            '-initialTier', 'optimizePool',
            '-tieringPolicy', 'noMovement']
    }

    @classmethod
    def get_tier(cls, initial, policy):
        ret = None
        for k, v in cls._map.items():
            if len(v) >= 4:
                v_initial, v_policy = v[1], v[3]
                if (cls.match_option(initial, v_initial) and
                        cls.match_option(policy, v_policy)):
                    ret = k
                    break
                elif cls.match_option(policy, 'noMovement'):
                    # no movement could have different initial tier
                    ret = cls.NO_MOVE
                    break
        if ret is None:
            raise ValueError(_('Initial tier: {}, policy: {} is not valid.')
                             .format(initial, policy))
        return ret

    @staticmethod
    def match_option(output, option):
        return output.replace(' ', '').lower() == option.lower()


class VNXLun(object):

    DEFAULT_TIER = VNXTieringEnum.HIGH_AUTO
    DEFAULT_PROVISION = VNXProvisionEnum.THICK

    def __init__(self):
        self._lun_id = -1
        self._capacity = 0.0
        self._pool_name = ''
        self._tier = self.DEFAULT_TIER
        self._provision = self.DEFAULT_PROVISION

        self._const = VNXLunProperties

    @property
    def lun_id(self):
        return self._lun_id

    @lun_id.setter
    def lun_id(self, data):
        if isinstance(data, dict):
            self._lun_id = self._get(data, self._const.LUN_ID)
        elif isinstance(data, int):
            self._lun_id = data
        elif isinstance(data, str):
            try:
                self._lun_id = int(data)
            except ValueError:
                raise ValueError(
                    _('LUN number ({}) is not an integer.').format(data))
        else:
            self._raise_type_error(data)

        if self.lun_id < 0:
            raise ValueError(_('LUN id({}) is not valid.')
                             .format(self.lun_id))

    @property
    def pool_name(self):
        return self._pool_name

    @pool_name.setter
    def pool_name(self, data):
        if isinstance(data, dict):
            self._pool_name = self._get(data, self._const.LUN_POOL)
        elif isinstance(data, str):
            self._pool_name = data
        else:
            self._raise_type_error(data)

    @property
    def capacity(self):
        return self._capacity

    @capacity.setter
    def capacity(self, data):
        if isinstance(data, dict):
            self._capacity = self._get(data, self._const.LUN_CAPACITY)
        elif isinstance(data, float):
            self._capacity = data
        elif isinstance(data, int):
            self._capacity = float(data)
        else:
            self._raise_type_error(data)

    @property
    def tier(self):
        return self._tier

    @tier.setter
    def tier(self, data):
        if isinstance(data, dict):
            initial = self._get(data, self._const.LUN_INITIAL_TIER)
            policy = self._get(data, self._const.LUN_TIERING_POLICY)

            self._tier = VNXTieringEnum.get_tier(initial, policy)
        elif isinstance(data, str) and data in VNXTieringEnum.get_all():
            self._tier = data
        else:
            self._raise_type_error(data)

    @property
    def provision(self):
        return self._provision

    @provision.setter
    def provision(self, data):
        self._provision = VNXProvisionEnum.THICK
        if isinstance(data, dict):
            is_thin = self._get(data, self._const.LUN_IS_THIN_LUN)
            is_compressed = self._get(data, self._const.LUN_IS_COMPRESSED)
            is_dedup = self._get(data, self._const.LUN_DEDUP_STATE)

            if is_compressed:
                self._provision = VNXProvisionEnum.COMPRESSED
            elif is_dedup:
                self._provision = VNXProvisionEnum.DEDUPED
            elif is_thin:
                self._provision = VNXProvisionEnum.THIN
        elif isinstance(data, str) and data in VNXProvisionEnum.get_all():
            self._provision = data
        else:
            self._raise_type_error(data)

    @staticmethod
    def _raise_type_error(data):
        raise ValueError(_('Input type {} is not supported.')
                         .format(type(data)))

    def update(self, data):
        self.lun_id = data
        self.pool_name = data
        self.capacity = data
        self.provision = data
        self.tier = data

    @staticmethod
    def get_lun_by_id(client, lun_id):
        lun = VNXLun()
        lun.lun_id = lun_id
        lun.update(client)
        return lun

    @staticmethod
    def _get(data, key):
        if isinstance(key, PropertyDescriptor):
            key = key.key
        return data.get(key)

    def __repr__(self):
        return ('VNXLun ['
                'lun_id: {}, '
                'pool_name: {}, '
                'capacity: {}, '
                'provision: {}, '
                'tier: {}]'
                .format(self.lun_id,
                        self.pool_name,
                        self.capacity,
                        self.provision,
                        self.tier))


class Converter(object):
    @staticmethod
    def str_to_boolean(str_input):
        ret = False
        if str_input.strip().lower() in ('yes', 'true', 'enabled', 'on'):
            ret = True
        return ret


class Dict(dict):
    def __getattr__(self, item):
        try:
            ret = super(Dict, self).__getattr__(item)
        except AttributeError:
            if item in self:
                value = self.get(item)
            else:
                raise AttributeError(
                    _("'{}' object has no attribute '{}'")
                    .format(__name__, item))
            ret = value
        return ret


class VNXCliParser(_Enum):
    @classmethod
    def get_all_property_descriptor(cls):
        return (p for p in cls.get_all()
                if isinstance(p, PropertyDescriptor))

    @classmethod
    def get_property_options(cls):
        properties = cls.get_all_property_descriptor()
        return [p.option for p in properties if p.option is not None]

    @classmethod
    def parse(cls, output, properties=None):
        ret = Dict()
        output = output.strip()

        if properties is None:
            properties = cls.get_all_property_descriptor()

        for p in properties:
            pattern = re.compile(
                '^\s*{}\s*[:]?\s*(?P<value>.*)\s*$'.format(
                    re.escape(p.label)),
                re.MULTILINE | re.IGNORECASE)
            matched = re.search(pattern, output)

            if matched is not None:
                value = matched.group('value')
                if p.converter is not None and callable(p.converter):
                    value = p.converter(value)
                ret[p.key] = value
            else:
                ret[p.key] = None
        return ret


class VNXLunProperties(VNXCliParser):
    LUN_STATE = PropertyDescriptor(
        '-state',
        'Current State',
        'state')
    LUN_STATUS = PropertyDescriptor(
        '-status',
        'Status')
    LUN_OPERATION = PropertyDescriptor(
        '-opDetails',
        'Current Operation',
        'operation')
    LUN_CAPACITY = PropertyDescriptor(
        '-userCap',
        'User Capacity (GBs)',
        'total_capacity_gb',
        float)
    LUN_OWNER = PropertyDescriptor(
        '-owner',
        'Current Owner',
        'owner')
    LUN_ATTACHEDSNAP = PropertyDescriptor(
        '-attachedSnapshot',
        'Attached Snapshot')
    LUN_NAME = PropertyDescriptor(
        None,
        'Name',
        'lun_name')
    LUN_ID = PropertyDescriptor(
        None,
        'LOGICAL UNIT NUMBER',
        'lun_id',
        int)
    LUN_POOL = PropertyDescriptor(
        '-poolName',
        'Pool Name',
        'pool')
    LUN_IS_THIN_LUN = PropertyDescriptor(
        '-isThinLUN',
        'Is Thin LUN',
        converter=Converter.str_to_boolean)
    LUN_IS_COMPRESSED = PropertyDescriptor(
        '-isCompressed',
        'Is Compressed',
        converter=Converter.str_to_boolean)
    LUN_DEDUP_STATE = PropertyDescriptor(
        '-dedupState',
        'Deduplication State',
        'dedup_state',
        Converter.str_to_boolean)
    LUN_INITIAL_TIER = PropertyDescriptor(
        '-initialTier',
        'Initial Tier')
    LUN_TIERING_POLICY = PropertyDescriptor(
        '-tieringPolicy',
        'Tiering Policy')

    lun_all = [LUN_STATE,
               LUN_STATUS,
               LUN_OPERATION,
               LUN_CAPACITY,
               LUN_OWNER,
               LUN_ATTACHEDSNAP]

    lun_with_pool = [LUN_STATE,
                     LUN_CAPACITY,
                     LUN_OWNER,
                     LUN_ATTACHEDSNAP,
                     LUN_POOL]


class VNXPoolProperties(VNXCliParser):
    POOL_ID = PropertyDescriptor(
        None,
        'Pool ID',
        'pool_id',
        int)
    POOL_STATE = PropertyDescriptor(
        '-state',
        'State')
    POOL_TOTAL_CAPACITY = PropertyDescriptor(
        '-userCap',
        'User Capacity (GBs)',
        'total_capacity_gb',
        float)
    POOL_FREE_CAPACITY = PropertyDescriptor(
        '-availableCap',
        'Available Capacity (GBs)',
        'free_capacity_gb',
        float)
    POOL_FAST_CACHE = PropertyDescriptor(
        '-fastcache',
        'FAST Cache',
        'fast_cache_enabled',
        Converter.str_to_boolean)
    POOL_NAME = PropertyDescriptor(
        None,
        'Pool Name')
    POOL_SUBSCRIBED_CAPACITY = PropertyDescriptor(
        '-subscribedCap',
        'Total Subscribed Capacity (GBs)',
        'provisioned_capacity_gb',
        float)
    POOL_FULL_THRESHOLD = PropertyDescriptor(
        '-prcntFullThreshold',
        'Percent Full Threshold',
        'pool_full_threshold',
        int)

    pool_all = [POOL_TOTAL_CAPACITY,
                POOL_FREE_CAPACITY,
                POOL_STATE,
                POOL_FULL_THRESHOLD]


class VNXPoolFeatureProperties(VNXCliParser):
    MAX_POOL_LUNS = PropertyDescriptor(
        '-maxPoolLUNs',
        'Max. Pool LUNs',
        'max_pool_luns',
        int)
    TOTAL_POOL_LUNS = PropertyDescriptor(
        '-numPoolLUNs',
        'Total Number of Pool LUNs',
        'total_pool_luns',
        int)

    default = [MAX_POOL_LUNS, TOTAL_POOL_LUNS]


@decorate_all_methods(log_enter_exit)
class CommandLineHelper(object):
    # extra spec constants
    tiering_spec = 'storagetype:tiering'
    provisioning_specs = [
        'provisioning:type',
        'storagetype:provisioning']
    copytype_spec = 'copytype:snap'

    def __init__(self, configuration):
        configuration.append_config_values(san.san_opts)

        self.timeout = configuration.default_timeout * INTERVAL_60_SEC
        self.max_luns = configuration.max_luns_per_storage_group

        # Checking for existence of naviseccli tool
        navisecclipath = configuration.naviseccli_path
        if not os.path.exists(navisecclipath):
            err_msg = _('naviseccli_path: Could not find '
                        'NAVISECCLI tool %(path)s.') % {'path': navisecclipath}
            LOG.error(err_msg)
            raise exception.VolumeBackendAPIException(data=err_msg)

        self.command = (navisecclipath, '-address')
        self.active_storage_ip = configuration.san_ip
        self.primary_storage_ip = self.active_storage_ip
        self.secondary_storage_ip = configuration.san_secondary_ip
        if self.secondary_storage_ip == self.primary_storage_ip:
            LOG.warning(_LW("san_secondary_ip is configured as "
                            "the same value as san_ip."))
            self.secondary_storage_ip = None
        if not configuration.san_ip:
            err_msg = _('san_ip: Mandatory field configuration. '
                        'san_ip is not set.')
            LOG.error(err_msg)
            raise exception.VolumeBackendAPIException(data=err_msg)
        # Lock file name for this specific back-end
        self.toggle_lock_name = (configuration.config_group if
                                 configuration.config_group else 'default')
        self.credentials = ()
        storage_username = configuration.san_login
        storage_password = configuration.san_password
        storage_auth_type = configuration.storage_vnx_authentication_type
        storage_vnx_security_file = configuration.storage_vnx_security_file_dir

        if storage_auth_type is None:
            storage_auth_type = 'global'
        elif storage_auth_type.lower() not in ('ldap', 'local', 'global'):
            err_msg = (_('Invalid VNX authentication type: %s')
                       % storage_auth_type)
            LOG.error(err_msg)
            raise exception.VolumeBackendAPIException(data=err_msg)
        # if there is security file path provided, use this security file
        if storage_vnx_security_file:
            self.credentials = ('-secfilepath', storage_vnx_security_file)
            LOG.info(_LI("Using security file in %s for authentication"),
                     storage_vnx_security_file)
        # if there is a username/password provided, use those in the cmd line
        elif storage_username is not None and len(storage_username) > 0 and\
                storage_password is not None and len(storage_password) > 0:
            self.credentials = ('-user', storage_username,
                                '-password', storage_password,
                                '-scope', storage_auth_type)
            LOG.info(_LI("Plain text credentials are being used for "
                         "authentication"))
        else:
            LOG.info(_LI("Neither security file nor plain "
                         "text credentials are specified. Security file under "
                         "home directory will be used for authentication "
                         "if present."))

        self.iscsi_initiator_map = None
        if configuration.iscsi_initiators:
            self.iscsi_initiator_map = \
                json.loads(configuration.iscsi_initiators)
            LOG.info(_LI("iscsi_initiators: %s"), self.iscsi_initiator_map)

    def _raise_cli_error(self, cmd=None, rc=None, out='', **kwargs):
        raise exception.EMCVnxCLICmdError(cmd=cmd,
                                          rc=rc,
                                          out=out.split('\n'),
                                          **kwargs)

    def create_lun_with_advance_feature(self, pool, name, size,
                                        provisioning, tiering,
                                        consistencygroup_id=None,
                                        ignore_thresholds=False,
                                        poll=True):
        command_create_lun = ['lun', '-create',
                              '-capacity', int(size),
                              '-sq', 'gb',
                              '-poolName', pool,
                              '-name', name]
        if not poll:
            command_create_lun = ['-np'] + command_create_lun
        # provisioning
        if provisioning:
            command_create_lun.extend(VNXProvisionEnum.get_opt(provisioning))
        # tiering
        if tiering and tiering != 'none':
            command_create_lun.extend(VNXTieringEnum.get_opt(tiering))
        if ignore_thresholds:
            command_create_lun.append('-ignoreThresholds')

        # create lun
        data = self.create_lun_by_cmd(command_create_lun, name)

        # handle compression
        try:
            if provisioning == 'compressed':
                self.enable_or_disable_compression_on_lun(
                    name, 'on')
        except exception.EMCVnxCLICmdError as ex:
            with excutils.save_and_reraise_exception():
                self.delete_lun(name)
                LOG.error(_LE("Error on enable compression on lun %s."),
                          ex)

        # handle consistency group
        try:
            if consistencygroup_id:
                self.add_lun_to_consistency_group(
                    consistencygroup_id, data['lun_id'])
        except exception.EMCVnxCLICmdError as ex:
            with excutils.save_and_reraise_exception():
                self.delete_lun(name)
                LOG.error(_LE("Error on adding lun to consistency"
                              " group. %s"), ex)
        return data

    def create_lun_by_cmd(self, cmd, name):
        out, rc = self.command_execute(*cmd)
        if rc != 0:
            # Ignore the error that due to retry
            if VNXError.has_error(out, VNXError.LUN_EXISTED):
                LOG.warning(_LW('LUN already exists, LUN name %(name)s. '
                                'Message: %(msg)s'),
                            {'name': name, 'msg': out})
            else:
                self._raise_cli_error(cmd, rc, out)

        def _lun_state_validation(lun_data):
            lun_state = lun_data[VNXLunProperties.LUN_STATE.key]
            if lun_state == 'Initializing':
                return False
            # Lun in Ready or Faulted state is eligible for IO access,
            # so if no lun operation, return success.
            elif lun_state in ['Ready', 'Faulted']:
                return lun_data[VNXLunProperties.LUN_OPERATION.key] == 'None'
            # Raise exception if lun state is Offline, Invalid, Destroying
            # or other unexpected states.
            else:
                msg = (_("Volume %(name)s was created in VNX, "
                         "but in %(state)s state.")
                       % {'name': lun_data[VNXLunProperties.LUN_NAME.key],
                          'state': lun_state})
                raise exception.VolumeBackendAPIException(data=msg)

        def lun_is_ready():
            try:
                data = self.get_lun_by_name(name,
                                            VNXLunProperties.lun_all,
                                            False)
            except exception.EMCVnxCLICmdError as ex:
                orig_out = "\n".join(ex.kwargs["out"])
                if VNXError.has_error(orig_out, VNXError.GENERAL_NOT_FOUND):
                    return False
                else:
                    raise
            return _lun_state_validation(data)

        self._wait_for_a_condition(lun_is_ready,
                                   interval=INTERVAL_5_SEC,
                                   ignorable_exception_arbiter=lambda ex:
                                   isinstance(ex, exception.EMCVnxCLICmdError))
        lun = self.get_lun_by_name(name, VNXLunProperties.lun_all, False)
        return lun

    def delete_lun(self, name):
        """Deletes a LUN or mount point."""
        command_delete_lun = ['lun', '-destroy',
                              '-name', name,
                              '-forceDetach',
                              '-o']
        # executing cli command to delete volume
        out, rc = self.command_execute(*command_delete_lun)
        if rc != 0 or out.strip():
            # Ignore the error that due to retry
            if VNXError.has_error(out, VNXError.GENERAL_NOT_FOUND):
                LOG.warning(_LW("LUN is already deleted, LUN name %(name)s. "
                                "Message: %(msg)s"),
                            {'name': name, 'msg': out})
            elif VNXError.has_error(
                    out, VNXError.LUN_USED_BY_FEATURE):
                LOG.info(_LW("LUN %(name)s is used by a feature."
                             "Message: %(msg)s"),
                         {'name': name, 'msg': out})
                raise EMCLunUsedByFeature(cmd=command_delete_lun,
                                          rc=rc,
                                          out=out)
            elif VNXError.has_error(
                    out, VNXError.LUN_IN_SG):
                raise EMCLunInSG(cmd=command_delete_lun,
                                 rc=rc,
                                 out=out)
            else:
                self._raise_cli_error(command_delete_lun, rc, out)

    def remove_lun_from_sg(self, name, lun_id):
        for hlu, sg in self.get_hlus(lun_id):
            self.remove_hlu_from_storagegroup(hlu, sg)

    def get_hlus(self, lun_id, poll=True):
        hlus = list()
        command_storage_group_list = ('storagegroup', '-list')
        out, rc = self.command_execute(*command_storage_group_list,
                                       poll=poll)
        if rc != 0:
            self._raise_cli_error(command_storage_group_list, rc, out)
        sg_name_p = re.compile(r'^\s*(?P<sg_name>[^\n\r]+)')
        hlu_alu_p = re.compile(r'HLU/ALU Pairs:'
                               r'\s*HLU Number\s*ALU Number'
                               r'\s*[-\s]*'
                               r'(\d|\s)*'
                               r'\s+(?P<hlu>\d+)( |\t)+%s\n' % lun_id)
        for sg_info in out.split('Storage Group Name:'):
            hlu_alu_m = hlu_alu_p.search(sg_info)
            if hlu_alu_m is None:
                continue
            sg_name_m = sg_name_p.search(sg_info)
            if sg_name_m:
                hlus.append((hlu_alu_m.group('hlu'),
                             sg_name_m.group('sg_name')))
        return hlus

    def _wait_for_a_condition(self, testmethod, timeout=None,
                              interval=INTERVAL_5_SEC,
                              ignorable_exception_arbiter=lambda ex: True,
                              *args, **kwargs):
        start_time = time.time()
        if timeout is None:
            timeout = self.timeout

        def _inner():
            try:
                test_value = testmethod(*args, **kwargs)
            except Exception as ex:
                test_value = False
                with excutils.save_and_reraise_exception(
                        reraise=not ignorable_exception_arbiter(ex)):
                    LOG.debug('CommandLineHelper.'
                              '_wait_for_a_condition: %(method_name)s '
                              'execution failed for %(exception)s',
                              {'method_name': testmethod.__name__,
                               'exception': ex})
            if test_value:
                raise loopingcall.LoopingCallDone()

            if int(time.time()) - start_time > timeout:
                msg = (_('CommandLineHelper._wait_for_a_condition: %s timeout')
                       % testmethod.__name__)
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)

        timer = loopingcall.FixedIntervalLoopingCall(_inner)
        timer.start(interval=interval).wait()

    def expand_lun(self, name, new_size, poll=True):

        command_expand_lun = ('lun', '-expand',
                              '-name', name,
                              '-capacity', new_size,
                              '-sq', 'gb',
                              '-o',
                              '-ignoreThresholds')
        out, rc = self.command_execute(*command_expand_lun,
                                       poll=poll)
        if rc != 0:
            # Ignore the error that due to retry
            if VNXError.has_error(out, VNXError.LUN_ALREADY_EXPANDED):
                LOG.warning(_LW("LUN %(name)s is already expanded. "
                                "Message: %(msg)s"),
                            {'name': name, 'msg': out})
            else:
                self._raise_cli_error(command_expand_lun, rc, out)

    def expand_lun_and_wait(self, name, new_size):
        self.expand_lun(name, new_size, poll=False)

        def lun_is_extented():
            data = self.get_lun_by_name(name, poll=False)
            return new_size == data[VNXLunProperties.LUN_CAPACITY.key]

        self._wait_for_a_condition(lun_is_extented)

    def rename_lun(self, lun_id, new_name, poll=False):
        command_lun_rename = ('lun', '-modify',
                              '-l', lun_id,
                              '-newName', new_name,
                              '-o')
        out, rc = self.command_execute(*command_lun_rename,
                                       poll=poll)
        if rc != 0:
            self._raise_cli_error(command_lun_rename, rc, out)

    def modify_lun_tiering_by_id(self, lun_id, tiering):
        """Modify the tiering policy of the LUN."""
        command_modify_lun = ['lun', '-modify', '-l', lun_id, '-o']
        self._modify_lun_tiering(command_modify_lun, tiering)

    def modify_lun_tiering_by_name(self, name, tiering):
        """This function used to modify a lun's tiering policy."""
        command_modify_lun = ['lun', '-modify', '-name', name, '-o']
        self._modify_lun_tiering(command_modify_lun, tiering)

    def _modify_lun_tiering(self, command_modify_lun, tiering):
        if tiering and tiering != 'none':
            command_modify_lun.extend(VNXTieringEnum.get_opt(tiering))
            out, rc = self.command_execute(*command_modify_lun)
            if rc != 0:
                self._raise_cli_error(command_modify_lun, rc, out)

    def create_consistencygroup(self, cg_name, members=None, poll=False):
        """create the consistency group."""
        command_create_cg = ('snap', '-group',
                             '-create',
                             '-name', cg_name,
                             '-allowSnapAutoDelete', 'no')
        if members:
            command_create_cg += ('-res', ','.join(map(six.text_type,
                                                       members)))

        out, rc = self.command_execute(*command_create_cg, poll=poll)
        if rc != 0:
            # Ignore the error if consistency group already exists
            if VNXError.has_error(out, VNXError.CG_EXISTED):
                LOG.warning(_LW('Consistency group %(name)s already '
                                'exists. Message: %(msg)s'),
                            {'name': cg_name, 'msg': out})
            else:
                self._raise_cli_error(command_create_cg, rc, out)

        self._wait_for_a_condition(self.get_consistency_group_by_name,
                                   cg_name=cg_name,
                                   interval=INTERVAL_5_SEC,
                                   ignorable_exception_arbiter=lambda ex:
                                   isinstance(ex, exception.EMCVnxCLICmdError))

    def get_consistency_group_by_name(self, cg_name):
        cmd = ('snap', '-group', '-list', '-id', cg_name)
        data = {
            'Name': None,
            'Luns': None,
            'State': None
        }
        out, rc = self.command_execute(*cmd)
        if rc == 0:
            cg_pat = r"Name:(.*)\n"\
                     r"Description:(.*)\n"\
                     r"Allow auto delete:(.*)\n"\
                     r"Member LUN ID\(s\):(.*)\n"\
                     r"State:(.*)\n"
            for m in re.finditer(cg_pat, out):
                data['Name'] = m.groups()[0].strip()
                data['State'] = m.groups()[4].strip()
                # Handle case when no lun in cg Member LUN ID(s):  None
                luns_of_cg = m.groups()[3].replace('None', '').strip()
                data['Luns'] = ([lun.strip() for lun in luns_of_cg.split(',')]
                                if luns_of_cg else [])
                LOG.debug("Found consistent group %s.", data['Name'])

        else:
            self._raise_cli_error(cmd, rc, out)
        return data

    def add_lun_to_consistency_group(self, cg_name, lun_id, poll=False):
        add_lun_to_cg_cmd = ('snap', '-group',
                             '-addmember', '-id',
                             cg_name, '-res', lun_id)

        out, rc = self.command_execute(*add_lun_to_cg_cmd, poll=poll)
        if rc != 0:
            LOG.error(_LE("Can not add the lun %(lun)s to consistency "
                          "group %(cg_name)s."), {'lun': lun_id,
                                                  'cg_name': cg_name})
            self._raise_cli_error(add_lun_to_cg_cmd, rc, out)

    def remove_luns_from_consistencygroup(self, cg_name, remove_ids,
                                          poll=False):
        """Removes LUN(s) from cg"""
        remove_luns_cmd = ('snap', '-group', '-rmmember',
                           '-id', cg_name,
                           '-res', ','.join(remove_ids))
        out, rc = self.command_execute(*remove_luns_cmd, poll=poll)
        if rc != 0:
            LOG.error(_LE("Can not remove LUNs %(luns)s in consistency "
                          "group %(cg_name)s."), {'luns': remove_ids,
                                                  'cg_name': cg_name})
            self._raise_cli_error(remove_luns_cmd, rc, out)

    def replace_luns_in_consistencygroup(self, cg_name, new_ids,
                                         poll=False):
        """Replaces LUN(s) with new_ids for cg"""
        replace_luns_cmd = ('snap', '-group', '-replmember',
                            '-id', cg_name,
                            '-res', ','.join(new_ids))
        out, rc = self.command_execute(*replace_luns_cmd, poll=poll)
        if rc != 0:
            LOG.error(_LE("Can not place new LUNs %(luns)s in consistency "
                          "group %(cg_name)s."), {'luns': new_ids,
                                                  'cg_name': cg_name})
            self._raise_cli_error(replace_luns_cmd, rc, out)

    def delete_consistencygroup(self, cg_name):
        delete_cg_cmd = ('-np', 'snap', '-group',
                         '-destroy', '-id', cg_name)
        out, rc = self.command_execute(*delete_cg_cmd)
        if rc != 0:
            # Ignore the error if CG doesn't exist
            if VNXError.has_error(out, VNXError.GENERAL_NOT_FOUND):
                LOG.warning(_LW("CG %(cg_name)s does not exist. "
                                "Message: %(msg)s"),
                            {'cg_name': cg_name, 'msg': out})
            elif VNXError.has_error(out, VNXError.CG_IS_DELETING):
                LOG.warning(_LW("CG %(cg_name)s is deleting. "
                                "Message: %(msg)s"),
                            {'cg_name': cg_name, 'msg': out})
            else:
                self._raise_cli_error(delete_cg_cmd, rc, out)
        else:
            LOG.info(_LI('Consistency group %s was deleted '
                         'successfully.'), cg_name)

    def create_cgsnapshot(self, cg_name, snap_name, keep_for=None):
        """Create a cgsnapshot (snap group)."""
        create_cg_snap_cmd = self._build_snapshot_cmd(
            cg_name, snap_name, True, keep_for)

        out, rc = self.command_execute(*create_cg_snap_cmd)
        if rc != 0:
            # Ignore the error if cgsnapshot already exists
            if VNXError.has_error(out, VNXError.CG_SNAP_NAME_EXISTED):
                LOG.warning(_LW('Cgsnapshot name %(name)s already '
                                'exists. Message: %(msg)s'),
                            {'name': snap_name, 'msg': out})
            else:
                self._raise_cli_error(create_cg_snap_cmd, rc, out)
        self._wait_for_a_condition(self.check_snapshot,
                                   snap_name=snap_name,
                                   interval=INTERVAL_30_SEC,
                                   ignorable_exception_arbiter=lambda ex:
                                   isinstance(ex, exception.EMCVnxCLICmdError))

    def check_snapshot(self, snap_name, poll=True):
        """check if a snapshot/cgsnapshot is existed."""
        cmd_get = ('snap', '-list', '-id', snap_name)
        out, rc = self.command_execute(*cmd_get)
        if rc == 0:
            return True
        else:
            self._raise_cli_error(cmd_get, rc, out)

    def delete_cgsnapshot(self, snap_name):
        """Delete a cgsnapshot (snap group)."""
        delete_cg_snap_cmd = ('-np', 'snap', '-destroy',
                              '-id', snap_name, '-o')

        out, rc = self.command_execute(*delete_cg_snap_cmd)
        if rc != 0:
            # Ignore the error if cgsnapshot does not exist.
            if VNXError.has_error(out, VNXError.GENERAL_NOT_FOUND):
                LOG.warning(_LW('Snapshot %(name)s for consistency group '
                                'does not exist. Message: %(msg)s'),
                            {'name': snap_name, 'msg': out})
            else:
                self._raise_cli_error(delete_cg_snap_cmd, rc, out)

    def create_snapshot(self, lun_id, name, keep_for=None):
        command_create_snapshot = self._build_snapshot_cmd(
            lun_id, name, False, keep_for)
        out, rc = self.command_execute(*command_create_snapshot,
                                       poll=False)
        if rc != 0:
            # Ignore the error that due to retry
            if VNXError.has_error(out, VNXError.SNAP_NAME_EXISTED):
                LOG.warning(_LW('Snapshot %(name)s already exists. '
                                'Message: %(msg)s'),
                            {'name': name, 'msg': out})
            else:
                self._raise_cli_error(command_create_snapshot, rc, out)

    def _build_snapshot_cmd(self, res_id, name,
                            is_cg=False, keep_for=None):
        command_create_snapshot = ('snap', '-create',
                                   '-res', res_id,
                                   '-name', name,
                                   '-allowReadWrite', 'yes')
        if is_cg:
            command_create_snapshot = ('-np', ) + command_create_snapshot
            command_create_snapshot += ('-resType', 'CG')
        if keep_for:
            command_create_snapshot += (
                '-keepFor', six.text_type(keep_for) + 'h')
        else:
            command_create_snapshot += (
                '-allowAutoDelete', 'no')
        return command_create_snapshot

    def copy_snapshot(self, src_snap_name, new_name):
        command_copy_snapshot = ('snap', '-copy',
                                 '-id', src_snap_name,
                                 '-name', new_name,
                                 '-ignoreMigrationCheck',
                                 '-ignoreDeduplicationCheck')

        out, rc = self.command_execute(*command_copy_snapshot)
        if rc != 0:
            # Ignore the error if the snap already exists
            if VNXError.has_error(out, VNXError.SNAP_NAME_EXISTED):
                LOG.warning(_LW('Snapshot %(name)s already exists. '
                                'Message: %(msg)s'),
                            {'name': new_name, 'msg': out})
            else:
                self._raise_cli_error(command_copy_snapshot, rc, out)

    def delete_snapshot(self, name):

        def delete_snapshot_success():
            command_delete_snapshot = ('snap', '-destroy',
                                       '-id', name,
                                       '-o')
            out, rc = self.command_execute(*command_delete_snapshot,
                                           poll=True)
            if rc != 0:
                # Ignore the error that due to retry
                if rc == 5 and out.find("not exist") >= 0:
                    LOG.warning(_LW("Snapshot %(name)s may deleted already. "
                                    "Message: %(msg)s"),
                                {'name': name, 'msg': out})
                    return True
                # The snapshot cannot be destroyed because it is
                # attached to a snapshot mount point. Wait
                elif VNXError.has_error(out, VNXError.SNAP_NAME_IN_USE):
                    LOG.warning(_LW("Snapshot %(name)s is in use, retry. "
                                    "Message: %(msg)s"),
                                {'name': name, 'msg': out})
                    return False
                else:
                    self._raise_cli_error(command_delete_snapshot, rc, out)
            else:
                LOG.info(_LI('Snapshot %s was deleted successfully.'),
                         name)
                return True

        self._wait_for_a_condition(delete_snapshot_success,
                                   interval=INTERVAL_30_SEC,
                                   timeout=INTERVAL_30_SEC * 3)

    def create_mount_point(self, primary_lun_name, name):

        command_create_mount_point = ('lun', '-create',
                                      '-type', 'snap',
                                      '-primaryLunName', primary_lun_name,
                                      '-name', name)

        out, rc = self.command_execute(*command_create_mount_point,
                                       poll=False)
        if rc != 0:
            # Ignore the error that due to retry
            if VNXError.has_error(out, VNXError.LUN_EXISTED):
                LOG.warning(_LW("Mount point %(name)s already exists. "
                                "Message: %(msg)s"),
                            {'name': name, 'msg': out})
            else:
                self._raise_cli_error(command_create_mount_point, rc, out)

        return rc

    def modify_snapshot(self, snap_name, allow_rw=True, keep_for=None):

        modify_cmd = ('snap', '-modify', '-id', snap_name,
                      '-allowReadWrite', 'yes'if allow_rw else 'no')
        if keep_for:
            modify_cmd += ('-keepFor', six.text_type(keep_for) + 'h')

        out, rc = self.command_execute(*modify_cmd)
        if rc != 0:
            self._raise_cli_error(modify_cmd, rc, out)

    def attach_mount_point(self, name, snapshot_name):

        command_attach_mount_point = ('lun', '-attach',
                                      '-name', name,
                                      '-snapName', snapshot_name)

        out, rc = self.command_execute(*command_attach_mount_point)
        if rc != 0:
            # Ignore the error that due to retry
            if VNXError.has_error(out, VNXError.SNAP_ALREADY_MOUNTED):
                LOG.warning(_LW("Snapshot %(snapname)s is attached to "
                                "snapshot mount point %(mpname)s already. "
                                "Message: %(msg)s"),
                            {'snapname': snapshot_name,
                             'mpname': name,
                             'msg': out})
            else:
                self._raise_cli_error(command_attach_mount_point, rc, out)

        return rc

    def detach_mount_point(self, smp_name):

        command_detach_mount_point = ('lun', '-detach',
                                      '-name', smp_name, '-o')

        out, rc = self.command_execute(*command_detach_mount_point)
        if rc != 0:
            # Ignore the error that due to retry
            if VNXError.has_error(out, VNXError.SNAP_NOT_ATTACHED):
                LOG.warning(_LW("The specified Snapshot mount point %s is not "
                                "currently attached."), smp_name)
            else:
                self._raise_cli_error(command_detach_mount_point, rc, out)

        return rc

    def migrate_lun(self, src_id, dst_id, rate=VNXMigrationRate.HIGH):
        command_migrate_lun = ('migrate', '-start',
                               '-source', src_id,
                               '-dest', dst_id,
                               '-rate', rate,
                               '-o')
        # SP HA is not supported by LUN migration
        out, rc = self.command_execute(*command_migrate_lun,
                                       retry_disable=True,
                                       poll=True)

        if 0 != rc:
            self._raise_cli_error(command_migrate_lun, rc, out)

        return rc

    def migrate_lun_without_verification(self, src_id, dst_id,
                                         dst_name=None,
                                         rate=VNXMigrationRate.HIGH):
        try:
            self.migrate_lun(src_id, dst_id, rate)
            return True
        except exception.EMCVnxCLICmdError as ex:
            migration_succeed = False
            orig_out = "\n".join(ex.kwargs["out"])
            if self._is_sp_unavailable_error(orig_out):
                LOG.warning(_LW("Migration command may get network timeout. "
                                "Double check whether migration in fact "
                                "started successfully. Message: %(msg)s"),
                            {'msg': ex.kwargs["out"]})
                command_migrate_list = ('migrate', '-list',
                                        '-source', src_id)
                rc = self.command_execute(*command_migrate_list,
                                          poll=True)[1]
                if rc == 0:
                    migration_succeed = True

            if not migration_succeed:
                LOG.warning(_LW("Start migration failed. Message: %s"),
                            ex.kwargs["out"])
                if dst_name is not None:
                    LOG.warning(_LW("Delete temp LUN after migration "
                                    "start failed. LUN: %s"), dst_name)
                    self.delete_lun(dst_name)
                return False
            else:
                return True

    def verify_lun_migration(self, src_id):
        # Set the proper interval to verify the migration status
        def migration_is_ready(poll=False):
            mig_ready = False
            cmd_migrate_list = ('migrate', '-list', '-source', src_id)
            out, rc = self.command_execute(*cmd_migrate_list,
                                           poll=poll)
            LOG.debug("Migration output: %s", out)
            if rc == 0:
                # parse the percentage
                state = re.search(r'Current State:\s*([^\n]+)', out)
                percentage = re.search(r'Percent Complete:\s*([^\n]+)', out)
                percentage_complete = 'N/A'
                current_state = 'N/A'
                if state is not None:
                    current_state = state.group(1)
                    percentage_complete = percentage.group(1)
                else:
                    self._raise_cli_error(cmd_migrate_list, rc, out)
                if ("FAULTED" in current_state or
                        "STOPPED" in current_state):
                    reason = _("Migration of LUN %s has been stopped or"
                               " faulted.") % src_id
                    raise exception.VolumeBackendAPIException(data=reason)
                if ("TRANSITIONING" in current_state or
                        "MIGRATING" in current_state):
                    LOG.debug("Migration of LUN %(src_id)s in process "
                              "%(percentage)s %%.",
                              {"src_id": src_id,
                               "percentage": percentage_complete})
            else:
                if VNXError.has_error(out, VNXError.LUN_NOT_MIGRATING):
                    LOG.debug("Migration of LUN %s is finished.", src_id)
                    mig_ready = True
                else:
                    self._raise_cli_error(cmd_migrate_list, rc, out)
            return mig_ready

        try:
            if migration_is_ready(True):
                return True
            self._wait_for_a_condition(
                migration_is_ready,
                interval=INTERVAL_30_SEC,
                ignorable_exception_arbiter=lambda ex:
                type(ex) is not exception.VolumeBackendAPIException)
        # Migration cancellation for clean up
        except exception.VolumeBackendAPIException:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("Migration of LUN %s failed to complete."),
                          src_id)
                self.cancel_migration(src_id)

        return True

    def migration_disappeared(self, src_id, poll=False):
        cmd_migrate_list = ('migrate', '-list', '-source', src_id)
        out, rc = self.command_execute(*cmd_migrate_list,
                                       poll=poll)
        if rc != 0:
            if VNXError.has_error(out, VNXError.LUN_NOT_MIGRATING):
                LOG.debug("Migration of LUN %s is finished.", src_id)
                return True
            else:
                LOG.error(_LE("Failed to query migration status of LUN."),
                          src_id)
                self._raise_cli_error(cmd_migrate_list, rc, out)
        return False

    # Cancel migration in case where status is faulted or stopped
    def cancel_migration(self, src_id):
        LOG.info(_LI("Cancelling Migration from LUN %s."), src_id)
        cmd_migrate_cancel = ('migrate', '-cancel', '-source', src_id,
                              '-o')
        out, rc = self.command_execute(*cmd_migrate_cancel)
        if rc != 0:
            self._raise_cli_error(cmd_migrate_cancel, rc, out)
        self._wait_for_a_condition(self.migration_disappeared,
                                   interval=INTERVAL_30_SEC,
                                   src_id=src_id)

    def migrate_lun_with_verification(self, src_id,
                                      dst_id,
                                      dst_name=None,
                                      rate=VNXMigrationRate.HIGH):
        migration_started = self.migrate_lun_without_verification(
            src_id, dst_id, dst_name, rate)
        if not migration_started:
            return False

        eventlet.sleep(INTERVAL_30_SEC)
        return self.verify_lun_migration(src_id)

    def get_storage_group(self, name, poll=True):

        # ALU/HLU as key/value map
        lun_map = {}

        data = {'storage_group_name': name,
                'storage_group_uid': None,
                'lunmap': lun_map,
                'raw_output': ''}

        command_get_storage_group = ('storagegroup', '-list',
                                     '-gname', name, '-host',
                                     '-iscsiAttributes')

        out, rc = self.command_execute(*command_get_storage_group,
                                       poll=poll)
        if rc != 0:
            self._raise_cli_error(command_get_storage_group, rc, out)

        data['raw_output'] = out
        re_storage_group_id = 'Storage Group UID:\s*(.*)\s*'
        m = re.search(re_storage_group_id, out)
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

    def create_storage_group(self, name):

        command_create_storage_group = ('storagegroup', '-create',
                                        '-gname', name)

        out, rc = self.command_execute(*command_create_storage_group)
        if rc != 0:
            # Ignore the error that due to retry
            if VNXError.has_error(out, VNXError.SG_NAME_IN_USE):
                LOG.warning(_LW('Storage group %(name)s already exists. '
                                'Message: %(msg)s'),
                            {'name': name, 'msg': out})
            else:
                self._raise_cli_error(command_create_storage_group, rc, out)

    def delete_storage_group(self, name):

        command_delete_storage_group = ('storagegroup', '-destroy',
                                        '-gname', name, '-o')

        out, rc = self.command_execute(*command_delete_storage_group)
        if rc != 0:
            # Ignore the error that due to retry
            if rc == 83 and out.find("group name or UID does not "
                                     "match any storage groups") >= 0:
                LOG.warning(_LW("Storage group %(name)s doesn't exist, "
                                "may have already been deleted. "
                                "Message: %(msg)s"),
                            {'name': name, 'msg': out})
            else:
                self._raise_cli_error(command_delete_storage_group, rc, out)

    def connect_host_to_storage_group(self, hostname, sg_name):

        command_host_connect = ('storagegroup', '-connecthost',
                                '-host', hostname,
                                '-gname', sg_name,
                                '-o')

        out, rc = self.command_execute(*command_host_connect)
        if rc != 0:
            self._raise_cli_error(command_host_connect, rc, out)

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
                LOG.warning(_LW("Host %(host)s has already disconnected from "
                                "storage group %(sgname)s. Message: %(msg)s"),
                            {'host': hostname, 'sgname': sg_name, 'msg': out})
            else:
                self._raise_cli_error(command_host_disconnect, rc, out)

    def add_hlu_to_storage_group(self, hlu, alu, sg_name):
        """Adds a lun into storage group as specified hlu number.

        Return True if the hlu is as specified, otherwise False.
        """

        command_add_hlu = ('storagegroup', '-addhlu',
                           '-hlu', hlu,
                           '-alu', alu,
                           '-gname', sg_name, '-o')

        out, rc = self.command_execute(*command_add_hlu, poll=False)
        if rc != 0:
            # Do not need to consider the retry for add hlu
            # Retry is handled in the caller
            self._raise_cli_error(command_add_hlu, rc, out)

        return True

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
                LOG.warning(_LW("HLU %(hlu)s has already been removed from "
                                "%(sgname)s. Message: %(msg)s"),
                            {'hlu': hlu, 'sgname': sg_name, 'msg': out})
            else:
                self._raise_cli_error(command_remove_hlu, rc, out)

    def get_lun_by_name(self,
                        name,
                        properties=VNXLunProperties.lun_all,
                        poll=True):
        data = self.get_lun_properties(('-name', name),
                                       properties,
                                       poll=poll)
        return data

    def get_lun_by_id(self,
                      lunid,
                      properties=VNXLunProperties.lun_all,
                      poll=True):
        data = self.get_lun_properties(('-l', lunid),
                                       properties, poll=poll)
        return data

    def get_lun_current_ops_state(self, name, poll=False):
        data = self.get_lun_by_name(name, poll=poll)
        return data[VNXLunProperties.LUN_OPERATION.key]

    def wait_until_lun_ready_for_ops(self, name):
        def is_lun_ready_for_ops():
            data = self.get_lun_current_ops_state(name, False)
            return data == 'None'
        # Get the volume's latest operation state by polling.
        # Otherwise, the operation state may be out of date.
        ops = self.get_lun_current_ops_state(name, True)
        if ops != 'None':
            self._wait_for_a_condition(is_lun_ready_for_ops)

    def get_pool(self,
                 name,
                 properties=VNXPoolProperties.pool_all,
                 poll=True):
        data = self.get_pool_properties(('-name', name),
                                        properties=properties,
                                        poll=poll)
        return data

    def get_pool_properties(self,
                            filter_option,
                            properties=VNXPoolProperties.pool_all,
                            poll=True):
        module_list = ('storagepool', '-list')
        data = self._get_obj_properties(
            module_list, filter_option,
            base_properties=(VNXPoolProperties.POOL_NAME,),
            adv_properties=tuple(properties),
            poll=poll)
        return data

    def get_lun_properties(self,
                           filter_option,
                           properties=VNXLunProperties.lun_all,
                           poll=True):
        module_list = ('lun', '-list')
        data = self._get_obj_properties(
            module_list, filter_option,
            base_properties=(VNXLunProperties.LUN_NAME,
                             VNXLunProperties.LUN_ID),
            adv_properties=tuple(properties),
            poll=poll)
        return data

    def get_pool_feature_properties(
            self,
            properties=VNXPoolFeatureProperties.default,
            poll=True):
        module_list = ("storagepool", '-feature', '-info')
        data = self._get_obj_properties(
            module_list, tuple(),
            base_properties=(),
            adv_properties=tuple(properties),
            poll=poll)
        return data

    def _get_obj_properties(self, module_list,
                            filter_option,
                            base_properties=tuple(),
                            adv_properties=tuple(),
                            poll=True):
        # to do instance check
        command_get = module_list + filter_option
        options = []
        for prop in adv_properties:
            option = prop.option
            if option and option not in options:
                options.append(option)
        command_get += tuple(options)
        out, rc = self.command_execute(*command_get, poll=poll)

        if rc != 0:
            self._raise_cli_error(command_get, rc, out)

        data = VNXCliParser.parse(out,
                                  list(base_properties) + list(adv_properties))
        LOG.debug('Return Object properties. Data: %s', data)
        return data

    def _get_property_value(self, out, propertyDescriptor):
        label = propertyDescriptor.label
        m = re.search(label, out)
        if m:
            if (propertyDescriptor.converter is not None):
                try:
                    converter = propertyDescriptor.converter
                    if isinstance(converter, staticmethod):
                        converter = converter.__func__
                    return converter(m.group(1))
                except ValueError:
                    LOG.error(_LE("Invalid value for %(key)s, "
                                  "value is %(value)s."),
                              {'key': propertyDescriptor.key,
                               'value': m.group(1)})
                    return None
            else:
                return m.group(1)
        else:
            LOG.debug('%s value is not found in the output.',
                      propertyDescriptor.label)
            return None

    def check_lun_has_snap(self, lun_id):
        cmd = ('snap', '-list', '-res', lun_id)
        rc = self.command_execute(*cmd, poll=False)[1]
        if rc == 0:
            LOG.debug("Found snapshots for %s.", lun_id)
            return True
        else:
            return False

    def get_pool_list(self,
                      properties=VNXPoolProperties.pool_all,
                      poll=True):
        temp_cache = []
        list_cmd = ('storagepool', '-list')
        for prop in properties:
            list_cmd += (prop.option,)
        output_properties = [VNXPoolProperties.POOL_NAME] + properties
        out, rc = self.command_execute(*list_cmd, poll=poll)
        if rc != 0:
            self._raise_cli_error(list_cmd, rc, out)

        try:
            for pool in out.strip().split('\n\n'):
                pool_data = VNXPoolProperties.parse(
                    pool, output_properties)
                temp_cache.append(pool_data)
        except Exception as ex:
            LOG.error(_LE("Error happened during storage pool querying, %s."),
                      ex)
            # NOTE: Do not want to continue raise the exception
            # as the pools may be temporarily unavailable
            pass
        return temp_cache

    def get_array_serial(self, poll=False):
        """return array Serial No for pool backend."""
        data = {'array_serial': 'unknown'}

        command_get_array_serial = ('getagent', '-serial')
        # Set the property timeout to get array serial
        out, rc = self.command_execute(*command_get_array_serial,
                                       poll=poll)
        if 0 == rc:
            m = re.search(r'Serial No:\s+(\w+)', out)
            if m:
                data['array_serial'] = m.group(1)
            else:
                LOG.warning(_LW("No array serial number returned, "
                                "set as unknown."))
        else:
            self._raise_cli_error(command_get_array_serial, rc, out)

        return data

    def get_status_up_ports(self, storage_group_name, io_ports=None,
                            poll=True):
        """Function to get ports whose status are up."""
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
                self._raise_cli_error(cmd_get_port, rc, out)
            for i, sp in enumerate(sps):
                if io_ports:  # Skip ports which are not in io_ports
                    if (sp.split()[1], int(portid[i])) not in io_ports:
                        continue
                wwn = self.get_port_wwn(sp, portid[i], out)
                if (wwn is not None) and (wwn not in wwns):
                    LOG.debug('Add wwn:%(wwn)s for sg:%(sg)s.',
                              {'wwn': wwn,
                               'sg': storage_group_name})
                    wwns.append(wwn)
        elif 83 == rc:
            LOG.warning(_LW("Storage Group %s is not found."),
                        storage_group_name)
        else:
            self._raise_cli_error(cmd_get_hba, rc, out)
        return wwns

    def get_login_ports(self, storage_group_name, connector_wwpns,
                        io_ports=None):
        cmd_list_hba = ('port', '-list', '-gname', storage_group_name)
        out, rc = self.command_execute(*cmd_list_hba)
        ports = []
        wwns = []
        connector_hba_list = []
        if 0 == rc and out.find('Information about each HBA:') != -1:
            hba_list = out.split('Information about each SPPORT:')[0].split(
                'Information about each HBA:')[1:]
            allports = out.split('Information about each SPPORT:')[1]
            hba_uid_pat = re.compile('HBA\sUID:\s*((\w\w:){15}(\w\w))')
            for each in hba_list:
                obj_search = re.search(hba_uid_pat, each)
                if obj_search and obj_search.group(1). \
                        replace(':', '')[16:].lower() in connector_wwpns:
                    connector_hba_list.append(each)
            port_pat = re.compile('SP Name:\s*(SP\s\w)\n\s*' +
                                  'SP Port ID:\s*(\w*)\n\s*' +
                                  'HBA Devicename:.*\n\s*' +
                                  'Trusted:.*\n\s*' +
                                  'Logged In:\s*YES\n')

            for each in connector_hba_list:
                ports.extend(re.findall(port_pat, each))
            ports = list(set(ports))
            if io_ports:
                ports = filter(lambda po:
                               (po[0].split()[1], int(po[1])) in io_ports,
                               ports)
            for each in ports:
                wwn = self.get_port_wwn(each[0], each[1], allports)
                if wwn:
                    wwns.append(wwn)
        else:
            self._raise_cli_error(cmd_list_hba, rc, out)
        return wwns

    def get_port_wwn(self, sp, port_id, allports=None):
        """Returns wwn via sp and port_id

        :param sp: should be in this format 'SP A'
        :param port_id: '0' or 0
        """
        wwn = None
        if allports is None:
            allports, rc = self.get_port_output()
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
        out, rc = self.get_port_output()
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

    def get_port_output(self):
        cmd_get_port = ('port', '-list', '-sp')
        out, rc = self.command_execute(*cmd_get_port)
        if 0 != rc:
            self._raise_cli_error(cmd_get_port, rc, out)
        return out, rc

    def get_connection_getport_output(self):
        connection_getport_cmd = ('connection', '-getport', '-vlanid')
        out, rc = self.command_execute(*connection_getport_cmd)
        if 0 != rc:
            self._raise_cli_error(connection_getport_cmd, rc, out)
        return out, rc

    def _filter_iscsi_ports(self, all_ports, io_ports):
        """Filter ports in white list from all iSCSI ports."""
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
                LOG.warning(_LW('Invalid iSCSI port %(sp)s-%(port)s-%(vlan)s '
                                'found in io_port_list, will be ignored.'),
                            {'sp': invalid[0], 'port': invalid[1],
                             'vlan': invalid[2]})
        return new_iscsi_ports

    def get_iscsi_targets(self, poll=False, io_ports=None):
        cmd_getport = ('connection', '-getport', '-address', '-vlanid')
        out, rc = self.command_execute(*cmd_getport, poll=poll)
        if rc != 0:
            self._raise_cli_error(cmd_getport, rc, out)

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
                    LOG.debug("Skip port without IP Address: %s",
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
        for m_spport in re.finditer(
                r'\n\s+%s\s+SP\s.*\n.*\n\s*SPPort:\s+(A|B)-(\d+)v(\d+)\s*\n'
                % initiator_iqn, sg_raw_out, flags=re.IGNORECASE):
            spport_set.add((m_spport.group(1), int(m_spport.group(2)),
                           int(m_spport.group(3))))
        LOG.debug('See path %(path)s in %(sg)s.',
                  {'path': spport_set,
                   'sg': sgname})
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
                LOG.debug("See available iSCSI target: %s",
                          connection_pingnode)
                return True
        LOG.warning(_LW("See unavailable iSCSI target: %s"),
                    connection_pingnode)
        return False

    def find_available_iscsi_targets(self, hostname,
                                     registered_spport_set,
                                     all_iscsi_targets):
        """Finds available iscsi targets for a host.

        When the iscsi_initiator_map is configured, the driver will find
        an accessible portal and put it as the first portal in the portal
        list to ensure the accessible portal will be used when multipath
        is not used. All the registered portals will be returned for Nova
        to clean up all the unused devices related to this LUN created by
        logging into these portals during attaching other LUNs on VNX.
        """

        if self.iscsi_initiator_map and hostname in self.iscsi_initiator_map:
            iscsi_initiator_ips = list(self.iscsi_initiator_map[hostname])
            random.shuffle(iscsi_initiator_ips)
        else:
            iscsi_initiator_ips = None

        target_portals = []

        all_portals = all_iscsi_targets['A'] + all_iscsi_targets['B']
        random.shuffle(all_portals)
        for portal in all_portals:
            spport = (portal['SP'],
                      portal['Port ID'],
                      portal['Virtual Port ID'])
            if spport not in registered_spport_set:
                LOG.debug(
                    "Skip SP Port %(port)s since "
                    "no path from %(host)s is through it.",
                    {'port': spport,
                     'host': hostname})
                continue
            target_portals.append(portal)

        main_portal_index = None
        if iscsi_initiator_ips:
            for i, portal in enumerate(target_portals):
                for initiator_ip in iscsi_initiator_ips:
                    if self.ping_node(portal, initiator_ip):
                        main_portal_index = i
                        break
                else:
                    # Else for the for loop. If there is no main portal found,
                    # continue to try next initiator IP.
                    continue
                break

        if main_portal_index is not None:
            target_portals.insert(0, target_portals.pop(main_portal_index))
        return target_portals

    def _is_sp_unavailable_error(self, out):
        error_pattern = ('(^Error.*Message.*End of data stream.*)|'
                         '(.*Message.*connection refused.*)|'
                         '(^Error.*Message.*Service Unavailable.*)|'
                         '(^A network error occurred while trying to'
                         ' connect.* )|'
                         '(^Exception: Error occurred because of time out\s*)')
        pattern = re.compile(error_pattern)
        return pattern.match(out)

    @utils.retry(exception.EMCSPUnavailableException, retries=5,
                 interval=30, backoff_rate=1)
    def command_execute(self, *command, **kwargs):
        """Executes command against the VNX array.

        When there is named parameter poll=False, the command will be sent
        alone with option -np.
        """
        # NOTE: retry_disable need to be removed from kwargs
        # before it pass to utils.execute, otherwise exception will thrown
        retry_disable = kwargs.pop('retry_disable', False)
        # get active ip before execute command
        current_ip = self.active_storage_ip
        out, rc = self._command_execute_on_active_ip(*command, **kwargs)
        if not retry_disable and self._is_sp_unavailable_error(out):
            # When active sp is unavailable, switch to another sp
            # and set it to active and force a poll
            if self._toggle_sp(current_ip):
                LOG.debug('EMC: Command Exception: %(rc)s %(result)s. '
                          'Retry on another SP.', {'rc': rc,
                                                   'result': out})
                # Raise exception for retry
                raise exception.EMCSPUnavailableException(
                    cmd=command, rc=rc, out=out.split('\n'))

        return out, rc

    def _command_execute_on_active_ip(self, *command, **kwargs):
        if "check_exit_code" not in kwargs:
            kwargs["check_exit_code"] = True
        rc = 0
        out = ""
        need_poll = kwargs.pop('poll', True)
        if "-np" not in command and not need_poll:
            command = ("-np",) + command

        try:
            active_ip = (self.active_storage_ip,)
            out, err = utils.execute(
                *(self.command
                  + active_ip
                  + self.credentials
                  + command),
                **kwargs)
        except processutils.ProcessExecutionError as pe:
            rc = pe.exit_code
            out = pe.stdout
            out = out.replace('\n', '\\n')

        LOG.debug('EMC: Command: %(command)s. Result: %(result)s.',
                  {'command': self.command + active_ip + command,
                   'result': out.replace('\n', '\\n')})

        return out, rc

    def _toggle_sp(self, current_ip):
        """Toggle the storage IP.

        :param current_ip: active ip before toggle
        :returns True or False: if toggle happens, return True, otherwise False

        """
        @lockutils.synchronized(
            'vnx-toggle-' + self.toggle_lock_name, 'vnx-toggle-', True)
        def inner():
            if self.secondary_storage_ip is None:
                return False
            self.active_storage_ip = (
                self.secondary_storage_ip
                if current_ip == self.primary_storage_ip
                else self.primary_storage_ip)

            LOG.info(_LI('Toggle san_ip from %(current)s to '
                         '%(new)s.'),
                     {'current': current_ip,
                      'new': self.active_storage_ip})
            return True
        return inner()

    def get_enablers_on_array(self, poll=False):
        """The function would get all the enablers installed on array."""
        enablers = []
        cmd_list = ('ndu', '-list')
        out, rc = self.command_execute(*cmd_list, poll=poll)

        if rc != 0:
            self._raise_cli_error(cmd_list, rc, out)
        else:
            enabler_pat = r'Name of the software package:\s*(\S+)\s*'
            for m in re.finditer(enabler_pat, out):
                enablers.append(m.groups()[0])

        LOG.debug('Enablers on array %s.', enablers)
        return enablers

    def enable_or_disable_compression_on_lun(self, volumename, compression):
        """The function will enable or disable the compression on lun."""
        lun_data = self.get_lun_by_name(volumename)

        command_compression_cmd = ('compression', '-' + compression,
                                   '-l', lun_data['lun_id'],
                                   '-ignoreThresholds', '-o')

        out, rc = self.command_execute(*command_compression_cmd)

        if 0 != rc:
            self._raise_cli_error(command_compression_cmd, rc, out)
        return rc, out

    def deregister_initiator(self, initiator_uid):
        """This function tries to deregister initiators on VNX."""
        command_deregister = ('port', '-removeHBA',
                              '-hbauid', initiator_uid,
                              '-o')
        out, rc = self.command_execute(*command_deregister)
        return rc, out


@decorate_all_methods(log_enter_exit)
class EMCVnxCliBase(object):
    """This class defines the functions to use the native CLI functionality."""

    VERSION = '07.01.01'
    stats = {'driver_version': VERSION,
             'storage_protocol': None,
             'vendor_name': 'EMC',
             'volume_backend_name': None,
             'compression_support': 'False',
             'fast_support': 'False',
             'deduplication_support': 'False',
             'thin_provisioning_support': False,
             'thick_provisioning_support': True}
    REPLICATION_KEYS = ['san_ip', 'san_login', 'san_password',
                        'san_secondary_ip',
                        'storage_vnx_authentication_type',
                        'storage_vnx_security_file_dir']
    enablers = []
    tmp_snap_prefix = 'tmp-snap-'
    tmp_smp_for_backup_prefix = 'tmp-smp-'
    snap_as_vol_prefix = 'snap-as-vol-'

    def __init__(self, prtcl, configuration=None, active_backend_id=None):
        self.protocol = prtcl
        self.configuration = configuration
        self.max_luns_per_sg = self.configuration.max_luns_per_storage_group
        self.destroy_empty_sg = self.configuration.destroy_empty_storage_group
        self.itor_auto_reg = self.configuration.initiator_auto_registration
        self.itor_auto_dereg = self.configuration.initiator_auto_deregistration
        self.check_max_pool_luns_threshold = (
            self.configuration.check_max_pool_luns_threshold)
        # if zoning_mode is fabric, use lookup service to build itor_tgt_map
        self.zonemanager_lookup_service = None
        zm_conf = config.Configuration(manager.volume_manager_opts)
        if (zm_conf.safe_get('zoning_mode') == 'fabric' or
                self.configuration.safe_get('zoning_mode') == 'fabric'):
            from cinder.zonemanager import fc_san_lookup_service as fc_service
            self.zonemanager_lookup_service = \
                fc_service.FCSanLookupService(configuration=configuration)
        self.max_retries = 5
        if self.destroy_empty_sg:
            LOG.warning(_LW("destroy_empty_storage_group: True. "
                            "Empty storage group will be deleted "
                            "after volume is detached."))
        if not self.itor_auto_reg:
            LOG.info(_LI("initiator_auto_registration: False. "
                         "Initiator auto registration is not enabled. "
                         "Please register initiator manually."))
        self.hlu_set = set(range(1, self.max_luns_per_sg + 1))
        self._client = self._build_client(active_backend_id)
        self._active_backend_id = active_backend_id
        # Create connection to the secondary storage device
        self._mirror = self._build_mirror_view()
        self.update_enabler_in_volume_stats()
        # Fail the driver if configuration is not correct
        if self._mirror:
            if '-MirrorView/S' not in self.enablers:
                no_enabler_err = _('MirrorView/S enabler is not installed.')
                raise exception.VolumeBackendAPIException(data=no_enabler_err)
        else:
            self._mirror = None

        conf_pools = self.configuration.safe_get("storage_vnx_pool_names")
        self.storage_pools = self._get_managed_storage_pools(conf_pools)
        self.array_serial = None
        self.io_ports = self._parse_ports(self.configuration.io_port_list,
                                          self.protocol)
        if self.protocol == 'iSCSI':
            self.iscsi_targets = self._client.get_iscsi_targets(
                poll=True, io_ports=self.io_ports)
        self.hlu_cache = {}
        self.force_delete_lun_in_sg = (
            self.configuration.force_delete_lun_in_storagegroup)
        if self.force_delete_lun_in_sg:
            LOG.warning(_LW("force_delete_lun_in_storagegroup=True"))

        self.max_over_subscription_ratio = (
            self.configuration.max_over_subscription_ratio)
        self.ignore_pool_full_threshold = (
            self.configuration.ignore_pool_full_threshold)
        if self.ignore_pool_full_threshold:
            LOG.warning(_LW("ignore_pool_full_threshold: True. "
                            "LUN creation will still be forced "
                            "even if the pool full threshold is exceeded."))
        self.reserved_percentage = self.configuration.reserved_percentage

    def _get_managed_storage_pools(self, pools):
        storage_pools = set()
        if pools:
            storage_pools = set([po.strip() for po in pools.split(",")])
            array_pools = self._client.get_pool_list(
                [VNXPoolProperties.POOL_STATE], False)
            array_pools = set([po['pool_name'] for po in array_pools])
            un_exist_pools = storage_pools.difference(array_pools)
            storage_pools.difference_update(un_exist_pools)
            if not storage_pools:
                msg = _("All the specified storage pools to be managed "
                        "do not exist. Please check your configuration. "
                        "Non-existent pools: %s") % ",".join(un_exist_pools)
                raise exception.VolumeBackendAPIException(data=msg)
            if un_exist_pools:
                LOG.warning(_LW("The following specified storage pools "
                                "do not exist: %(unexist)s. "
                                "This host will only manage the storage "
                                "pools: %(exist)s"),
                            {'unexist': ",".join(un_exist_pools),
                             'exist': ",".join(storage_pools)})
            else:
                LOG.debug("This host will manage the storage pools: %s.",
                          ",".join(storage_pools))
        else:
            LOG.debug("No storage pool is configured. This host will "
                      "manage all the pools on the VNX system.")
        return storage_pools

    def _parse_ports(self, io_port_list, protocol):
        """Validates IO port format, supported format is a-1, b-3, a-3-0."""
        if not io_port_list or io_port_list == '*':
            return None
        ports = re.split('\s*,\s*', io_port_list)
        valid_ports = []
        invalid_ports = []
        if 'iSCSI' == protocol:
            out, rc = self._client.get_connection_getport_output()
            for port in ports:
                port_tuple = port.split('-')
                if (re.match('[abAB]-\d+-\d+$', port) and
                        self._validate_iscsi_port(
                            port_tuple[0], port_tuple[1], port_tuple[2], out)):
                    valid_ports.append(
                        (port_tuple[0].upper(), int(port_tuple[1]),
                         int(port_tuple[2])))
                else:
                    invalid_ports.append(port)
        elif 'FC' == protocol:
            out, rc = self._client.get_port_output()
            for port in ports:
                port_tuple = port.split('-')
                if re.match('[abAB]-\d+$', port) and self._validate_fc_port(
                        port_tuple[0], port_tuple[1], out):
                    valid_ports.append(
                        (port_tuple[0].upper(), int(port_tuple[1])))
                else:
                    invalid_ports.append(port)
        if len(invalid_ports) > 0:
            msg = _('Invalid %(protocol)s ports %(port)s specified '
                    'for io_port_list.') % {'protocol': self.protocol,
                                            'port': ','.join(invalid_ports)}
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        return valid_ports

    def _validate_iscsi_port(self, sp, port_id, vport_id, cmd_output):
        """Validates whether the iSCSI port is existed on VNX."""
        sp_port_pattern = (r'SP:\s+%(sp)s\nPort ID:\s+%(port_id)s\n' %
                           {'sp': sp.upper(), 'port_id': port_id})
        sp_port_fields = re.split(sp_port_pattern, cmd_output)
        if len(sp_port_fields) < 2:
            return False
        sp_port_info = re.split('SP:\s+(A|B)', sp_port_fields[1])[0]
        vport_pattern = '\nVirtual Port ID:\s+%s\nVLAN ID:' % vport_id
        return re.search(vport_pattern, sp_port_info) is not None

    def _validate_fc_port(self, sp, port_id, cmd_output):
        """Validates whether the FC port is existed on VNX"""
        fc_pattern = ('SP Name:\s*SP\s*' + sp.upper() +
                      '\nSP Port ID:\s*' + str(port_id) +
                      '\nSP UID:\s*((\w\w:){15}(\w\w))')
        return re.search(fc_pattern, cmd_output)

    def get_array_serial(self):
        if not self.array_serial:
            self.array_serial = self._client.get_array_serial()
        return self.array_serial['array_serial']

    def _construct_store_spec(self, volume, snapshot):
        if snapshot['cgsnapshot_id']:
            # Snapshot is part of cg snapshot
            snapshot_name = snapshot['cgsnapshot_id']
        else:
            snapshot_name = snapshot['name']
        new_snap_name = snapshot_name
        if self._is_snapcopy_enabled(volume):
            new_snap_name = self._construct_snap_name(volume)
        temp_snap = False
        if not self._is_snapcopy_enabled(volume):
            # snapshot should be expired after some time
            temp_snap = True
        async_migrate = False
        if self._is_async_migration(volume):
            async_migrate = True
        pool_name = self.get_target_storagepool(volume, snapshot['volume'])
        volume_name = volume['name']
        volume_size = snapshot['volume_size']
        dest_volume_name = volume_name + '_dest'
        specs = self.get_volumetype_extraspecs(volume)
        provisioning, tiering = self._get_extra_spec_value(specs)
        store_spec = {
            'volume': volume,
            'src_snap_name': snapshot_name,
            'new_snap_name': new_snap_name,
            'dest_vol_name': dest_volume_name,
            'pool_name': pool_name,
            'provisioning': provisioning,
            'tiering': tiering,
            'volume_size': volume_size,
            'client': self._client,
            'ignore_pool_full_threshold': self.ignore_pool_full_threshold,
            'temp_snap': temp_snap,
            'async_migrate': async_migrate,
        }
        return store_spec

    def _construct_snap_name(self, volume):
        """Returns snapshot or cgsnapshot name."""
        if self._is_snapcopy_enabled(volume):
            return self.snap_as_vol_prefix + six.text_type(volume['name_id'])
        else:
            return self.tmp_snap_prefix + six.text_type(volume['name_id'])

    def _construct_tmp_smp_name(self, snapshot):
        return self.tmp_smp_for_backup_prefix + snapshot.id

    def _construct_mirror_name(self, volume):
        return 'mirror_' + volume.id

    def create_volume(self, volume):
        """Creates a EMC volume."""
        volume_size = volume['size']
        volume_name = volume['name']

        self._volume_creation_check(volume)
        volume_metadata = self._get_volume_metadata(volume)
        # defining CLI command
        specs = self.get_volumetype_extraspecs(volume)
        pool = self.get_target_storagepool(volume)
        provisioning, tiering = self._get_extra_spec_value(specs)
        if 'snapcopy' in volume_metadata:
            # We ignore snapcopy metadata when creating volume
            LOG.warning(_LW('snapcopy metadata is ignored when'
                            ' creating volume.'))

        volume_metadata['snapcopy'] = 'False'
        LOG.info(_LI('Create Volume: %(volume)s  Size: %(size)s '
                     'pool: %(pool)s '
                     'provisioning: %(provisioning)s '
                     'tiering: %(tiering)s '),
                 {'volume': volume_name,
                  'size': volume_size,
                  'pool': pool,
                  'provisioning': provisioning,
                  'tiering': tiering})

        data = self._client.create_lun_with_advance_feature(
            pool, volume_name, volume_size,
            provisioning, tiering, volume['consistencygroup_id'],
            ignore_thresholds=self.ignore_pool_full_threshold,
            poll=False)
        pl = self._build_provider_location(lun_id=data['lun_id'],
                                           base_lun_name=volume['name'])
        # Setup LUN Replication/MirrorView between devices,
        # secondary LUN will inherit properties from primary LUN.
        rep_update, metadata_update = self.setup_lun_replication(
            volume, data['lun_id'], provisioning, tiering)
        volume_metadata.update(metadata_update)
        model_update = {'provider_location': pl,
                        'metadata': volume_metadata}
        model_update.update(rep_update)
        return model_update

    def _volume_creation_check(self, volume):
        """Checks on extra spec before the volume can be created."""
        specs = self.get_volumetype_extraspecs(volume)
        self._get_and_validate_extra_specs(specs)

    def _get_migration_rate(self, volume):
        metadata = self._get_volume_metadata(volume)
        rate = metadata.get('migrate_rate', VNXMigrationRate.HIGH)
        if rate:
            if rate.lower() in VNXMigrationRate.get_all():
                return rate.lower()
            else:
                LOG.warning(_LW('Unknown migration rate specified, '
                                'using [high] as migration rate.'))

        return VNXMigrationRate.HIGH

    def _get_and_validate_extra_specs(self, specs):
        """Checks on extra specs combinations."""
        if "storagetype:pool" in specs:
            LOG.warning(_LW("Extra spec key 'storagetype:pool' is obsoleted "
                            "since driver version 5.1.0. This key will be "
                            "ignored."))

        provisioning, tiering = self._get_extra_spec_value(specs)
        # step 1: check extra spec value
        if provisioning:
            self._check_extra_spec_value(
                provisioning,
                VNXProvisionEnum.get_all())
        if tiering:
            self._check_extra_spec_value(
                tiering,
                VNXTieringEnum.get_all())

        # step 2: check extra spec combination
        self._check_extra_spec_combination([provisioning, tiering])
        return provisioning, tiering

    def _check_extra_spec_value(self, extra_spec, valid_values):
        """Checks whether an extra spec's value is valid."""

        if not extra_spec or not valid_values:
            LOG.error(_LE('The given extra_spec or valid_values is None.'))
        elif extra_spec not in valid_values:
            msg = _("The extra_spec: %s is invalid.") % extra_spec
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        return

    def _get_extra_spec_value(self, extra_specs):
        """Gets EMC extra spec values."""
        provisioning = 'thick'
        if self._client.provisioning_specs[0] in extra_specs:
            provisioning = (
                extra_specs[self._client.provisioning_specs[0]].lower())
            if self._client.provisioning_specs[1] in extra_specs:
                LOG.warning(_LW("Both 'storagetype:prvosioning' and "
                                "'provisioning:type' are set in the "
                                "extra specs, the value of "
                                "'provisioning:type' will be used. The "
                                "key 'storagetype:provisioning' may be "
                                "deprecated in the next release."))
        elif self._client.provisioning_specs[1] in extra_specs:
            provisioning = (
                extra_specs[self._client.provisioning_specs[1]].lower())
            LOG.warning(_LW("Extra spec key 'storagetype:provisioning' may "
                            "be deprecated in the next release. It is "
                            "recommended to use extra spec key "
                            "'provisioning:type' instead."))
        tiering = extra_specs.get(
            self._client.tiering_spec, 'None').lower()

        return provisioning, tiering

    def _check_extra_spec_combination(self, spec_values):
        """Checks whether extra spec combination is valid."""
        enablers = self.enablers
        # check provisioning, tiering
        # deduplicated and tiering can not be both enabled
        provisioning, tiering = spec_values
        if provisioning == 'deduplicated' and tiering != 'none':
            msg = _("deduplicated and auto tiering can't be both enabled.")
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        elif provisioning == 'compressed' and '-Compression' not in enablers:
            msg = _("Compression Enabler is not installed. "
                    "Can not create compressed volume.")
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        elif provisioning == 'deduplicated' and \
                '-Deduplication' not in enablers:
            msg = _("Deduplication Enabler is not installed."
                    " Can not create deduplicated volume")
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        elif provisioning in ['thin', 'deduplicated', 'compressed'] and \
                '-ThinProvisioning' not in enablers:
            msg = _("ThinProvisioning Enabler is not installed. "
                    "Can not create thin volume")
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        elif tiering != 'none' and '-FAST' not in enablers:
            msg = _("FAST VP Enabler is not installed. "
                    "Can't set tiering policy for the volume")
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        return

    def delete_volume(self, volume, force_delete=False):
        """Deletes an EMC volume."""
        if self._is_replication_enabled(volume):
            self.cleanup_lun_replication(volume)

        def _delete_lun(volume, force=False):
            # For LUN/SMP which is still in storage group
            try:
                self._client.delete_lun(volume['name'])
            except EMCLunInSG:
                if force:
                    LOG.warning(_LW('LUN corresponding to %s is still '
                                    'in some Storage Groups.'
                                    'Try to bring the LUN out of Storage '
                                    'Groups and retry the deletion.'),
                                volume['name'])
                    lun_id = self.get_lun_id(volume)
                    self._client.remove_lun_from_sg(volume['name'], lun_id)
                    self._client.delete_lun(volume['name'])
                else:
                    raise
        try:
            _delete_lun(volume, self.force_delete_lun_in_sg)
        except EMCLunUsedByFeature:
            if self._is_async_migration(volume):
                lun_id = self.get_lun_id(volume)
                self._client.cancel_migration(lun_id)
                self._client.detach_mount_point(volume['name'])
                _delete_lun(volume, self.force_delete_lun_in_sg)
            else:
                raise

        if volume['provider_location']:
            lun_type = self.extract_provider_location(
                volume['provider_location'], 'type')
            if lun_type == 'smp':
                self._client.delete_snapshot(
                    self._construct_snap_name(volume))
        if self._is_async_migration(volume):
            self._client.delete_snapshot(self._construct_snap_name(volume))

    def extend_volume(self, volume, new_size):
        """Extends an EMC volume."""

        try:
            self._client.expand_lun_and_wait(volume['name'], new_size)
        except exception.EMCVnxCLICmdError as ex:
            with excutils.save_and_reraise_exception(ex) as ctxt:
                out = "\n".join(ex.kwargs["out"])
                if VNXError.has_error(out, VNXError.LUN_IS_PREPARING):
                    # The error means the operation cannot be performed
                    # because the LUN is 'Preparing'. Wait for a while
                    # so that the LUN may get out of the transitioning
                    # state.
                    LOG.warning(_LW("LUN %(name)s is not ready for extension: "
                                    "%(out)s"),
                                {'name': volume['name'], 'out': out})
                    self._client.wait_until_lun_ready_for_ops(volume['name'])
                    self._client.expand_lun_and_wait(volume['name'], new_size)
                    ctxt.reraise = False
                else:
                    ctxt.reraise = True

    def _get_original_status(self, volume):
        if not volume['volume_attachment']:
            return 'available'
        else:
            return 'in-use'

    def _is_valid_for_storage_assisted_migration(
            self, volume, host, new_type=None):
        """Check the src and dest volume to decide the migration type."""
        false_ret = (False, None)

        if 'location_info' not in host['capabilities']:
            LOG.warning(_LW("Failed to get target_pool_name and "
                            "target_array_serial. 'location_info' "
                            "is not in host['capabilities']."))
            return false_ret

        # mandatory info should be ok
        info = host['capabilities']['location_info']
        LOG.debug("Host for migration is %s.", info)
        try:
            info_detail = info.split('|')
            target_pool_name = info_detail[0]
            target_array_serial = info_detail[1]
        except AttributeError:
            LOG.warning(_LW("Error on parsing target_pool_name/"
                            "target_array_serial."))
            return false_ret

        # source and destination should be on same array
        array_serial = self.get_array_serial()
        if target_array_serial != array_serial:
            LOG.debug('Skip storage-assisted migration because '
                      'target and source backend are not managing'
                      'the same array.')
            return false_ret

        if len(target_pool_name) == 0:
            # Destination host is using a legacy driver
            LOG.warning(_LW("Didn't get the pool information of the "
                            "host %s. Storage assisted Migration is not "
                            "supported. The host may be using a legacy "
                            "driver."),
                        host['name'])
            return false_ret

        # Same protocol should be used if volume is in-use
        if host['capabilities']['storage_protocol'] != self.protocol \
                and self._get_original_status(volume) == 'in-use':
            LOG.debug('Skip storage-assisted migration because '
                      'in-use volume can not be '
                      'migrate between different protocols.')
            return false_ret

        return (True, target_pool_name)

    def migrate_volume(self, ctxt, volume, host, new_type=None):
        """Leverage the VNX on-array migration functionality.

        This method is invoked at the source backend.
        """
        false_ret = (False, None)
        is_valid, target_pool_name = \
            self._is_valid_for_storage_assisted_migration(
                volume, host, new_type)
        if not is_valid:
            return false_ret

        specs = None
        if new_type is not None:
            specs = new_type.get('extra_specs')

        return self._migrate_volume(volume, target_pool_name, specs)

    def _migrate_volume(self,
                        volume,
                        target_pool_name,
                        type_specs=None,
                        src_id=None):
        LOG.debug("Starting real storage-assisted migration...")
        # first create a new volume with same name and size of source volume
        volume_name = volume['name']
        new_volume_name = "%(src)s-%(ts)s" % {'src': volume_name,
                                              'ts': int(time.time())}

        if src_id is None:
            src_id = self.get_lun_id(volume)

        if type_specs is not None:
            provisioning, tiering = self._get_extra_spec_value(
                type_specs)
        else:
            provisioning, tiering = self._get_extra_spec_value(
                self.get_volumetype_extraspecs(volume))

        data = self._client.create_lun_with_advance_feature(
            target_pool_name, new_volume_name, volume['size'],
            provisioning, tiering,
            ignore_thresholds=self.ignore_pool_full_threshold)

        dst_id = data['lun_id']
        moved = self._client.migrate_lun_with_verification(
            src_id, dst_id, new_volume_name,
            rate=self._get_migration_rate(volume))

        lun_type = self.extract_provider_location(
            volume['provider_location'], 'type')
        # A smp will become a LUN after migration
        if lun_type == 'smp':
            self._client.delete_snapshot(
                self._construct_snap_name(volume))

        pl = self._build_provider_location(src_id, 'lun',
                                           base_lun_name=volume['name'])
        volume_metadata = self._get_volume_metadata(volume)
        volume_metadata['snapcopy'] = 'False'
        model_update = {'provider_location': pl,
                        'metadata': volume_metadata}
        return moved, model_update

    def update_migrated_volume(self, context, volume, new_volume,
                               original_volume_status):
        """Updates metadata after host-assisted migration."""

        lun_type = self.extract_provider_location(
            new_volume['provider_location'], 'type')
        volume_metadata = self._get_volume_metadata(volume)
        model_update = {'provider_location':
                        new_volume['provider_location']}
        if lun_type:
            volume_metadata['snapcopy'] = (
                'True' if lun_type == 'smp' else 'False')
            model_update['metadata'] = volume_metadata
        return model_update

    def retype(self, ctxt, volume, new_type, diff, host):
        new_specs = new_type['extra_specs']

        new_provisioning, new_tiering = (
            self._get_and_validate_extra_specs(new_specs))

        # Check what changes are needed
        changes = self.determine_changes_when_retype(volume, new_type, host)

        if self._client.check_lun_has_snap(self.get_lun_id(volume)):
            # Reject if volume has snapshot when migration is needed
            if changes['migration']:
                LOG.debug('Driver is not able to do retype because the volume '
                          '%s has a snapshot which is forbidden to migrate.',
                          volume['id'])
                return False
            # Reject if volume has snapshot when trying to
            # turn on compression
            if changes['compression_on']:
                LOG.debug('Driver is not able to do retype because the volume '
                          '%s has a snapshot which is forbidden to turn on '
                          'compression.', volume['id'])
                return False

        if changes['migration']:
            # Check whether the migration is valid
            is_valid, target_pool_name = (
                self._is_valid_for_storage_assisted_migration(
                    volume, host, new_type))
            if is_valid:
                specs = None
                if new_type is not None:
                    specs = new_type.get('extra_specs')
                moved, model_update = self._migrate_volume(
                    volume, target_pool_name, specs)
                if moved:
                    return moved, model_update
                else:
                    LOG.warning(_LW('Storage-assisted migration failed during '
                                    'retype.'))
                    return False
            else:
                # Migration is invalid
                LOG.debug('Driver is not able to do retype due to '
                          'storage-assisted migration is not valid '
                          'in this situation.')
                return False
        if changes['compression_on']:
            # Turn on compression feature on the volume
            self._client.enable_or_disable_compression_on_lun(
                volume['name'], 'on')
        if changes['tiering']:
            # Modify lun to change tiering policy
            self._client.modify_lun_tiering_by_name(volume['name'],
                                                    new_tiering)
        return True

    def determine_changes_when_retype(self, volume, new_type, host):
        changes = {
            'migration': False,
            'tiering': False,
            'compression_on': False
        }

        old_specs = self.get_volumetype_extraspecs(volume)
        old_provisioning, old_tiering = (
            self._get_extra_spec_value(old_specs))

        new_specs = new_type['extra_specs']
        new_provisioning, new_tiering = (
            self._get_extra_spec_value(new_specs))

        lun_type = self.extract_provider_location(
            volume['provider_location'], 'type')

        if volume['host'] != host['host']:
            changes['migration'] = True
        elif old_provisioning != new_provisioning:
            if (old_provisioning in ['thin', 'thick'] and
                    new_provisioning == 'compressed'):
                changes['compression_on'] = True
            else:
                changes['migration'] = True
        if lun_type == 'smp':
            changes['migration'] = True

        if new_tiering != old_tiering:
            changes['tiering'] = True
        return changes

    def determine_all_enablers_exist(self, enablers):
        """Determine all wanted enablers whether exist."""
        wanted = ['-ThinProvisioning',
                  '-Deduplication',
                  '-FAST',
                  '-Compression']
        for each in wanted:
            if each not in enablers:
                return False
        return True

    def _build_pool_stats(self, pool, pool_feature=None):
        pool_stats = {
            'pool_name': pool['pool_name'],
            'total_capacity_gb': pool['total_capacity_gb'],
            'provisioned_capacity_gb': (pool['provisioned_capacity_gb'])
        }

        # Handle pool state Initializing, Ready, Faulted, Offline or Deleting.
        if pool['state'] in ('Initializing', 'Offline', 'Deleting'):
            pool_stats['free_capacity_gb'] = 0
            LOG.warning(_LW("Storage Pool '%(pool)s' is '%(state)s'."),
                        {'pool': pool_stats['pool_name'],
                         'state': pool['state']})
        else:
            pool_stats['free_capacity_gb'] = pool['free_capacity_gb']
            if self.check_max_pool_luns_threshold:
                pool_feature = self._client.get_pool_feature_properties(
                    poll=False) if not pool_feature else pool_feature
                if (pool_feature['max_pool_luns'] <=
                        pool_feature['total_pool_luns']):
                    LOG.warning(_LW("Maximum number of Pool LUNs, %s, "
                                    "have been created. "
                                    "No more LUN creation can be done."),
                                pool_feature['max_pool_luns'])
                    pool_stats['free_capacity_gb'] = 0

        if not self.reserved_percentage:
            # Since the admin is not sure of what value is proper,
            # the driver will calculate the recommended value.

            # Some extra capacity will be used by meta data of pool LUNs.
            # The overhead is about LUN_Capacity * 0.02 + 3 GB
            # reserved_percentage will be used to make sure the scheduler
            # takes the overhead into consideration.
            # Assume that all the remaining capacity is to be used to create
            # a thick LUN, reserved_percentage is estimated as follows:
            reserved = (((0.02 * pool['free_capacity_gb'] + 3) /
                         (1.02 * pool['total_capacity_gb'])) * 100)
            # Take pool full threshold into consideration
            if not self.ignore_pool_full_threshold:
                reserved += 100 - pool['pool_full_threshold']
            pool_stats['reserved_percentage'] = int(math.ceil(min(reserved,
                                                                  100)))
        else:
            pool_stats['reserved_percentage'] = self.reserved_percentage

        array_serial = self.get_array_serial()
        pool_stats['location_info'] = ('%(pool_name)s|%(array_serial)s' %
                                       {'pool_name': pool['pool_name'],
                                        'array_serial': array_serial})
        # Check if this pool's fast_cache is enabled
        if 'fast_cache_enabled' not in pool:
            pool_stats['fast_cache_enabled'] = 'False'
        else:
            pool_stats['fast_cache_enabled'] = pool['fast_cache_enabled']

        # Copy advanced feature stats from backend stats
        pool_stats['compression_support'] = self.stats['compression_support']
        pool_stats['fast_support'] = self.stats['fast_support']
        pool_stats['deduplication_support'] = (
            self.stats['deduplication_support'])
        # Thin provisioning is supported on VNX pools only when
        # ThinProvisioning Enabler software is installed on VNX,
        # and thick provisioning is always supported on VNX pools.
        pool_stats['thin_provisioning_support'] = (
            self.stats['thin_provisioning_support'])
        pool_stats['thick_provisioning_support'] = True
        pool_stats['consistencygroup_support'] = (
            self.stats['consistencygroup_support'])
        pool_stats['max_over_subscription_ratio'] = (
            self.max_over_subscription_ratio)
        # Add replication V2 support
        targets = []
        if self._mirror:
            pool_stats['replication_enabled'] = True
            pool_stats['replication_count'] = 1
            pool_stats['replication_type'] = ['sync']
            for device in self.configuration.replication_device:
                targets.append(device['backend_id'])
        else:
            pool_stats['replication_enabled'] = False
        pool_stats['replication_targets'] = targets
        return pool_stats

    def update_enabler_in_volume_stats(self):
        """Updates the enabler information in stats."""
        if not self.determine_all_enablers_exist(self.enablers):
            self.enablers = self._client.get_enablers_on_array()

        self.stats['compression_support'] = (
            'True' if '-Compression' in self.enablers else 'False')

        self.stats['fast_support'] = (
            'True' if '-FAST' in self.enablers else 'False')

        self.stats['deduplication_support'] = (
            'True' if '-Deduplication' in self.enablers else 'False')

        self.stats['thin_provisioning_support'] = (
            True if '-ThinProvisioning' in self.enablers else False)

        self.stats['consistencygroup_support'] = (
            'True' if '-VNXSnapshots' in self.enablers else 'False')

        return self.stats

    def create_snapshot(self, snapshot):
        """Creates a snapshot."""

        snapshot_name = snapshot['name']
        volume_name = snapshot['volume_name']
        volume = snapshot['volume']
        LOG.info(_LI('Create snapshot: %(snapshot)s: volume: %(volume)s'),
                 {'snapshot': snapshot_name,
                  'volume': volume_name})
        lun_id = self.get_lun_id(volume)

        try:
            self._client.create_snapshot(lun_id, snapshot_name)
        except exception.EMCVnxCLICmdError as ex:
            with excutils.save_and_reraise_exception(ex) as ctxt:
                out = "\n".join(ex.kwargs["out"])
                if VNXError.has_error(out, VNXError.LUN_IS_PREPARING):
                    # The error means the operation cannot be performed
                    # because the LUN is 'Preparing'. Wait for a while
                    # so that the LUN may get out of the transitioning
                    # state.
                    LOG.warning(_LW("LUN %(name)s is not ready for snapshot: "
                                    "%(out)s"),
                                {'name': volume_name, 'out': out})
                    self._client.wait_until_lun_ready_for_ops(volume['name'])
                    self._client.create_snapshot(lun_id, snapshot_name)
                    ctxt.reraise = False
                else:
                    ctxt.reraise = True

    def delete_snapshot(self, snapshot):
        """Deletes a snapshot."""

        snapshot_name = snapshot['name']

        LOG.info(_LI('Delete Snapshot: %(snapshot)s'),
                 {'snapshot': snapshot_name})

        self._client.delete_snapshot(snapshot_name)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Constructs a work flow to create a volume from snapshot.

        This flow will do the following:

        1. Create a snap mount point (SMP) for the snapshot.
        2. Attach the snapshot to the SMP created in the first step.
        3. Create a temporary lun prepare for migration.
           (Skipped if snapcopy='true')
        4. Start a migration between the SMP and the temp lun.
           (Skipped if snapcopy='true')
        """
        self._volume_creation_check(volume)
        flow_name = 'create_volume_from_snapshot'
        base_lun_name = self._get_base_lun_name(snapshot.volume)
        work_flow = linear_flow.Flow(flow_name)
        store_spec = self._construct_store_spec(volume, snapshot)
        store_spec.update({'base_lun_name': base_lun_name})
        volume_metadata = self._get_volume_metadata(volume)
        rep_update = {}
        if self._is_snapcopy_enabled(volume):
            if self._is_replication_enabled(volume):
                err_msg = _("Unable to enable replication "
                            "and snapcopy at the same time.")
                raise exception.VolumeBackendAPIException(data=err_msg)
            work_flow.add(CopySnapshotTask(),
                          ModifySnapshotTask(),
                          CreateSMPTask(),
                          AttachSnapTask())
            flow_engine = taskflow.engines.load(work_flow,
                                                store=store_spec)
            flow_engine.run()
            new_lun_id = flow_engine.storage.fetch('new_smp_id')
            pl = self._build_provider_location(
                new_lun_id, 'smp', base_lun_name)
            volume_metadata['snapcopy'] = 'True'
        else:
            store_spec.update({'rate': self._get_migration_rate(volume)})

            if self._is_async_migration(volume):
                work_flow.add(CopySnapshotTask())
                work_flow.add(ModifySnapshotTask())
            work_flow.add(CreateSMPTask(),
                          AttachSnapTask(),
                          CreateDestLunTask(),
                          MigrateLunTask())
            flow_engine = taskflow.engines.load(work_flow,
                                                store=store_spec)
            flow_engine.run()
            new_lun_id = flow_engine.storage.fetch('new_lun_id')
            pl = self._build_provider_location(
                new_lun_id, 'lun', volume['name'])
            volume_metadata['snapcopy'] = 'False'
            # Setup LUN Replication/MirrorView between devices,
            # secondary LUN will inherit properties from primary LUN.
            rep_update, metadata_update = self.setup_lun_replication(
                volume, new_lun_id,
                store_spec['provisioning'],
                store_spec['tiering'])
            volume_metadata.update(metadata_update)
        model_update = {'provider_location': pl,
                        'metadata': volume_metadata}
        model_update.update(rep_update)
        volume_host = volume['host']
        host = vol_utils.extract_host(volume_host, 'backend')
        host_and_pool = vol_utils.append_host(host, store_spec['pool_name'])
        if volume_host != host_and_pool:
            model_update['host'] = host_and_pool

        return model_update

    def create_cloned_volume(self, volume, src_vref):
        """Creates a clone of the specified volume."""
        self._volume_creation_check(volume)
        base_lun_name = self._get_base_lun_name(src_vref)
        source_lun_id = self.get_lun_id(src_vref)
        volume_size = volume['size']
        source_volume_name = src_vref['name']
        consistencygroup_id = src_vref['consistencygroup_id']
        cgsnapshot_name = None
        if consistencygroup_id:
            cgsnapshot_name = self._construct_snap_name(volume)

        snapshot_name = self._construct_snap_name(volume)
        snapshot = {
            'name': snapshot_name,
            'volume_name': source_volume_name,
            'volume_size': volume_size,
            'volume': src_vref,
            'cgsnapshot_id': cgsnapshot_name,
            'consistencygroup_id': consistencygroup_id,
            'id': cgsnapshot_name
        }
        flow_name = 'create_cloned_volume'
        store_spec = self._construct_store_spec(volume, snapshot)
        work_flow = linear_flow.Flow(flow_name)
        store_spec.update({'snapshot': snapshot})
        store_spec.update({'source_lun_id': source_lun_id})
        store_spec.update({'base_lun_name': base_lun_name})
        volume_metadata = self._get_volume_metadata(volume)
        rep_update = {}
        if self._is_snapcopy_enabled(volume):
            # snapcopy feature enabled
            if self._is_replication_enabled(volume):
                err_msg = _("Unable to enable replication "
                            "and snapcopy at the same time.")
                raise exception.VolumeBackendAPIException(data=err_msg)
            work_flow.add(CreateSnapshotTask(),
                          CreateSMPTask(),
                          AttachSnapTask())
            flow_engine = taskflow.engines.load(work_flow,
                                                store=store_spec)
            flow_engine.run()
            new_lun_id = flow_engine.storage.fetch('new_smp_id')
            pl = self._build_provider_location(
                new_lun_id, 'smp', base_lun_name)
        else:
            # snapcopy feature disabled, need to migrate
            store_spec.update({'rate': self._get_migration_rate(volume)})
            work_flow.add(CreateSnapshotTask(),
                          CreateSMPTask(),
                          AttachSnapTask(),
                          CreateDestLunTask(),
                          MigrateLunTask())
            flow_engine = taskflow.engines.load(work_flow,
                                                store=store_spec)
            flow_engine.run()
            new_lun_id = flow_engine.storage.fetch('new_lun_id')
            # Delete temp Snapshot for sync migration
            if not self._is_async_migration(volume):
                if consistencygroup_id:
                    self._client.delete_cgsnapshot(snapshot['id'])
                else:
                    self.delete_snapshot(snapshot)
            # After migration, volume's base lun is itself
            pl = self._build_provider_location(
                new_lun_id, 'lun', volume['name'])
            volume_metadata['snapcopy'] = 'False'

            # Setup LUN Replication/MirrorView between devices,
            # secondary LUN will inherit properties from primary LUN.
            rep_update, metadata_update = self.setup_lun_replication(
                volume, new_lun_id,
                store_spec['provisioning'],
                store_spec['tiering'])
            volume_metadata.update(metadata_update)

        model_update = {'provider_location': pl,
                        'metadata': volume_metadata}
        model_update.update(rep_update)
        volume_host = volume['host']
        host = vol_utils.extract_host(volume_host, 'backend')
        host_and_pool = vol_utils.append_host(host, store_spec['pool_name'])
        if volume_host != host_and_pool:
            model_update['host'] = host_and_pool

        return model_update

    @staticmethod
    def _get_volume_metadata(volume):
        # Since versionedobjects is partially merged, metadata
        # may come from 'volume_metadata' or 'metadata', here
        # we need to take care both of them.
        volume_metadata = {}
        if 'volume_metadata' in volume:
            for metadata in volume['volume_metadata']:
                volume_metadata[metadata['key']] = metadata['value']
            return volume_metadata
        return volume['metadata'] if 'metadata' in volume else {}

    def _is_snapcopy_enabled(self, volume):
        meta = self._get_volume_metadata(volume)
        return 'snapcopy' in meta and meta['snapcopy'].lower() == 'true'

    def _is_async_migration(self, volume):
        meta = self._get_volume_metadata(volume)
        return ('async_migrate' in meta and
                meta['async_migrate'].lower() == 'true')

    def _get_base_lun_name(self, volume):
        """Returns base LUN name for SMP or LUN."""
        base_name = self.extract_provider_location(
            volume['provider_location'], 'base_lun_name')
        if base_name is None or base_name == 'None':
            return volume['name']
        return base_name

    def dumps_provider_location(self, pl_dict):
        return '|'.join([k + '^' + pl_dict[k] for k in pl_dict])

    def _build_provider_location(self, lun_id, type='lun', base_lun_name=None):
        """Builds provider_location for volume or snapshot.

        :param lun_id: LUN ID in VNX
        :param type: 'lun' or 'smp'
        :param base_lun_name: primary LUN name,
                              it will be used when creating snap lun
        """
        pl_dict = {'system': self.get_array_serial(),
                   'type': type,
                   'id': six.text_type(lun_id),
                   'base_lun_name': six.text_type(base_lun_name),
                   'version': self.VERSION}
        return self.dumps_provider_location(pl_dict)

    def _update_provider_location(self, provider_location,
                                  key=None, value=None):
        pl_dict = {tp.split('^')[0]: tp.split('^')[1]
                   for tp in provider_location.split('|')}
        pl_dict[key] = value
        return self.dumps_provider_location(pl_dict)

    @staticmethod
    def extract_provider_location(provider_location, key='id'):
        """Extracts value of the specified field from provider_location string.

        :param provider_location: provider_location string
        :param key: field name of the value that to be extracted
        :return: value of the specified field if it exists, otherwise,
                 None is returned
        """
        ret = None
        if provider_location is not None:
            kvps = provider_location.split('|')
            for kvp in kvps:
                fields = kvp.split('^')
                if len(fields) == 2 and fields[0] == key:
                    ret = fields[1]
        return ret

    def _consistencygroup_creation_check(self, group):
        """Check extra spec for consistency group."""

        if group.get('volume_type_id') is not None:
            for id in group['volume_type_id'].split(","):
                if id:
                    provisioning, tiering = (
                        self._get_extra_spec_value(
                            volume_types.get_volume_type_extra_specs(id)))
                    if provisioning == 'compressed':
                        msg = _("Failed to create consistency group %s "
                                "because VNX consistency group cannot "
                                "accept compressed LUNs as members."
                                ) % group['id']
                        raise exception.VolumeBackendAPIException(data=msg)

    def create_consistencygroup(self, context, group):
        """Creates a consistency group."""
        LOG.info(_LI('Start to create consistency group: %(group_name)s '
                     'id: %(id)s'),
                 {'group_name': group['name'], 'id': group['id']})

        self._consistencygroup_creation_check(group)

        model_update = {'status': fields.ConsistencyGroupStatus.AVAILABLE}
        try:
            self._client.create_consistencygroup(group['id'])
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE('Create consistency group %s failed.'),
                          group['id'])

        return model_update

    def delete_consistencygroup(self, context, group, volumes):
        """Deletes a consistency group."""
        cg_name = group['id']

        model_update = {}
        volumes_model_update = []
        model_update['status'] = group['status']
        LOG.info(_LI('Start to delete consistency group: %(cg_name)s'),
                 {'cg_name': cg_name})
        try:
            self._client.delete_consistencygroup(cg_name)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE('Delete consistency group %s failed.'), cg_name)

        for volume_ref in volumes:
            try:
                self._client.delete_lun(volume_ref['name'])
                volumes_model_update.append(
                    {'id': volume_ref['id'], 'status': 'deleted'})
            except Exception:
                volumes_model_update.append(
                    {'id': volume_ref['id'], 'status': 'error_deleting'})

        return model_update, volumes_model_update

    def update_consistencygroup(self, context,
                                group,
                                add_volumes,
                                remove_volumes):
        """Adds or removes LUN(s) to/from an existing consistency group"""
        model_update = {'status': fields.ConsistencyGroupStatus.AVAILABLE}
        cg_name = group['id']
        add_ids = [six.text_type(self.get_lun_id(vol))
                   for vol in add_volumes] if add_volumes else []
        remove_ids = [six.text_type(self.get_lun_id(vol))
                      for vol in remove_volumes] if remove_volumes else []

        data = self._client.get_consistency_group_by_name(cg_name)
        ids_curr = data['Luns']
        ids_later = []

        if ids_curr:
            ids_later.extend(ids_curr)
        ids_later.extend(add_ids)
        for remove_id in remove_ids:
            if remove_id in ids_later:
                ids_later.remove(remove_id)
            else:
                LOG.warning(_LW("LUN with id %(remove_id)s is not present "
                                "in cg %(cg_name)s, skip it."),
                            {'remove_id': remove_id, 'cg_name': cg_name})
        # Remove all from cg
        if not ids_later:
            self._client.remove_luns_from_consistencygroup(cg_name,
                                                           ids_curr)
        else:
            self._client.replace_luns_in_consistencygroup(cg_name,
                                                          ids_later)
        return model_update, None, None

    def create_cgsnapshot(self, context, cgsnapshot, snapshots):
        """Creates a cgsnapshot (snap group)."""
        cgsnapshot_id = cgsnapshot['id']

        model_update = {}
        snapshots_model_update = []
        LOG.info(_LI('Start to create cgsnapshot for consistency group'
                     ': %(group_name)s'),
                 {'group_name': cgsnapshot['consistencygroup_id']})

        try:
            self._client.create_cgsnapshot(cgsnapshot['consistencygroup_id'],
                                           cgsnapshot['id'])
            for snapshot in snapshots:
                snapshots_model_update.append(
                    {'id': snapshot['id'], 'status': 'available'})
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE('Create cg snapshot %s failed.'),
                          cgsnapshot_id)

        model_update['status'] = 'available'

        return model_update, snapshots_model_update

    def delete_cgsnapshot(self, context, cgsnapshot, snapshots):
        """Deletes a cgsnapshot (snap group)."""
        cgsnapshot_id = cgsnapshot['id']

        model_update = {}
        snapshots_model_update = []
        model_update['status'] = cgsnapshot['status']
        LOG.info(_LI('Delete cgsnapshot %(snap_name)s for consistency group: '
                     '%(group_name)s'), {'snap_name': cgsnapshot['id'],
                 'group_name': cgsnapshot['consistencygroup_id']})

        try:
            self._client.delete_cgsnapshot(cgsnapshot['id'])
            for snapshot in snapshots:
                snapshots_model_update.append(
                    {'id': snapshot['id'], 'status': 'deleted'})
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE('Delete cgsnapshot %s failed.'),
                          cgsnapshot_id)

        return model_update, snapshots_model_update

    def get_lun_id(self, volume):
        lun_id = None
        try:
            provider_location = volume.get('provider_location')
            if provider_location:
                lun_id = self.extract_provider_location(
                    provider_location,
                    'id')
            if lun_id:
                lun_id = int(lun_id)
            else:
                LOG.debug('Lun id is not stored in provider location, '
                          'query it.')
                lun_id = self._client.get_lun_by_name(volume['name'])['lun_id']
        except Exception as ex:
            LOG.debug('Exception when getting lun id: %s.', six.text_type(ex))
            lun_id = self._client.get_lun_by_name(volume['name'])['lun_id']
        LOG.debug('Get lun_id: %s.', lun_id)
        return lun_id

    def get_lun_map(self, storage_group):
        data = self._client.get_storage_group(storage_group)
        return data['lunmap']

    def assure_storage_group(self, storage_group):
        self._client.create_storage_group(storage_group)

    def assure_host_in_storage_group(self, hostname, storage_group):
        try:
            self._client.connect_host_to_storage_group(hostname, storage_group)
        except exception.EMCVnxCLICmdError as ex:
            if ex.kwargs["rc"] == 83:
                # SG was not created or was destroyed by another concurrent
                # operation before connected.
                # Create SG and try to connect again
                LOG.warning(_LW('Storage Group %s is not found. Create it.'),
                            storage_group)
                self.assure_storage_group(storage_group)
                self._client.connect_host_to_storage_group(
                    hostname, storage_group)
            else:
                raise
        return hostname

    def get_lun_owner(self, volume):
        """Returns SP owner of the volume."""
        data = self._client.get_lun_by_name(volume['name'],
                                            poll=False)
        owner_sp = data['owner']
        LOG.debug('Owner SP : %s', owner_sp)
        return owner_sp

    def filter_available_hlu_set(self, used_hlus):
        used_hlu_set = set(used_hlus)
        return self.hlu_set - used_hlu_set

    def _extract_iscsi_uids(self, connector):
        if 'initiator' not in connector:
            if self.protocol == 'iSCSI':
                msg = (_('Host %s has no iSCSI initiator')
                       % connector['host'])
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
        return [re.sub(r'\S\S', lambda m: m.group(0) + ':',
                       wwn, len(wwn) // 2 - 1)
                for wwn in wwns]

    def _exec_command_setpath(self, initiator_uid, sp, port_id,
                              ip, host, vport_id=None):
        gname = host
        if vport_id is not None:
            cmd_iscsi_setpath = ('storagegroup', '-setpath', '-gname', gname,
                                 '-hbauid', initiator_uid, '-sp', sp,
                                 '-spport', port_id, '-spvport', vport_id,
                                 '-ip', ip, '-host', host, '-o')
            out, rc = self._client.command_execute(*cmd_iscsi_setpath)
        else:
            cmd_fc_setpath = ('storagegroup', '-setpath', '-gname', gname,
                              '-hbauid', initiator_uid, '-sp', sp,
                              '-spport', port_id,
                              '-ip', ip, '-host', host, '-o')
            out, rc = self._client.command_execute(*cmd_fc_setpath)
        if rc != 0:
            LOG.warning(_LW("Failed to register %(itor)s to SP%(sp)s "
                            "port %(portid)s because: %(msg)s."),
                        {'itor': initiator_uid,
                         'sp': sp,
                         'portid': port_id,
                         'msg': out})

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
                    new_ports = filter(
                        lambda pt: (pt['SP'], pt['Port ID'],
                                    pt['Virtual Port ID'])
                        not in sp_ports,
                        self.iscsi_targets[sp])
                    new_white[sp] = map(lambda white:
                                        {'SP': white['SP'],
                                         'Port ID': white['Port ID'],
                                         'Virtual Port ID':
                                             white['Virtual Port ID']},
                                        new_ports)
            else:
                new_white = self.iscsi_targets
            self._register_iscsi_initiator(ip, host, [initiator], new_white)

        elif self.protocol == 'FC':
            wwns = self._extract_fc_uids(connector)
            ports_list = []
            if sgdata:
                for wwn in wwns:
                    for port in io_port_filter:
                        if ((port not in ports_list) and
                                (not re.search(wwn + '\s+SP\s+' +
                                               port[0] + '\s+' + str(port[1]),
                                               sgdata['raw_output'],
                                               re.IGNORECASE))):
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
            LOG.info(_LI('Get ISCSI targets %(tg)s to register '
                         'initiator %(in)s.'),
                     {'tg': iscsi_targets,
                      'in': initiator_uid})

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
            LOG.info(_LI('Get FC targets %(tg)s to register '
                         'initiator %(in)s.'),
                     {'tg': fc_targets,
                      'in': initiator_uid})

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

    def _deregister_initiators(self, connector):
        initiator_uids = []
        try:
            if self.protocol == 'iSCSI':
                initiator_uids = self._extract_iscsi_uids(connector)
            elif self.protocol == 'FC':
                initiator_uids = self._extract_fc_uids(connector)
        except exception.VolumeBackendAPIException:
            LOG.warning(_LW("Failed to extract initiators of %s, so ignore "
                            "deregistration operation."),
                        connector['host'])
        if initiator_uids:
            for initiator_uid in initiator_uids:
                rc, out = self._client.deregister_initiator(initiator_uid)
                if rc != 0:
                    LOG.warning(_LW("Failed to deregister %(itor)s "
                                    "because: %(msg)s."),
                                {'itor': initiator_uid,
                                 'msg': out})

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
        """Automatically registers available initiators.

        Returns True if has registered initiator otherwise returns False.
        """
        initiator_uids = []
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

            LOG.info(_LI('iSCSI Initiators %(in)s of %(ins)s '
                         'need registration.'),
                     {'in': itors_toReg,
                      'ins': initiator_uids})
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

            LOG.info(_LI('FC Initiators %(in)s of %(ins)s need registration'),
                     {'in': itors_toReg,
                      'ins': initiator_uids})
            self._register_fc_initiator(ip, host, itors_toReg)
            return True

    def auto_register_initiator(self, connector, sgdata, io_ports_filter=None):
        """Automatically register available initiators.

        :returns: True if has registered initiator otherwise return False
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
        except exception.EMCVnxCLICmdError as ex:
            if ex.kwargs["rc"] != 83:
                raise
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
                except exception.EMCVnxCLICmdError as ex:
                    LOG.debug("Add HLU to storagegroup failed, retry %s",
                              tried)
            elif tried == 1:
                # The first try didn't get the in time data,
                # so we need a retry
                LOG.debug("Did not find candidate HLUs, retry %s",
                          tried)
            else:
                msg = (_('Reach limitation set by configuration '
                         'option max_luns_per_storage_group. '
                         'Operation to add %(vol)s into '
                         'Storage Group %(sg)s is rejected.')
                       % {'vol': volumename, 'sg': hostname})
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)

            # Need a full poll to get the real in time data
            # Query storage group with poll for retry
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

    def vnx_get_iscsi_properties(self, volume, connector, hlu, sg_raw_output):
        storage_group = connector['host']
        registered_spports = self._client.get_registered_spport_set(
            connector['initiator'],
            storage_group,
            sg_raw_output)
        targets = self._client.find_available_iscsi_targets(
            storage_group,
            registered_spports,
            self.iscsi_targets)
        properties = {'target_discovered': False,
                      'target_iqn': 'unknown',
                      'target_iqns': None,
                      'target_portal': 'unknown',
                      'target_portals': None,
                      'target_lun': 'unknown',
                      'target_luns': None,
                      'volume_id': volume['id']}
        if targets:
            properties['target_discovered'] = True
            properties['target_iqns'] = [t['Port WWN'] for t in targets]
            properties['target_iqn'] = properties['target_iqns'][0]
            properties['target_portals'] = [
                "%s:3260" % t['IP Address'] for t in targets]
            properties['target_portal'] = properties['target_portals'][0]
            properties['target_luns'] = [hlu] * len(targets)
            properties['target_lun'] = hlu
        else:
            LOG.error(_LE('Failed to find available iSCSI targets for %s.'),
                      storage_group)

        LOG.debug('The iSCSI properties for %(host)s is %(properties)s.',
                  {'host': storage_group,
                   'properties': properties})
        return properties

    def vnx_get_fc_properties(self, connector, device_number):
        fc_properties = {'target_lun': device_number,
                         'target_dicovered': True,
                         'target_wwn': None}
        if self.zonemanager_lookup_service is None:
            fc_properties['target_wwn'] = self.get_login_ports(connector,
                                                               self.io_ports)
        else:
            target_wwns, itor_tgt_map = self.get_initiator_target_map(
                connector['wwpns'],
                self.get_status_up_ports(connector, self.io_ports))
            fc_properties['target_wwn'] = target_wwns
            fc_properties['initiator_target_map'] = itor_tgt_map
        return fc_properties

    def initialize_connection(self, volume, connector):
        """Initializes the connection and returns connection info."""
        @conditional_lock(lock=self.destroy_empty_sg, host=connector['host'])
        def do_initialize_connection():
            return self.assure_host_access(
                volume, connector)

        data = {}
        if self.protocol == 'iSCSI':
            (device_number, sg_data) = do_initialize_connection()
            iscsi_properties = self.vnx_get_iscsi_properties(
                volume,
                connector,
                device_number,
                sg_data['raw_output']
            )
            data = {'driver_volume_type': 'iscsi',
                    'data': iscsi_properties}
        elif self.protocol == 'FC':
            (device_number, sg_data) = do_initialize_connection()
            fc_properties = self.vnx_get_fc_properties(connector,
                                                       device_number)
            fc_properties['volume_id'] = volume['id']
            data = {'driver_volume_type': 'fibre_channel',
                    'data': fc_properties}

        return data

    def terminate_connection(self, volume, connector):
        """Disallow connection from connector."""
        @conditional_lock(lock=self.destroy_empty_sg, host=connector['host'])
        def do_terminate_connection():
            hostname = connector['host']
            volume_name = volume['name']
            lun_id = self.get_lun_id(volume)
            lun_map = None
            conn_info = None
            if (hostname in self.hlu_cache and
                    lun_id in self.hlu_cache[hostname] and
                    not self.destroy_empty_sg and
                    not self.zonemanager_lookup_service):
                hlu = self.hlu_cache[hostname][lun_id]
                self._client.remove_hlu_from_storagegroup(hlu, hostname,
                                                          poll=True)
                self.hlu_cache[hostname].pop(lun_id)
            else:
                try:
                    lun_map = self.get_lun_map(hostname)
                    self.hlu_cache[hostname] = lun_map
                except exception.EMCVnxCLICmdError as ex:
                    if ex.kwargs["rc"] == 83:
                        LOG.warning(_LW("Storage Group %s is not found. "
                                        "terminate_connection() is "
                                        "unnecessary."),
                                    hostname)
                if lun_id in lun_map:
                    self._client.remove_hlu_from_storagegroup(
                        lun_map[lun_id], hostname)
                    lun_map.pop(lun_id)
                else:
                    LOG.warning(_LW("Volume %(vol)s was not in Storage Group"
                                    " %(sg)s."),
                                {'vol': volume_name, 'sg': hostname})

            if self.protocol == 'FC':
                conn_info = {'driver_volume_type': 'fibre_channel',
                             'data': {}}
                if self.zonemanager_lookup_service and not lun_map:
                    target_wwns, itor_tgt_map = self.get_initiator_target_map(
                        connector['wwpns'],
                        self.get_status_up_ports(connector))
                    conn_info['data']['initiator_target_map'] = itor_tgt_map

            if self.destroy_empty_sg and not lun_map:
                try:
                    LOG.info(_LI("Storage Group %s was empty."), hostname)
                    self._client.disconnect_host_from_storage_group(
                        hostname, hostname)
                    self._client.delete_storage_group(hostname)
                    if self.itor_auto_dereg:
                        self._deregister_initiators(connector)
                except Exception:
                    LOG.warning(_LW("Failed to destroy Storage Group %s."),
                                hostname)
                    try:
                        self._client.connect_host_to_storage_group(
                            hostname, hostname)
                    except Exception:
                        LOG.warning(_LW("Fail to connect host %(host)s "
                                        "back to storage group %(sg)s."),
                                    {'host': hostname, 'sg': hostname})
            return conn_info
        return do_terminate_connection()

    def initialize_connection_snapshot(self, snapshot, connector, **kwargs):
        """Initializes connection for mount point."""
        smp_name = self._construct_tmp_smp_name(snapshot)
        self._client.attach_mount_point(smp_name, snapshot.name)
        volume = {'name': smp_name, 'id': snapshot.id}
        return self.initialize_connection(volume, connector)

    def terminate_connection_snapshot(self, snapshot, connector, **kwargs):
        """Disallows connection for mount point."""
        smp_name = self._construct_tmp_smp_name(snapshot)
        volume = {'name': smp_name}
        conn_info = self.terminate_connection(volume, connector)
        self._client.detach_mount_point(smp_name)
        return conn_info

    def create_export_snapshot(self, context, snapshot, connector):
        """Creates mount point for a snapshot."""
        smp_name = self._construct_tmp_smp_name(snapshot)
        primary_lun_name = snapshot.volume_name
        self._client.create_mount_point(primary_lun_name, smp_name)
        return None

    def remove_export_snapshot(self, context, snapshot):
        """Removes mount point for a snapshot."""
        smp_name = self._construct_tmp_smp_name(snapshot)
        volume = {'name': smp_name, 'provider_location': None,
                  'volume_type_id': None}
        self.delete_volume(volume, True)

    def manage_existing_get_size(self, volume, existing_ref):
        """Returns size of volume to be managed by manage_existing."""
        if 'source-id' in existing_ref:
            data = self._client.get_lun_by_id(
                existing_ref['source-id'],
                properties=VNXLunProperties.lun_with_pool)
        elif 'source-name' in existing_ref:
            data = self._client.get_lun_by_name(
                existing_ref['source-name'],
                properties=VNXLunProperties.lun_with_pool)
        else:
            reason = _('Reference must contain source-id or source-name key.')
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref, reason=reason)
        target_pool = self.get_target_storagepool(volume)
        if target_pool and data['pool'] != target_pool:
            reason = (_('The imported lun %(lun_id)s is in pool %(lun_pool)s '
                        'which is not managed by the host %(host)s.')
                      % {'lun_id': data['lun_id'],
                         'lun_pool': data['pool'],
                         'host': volume['host']})
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref, reason=reason)
        return data['total_capacity_gb']

    def manage_existing(self, volume, manage_existing_ref):
        """Imports the existing backend storage object as a volume.

        manage_existing_ref:{
            'source-id':<lun id in VNX>
        }
        or
        manage_existing_ref:{
            'source-name':<lun name in VNX>
        }
        """
        client = self._client

        lun_id = self._get_lun_id(manage_existing_ref)

        specs = self.get_volumetype_extraspecs(volume)
        LOG.debug('Specs of the volume is: %s.', specs)

        host = volume['host']
        LOG.debug('Host of the volume is: %s.', host)

        tar_pool = vol_utils.extract_host(volume['host'], 'pool')
        LOG.debug("Target pool of LUN to manage is: %s.", tar_pool)

        tar_type, tar_tier = self._get_extra_spec_value(specs)
        vnx_lun = self._get_lun_pool_and_type(lun_id)
        LOG.debug("LUN to manage: %s.", vnx_lun)
        LOG.debug("Target info: pool: %(pool)s, type: %(type)s, "
                  "tier: %(tier)s.", {'pool': tar_pool,
                                      'type': tar_type,
                                      'tier': tar_tier})

        do_migration = (tar_type is not None and
                        tar_type != vnx_lun.provision or
                        tar_pool != vnx_lun.pool_name)
        change_tier = (tar_tier is not None and
                       not do_migration and
                       tar_tier != vnx_lun.tier)

        reason = None
        if do_migration:
            LOG.debug("Need migration during manage.")
            if client.check_lun_has_snap(lun_id):
                reason = _('Driver is not able to do retype because'
                           ' the volume (LUN {}) has snapshot which is '
                           'forbidden to migrate.').format(lun_id)
            else:
                volume['size'] = vnx_lun.capacity
                moved, empty = self._migrate_volume(volume,
                                                    tar_pool,
                                                    specs,
                                                    src_id=lun_id)
                if not moved:
                    reason = _('Storage-assisted migration failed during '
                               'manage volume.')

        if reason is None and change_tier:
            LOG.debug('Change LUN tier policy to: %s.', tar_tier)
            client.modify_lun_tiering_by_id(lun_id, tar_tier)

        if reason is not None:
            raise exception.ManageExistingVolumeTypeMismatch(reason=reason)
        else:
            client.rename_lun(lun_id, volume['name'])

        location = self._build_provider_location(lun_id, 'lun', volume['name'])
        return {'provider_location': location}

    def _get_lun_pool_and_type(self, lun_id):
        client = self._client
        data = client.get_lun_by_id(lun_id,
                                    VNXLunProperties.get_all(),
                                    poll=False)
        lun = VNXLun()
        lun.update(data)
        return lun

    def _get_lun_id(self, manage_existing_ref):
        if 'source-id' in manage_existing_ref:
            lun_id = manage_existing_ref['source-id']
        elif 'source-name' in manage_existing_ref:
            lun_id = self._client.get_lun_by_name(
                manage_existing_ref['source-name'], poll=False)['lun_id']
        else:
            reason = _('Reference must contain source-id or source-name key.')
            raise exception.ManageExistingInvalidReference(
                existing_ref=manage_existing_ref, reason=reason)
        return lun_id

    def get_login_ports(self, connector, io_ports=None):
        return self._client.get_login_ports(connector['host'],
                                            connector['wwpns'],
                                            io_ports)

    def get_status_up_ports(self, connector, io_ports=None):
        return self._client.get_status_up_ports(connector['host'],
                                                io_ports=io_ports)

    def get_initiator_target_map(self, fc_initiators, fc_targets):
        target_wwns = []
        itor_tgt_map = {}

        if self.zonemanager_lookup_service:
            mapping = \
                self.zonemanager_lookup_service. \
                get_device_mapping_from_network(fc_initiators, fc_targets)
            for each in mapping:
                map_d = mapping[each]
                target_wwns.extend(map_d['target_port_wwn_list'])
                for initiator in map_d['initiator_port_wwn_list']:
                    itor_tgt_map[initiator] = map_d['target_port_wwn_list']
        return list(set(target_wwns)), itor_tgt_map

    def get_volumetype_extraspecs(self, volume):
        specs = {}

        type_id = volume['volume_type_id']
        if type_id is not None:
            specs = volume_types.get_volume_type_extra_specs(type_id)

        return specs

    def failover_host(self, context, volumes, secondary_backend_id):
        """Fails over the volume back and forth.

        Driver needs to update following info for this volume:
        2. provider_location: update serial number and lun id
        """
        volume_update_list = []

        if secondary_backend_id != 'default':
            rep_status = 'failed-over'
            backend_id = (
                self.configuration.replication_device[0]['backend_id'])
            if secondary_backend_id != backend_id:
                msg = (_('Invalid secondary_backend_id specified. '
                         'Valid backend id is %s.') % backend_id)
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)
        else:
            rep_status = 'enabled'

        def failover_one(volume, new_status):
            rep_data = json.loads(volume['replication_driver_data'])
            is_primary = rep_data['is_primary']
            mirror_name = self._construct_mirror_name(volume)
            mirror_view = self._get_mirror_view(volume)
            remote_client = mirror_view._secondary_client

            provider_location = volume['provider_location']
            try:
                mirror_view.promote_image(mirror_name)
            except exception.EMCVnxCLICmdError as ex:
                msg = _LE(
                    'Failed to failover volume %(volume_id)s '
                    'to %(target)s: %(error)s.')
                LOG.error(msg, {'volume_id': volume.id,
                                'target': secondary_backend_id,
                                'error': ex},)
                new_status = 'error'
            else:
                rep_data.update({'is_primary': not is_primary})
                # Transfer ownership to secondary_backend_id and
                # update provider_location field
                provider_location = self._update_provider_location(
                    provider_location,
                    'system', remote_client.get_array_serial()['array_serial'])
                provider_location = self._update_provider_location(
                    provider_location,
                    'id',
                    six.text_type(
                        remote_client.get_lun_by_name(volume.name)['lun_id'])
                )
            model_update = {'volume_id': volume.id,
                            'updates':
                                {'replication_driver_data':
                                    json.dumps(rep_data),
                                 'replication_status': new_status,
                                 'provider_location': provider_location}}
            volume_update_list.append(model_update)
        for volume in volumes:
            if self._is_replication_enabled(volume):
                failover_one(volume, rep_status)
            else:
                volume_update_list.append({
                    'volume_id': volume.id,
                    'updates': {'status': 'error'}})
        return secondary_backend_id, volume_update_list

    def _is_replication_enabled(self, volume):
        """Return True if replication extra specs is specified.

        If replication_enabled exists and value is '<is> True', return True.
        If replication exists and value is 'True', return True.
        Otherwise, return False.
        """
        specs = self.get_volumetype_extraspecs(volume)
        return specs and specs.get('replication_enabled') == '<is> True'

    def setup_lun_replication(self, volume, primary_lun_id,
                              provisioning, tiering):
        """Setup replication for LUN, this only happens in primary system."""
        rep_update = {'replication_driver_data': None,
                      'replication_status': 'disabled'}
        metadata_update = {}
        if self._is_replication_enabled(volume):
            LOG.debug('Starting setup replication '
                      'for volume: %s.', volume.id)
            lun_size = volume['size']
            mirror_name = self._construct_mirror_name(volume)
            pool_name = vol_utils.extract_host(volume.host, 'pool')
            self._mirror.create_mirror_workflow(
                mirror_name, primary_lun_id, pool_name,
                volume.name, lun_size,
                provisioning, tiering)

            LOG.info(_LI('Successfully setup replication for %s.'), volume.id)
            rep_update.update({'replication_driver_data':
                              self.__class__._build_replication_driver_data(
                                  self.configuration),
                               'replication_status': 'enabled'})
            metadata_update = {
                'system': self.get_array_serial()}
        return rep_update, metadata_update

    def cleanup_lun_replication(self, volume):
        if self._is_replication_enabled(volume):
            LOG.debug('Starting cleanup replication form volume: '
                      '%s.', volume.id)
            mirror_name = self._construct_mirror_name(volume)
            mirror_view = self._get_mirror_view(volume)
            mv = mirror_view.get_image(mirror_name)
            if mv:
                mirror_view.destroy_mirror_view(mirror_name, volume.name, mv)

    def _get_mirror_view(self, volume):
        """Determines where to build a Mirror View operator."""
        if volume['replication_driver_data']:
            rep_data = json.loads(volume['replication_driver_data'])
            is_primary = rep_data['is_primary']
        else:
            is_primary = True
        if is_primary:
            # if on primary, promote to configured array in conf
            mirror_view = self._mirror
        else:
            # else promote to array according to volume data
            mirror_view = self._build_mirror_view(volume)
        return mirror_view

    @staticmethod
    def _build_replication_driver_data(configuration):
        """Builds driver specific data for replication.

        This data will be used by secondary backend to connect
        primary device.
        """
        driver_data = dict()
        driver_data['san_ip'] = configuration.san_ip
        driver_data['san_login'] = configuration.san_login
        driver_data['san_password'] = configuration.san_password
        driver_data['san_secondary_ip'] = configuration.san_secondary_ip
        driver_data['storage_vnx_authentication_type'] = (
            configuration.storage_vnx_authentication_type)
        driver_data['storage_vnx_security_file_dir'] = (
            configuration.storage_vnx_security_file_dir)
        driver_data['is_primary'] = True
        return json.dumps(driver_data)

    def _build_client(self, active_backend_id=None):
        """Builds a client pointing to the right VNX."""
        if not active_backend_id:
            return CommandLineHelper(self.configuration)
        else:
            configuration = self.configuration
            if not configuration.replication_device:
                err_msg = (
                    _('replication_device should be configured '
                      'on backend: %s.') % configuration.config_group)
                LOG.error(err_msg)
                raise exception.VolumeBackendAPIException(data=err_msg)
            current_target = None
            for target in configuration.replication_device:
                if target['backend_id'] == active_backend_id:
                    current_target = target
                    break
            if not current_target:
                err_msg = (
                    _('replication_device with backend_id [%s] is missing.')
                    % active_backend_id)
                LOG.error(err_msg)
                raise exception.VolumeBackendAPIException(data=err_msg)
            target_conf = copy.copy(configuration)
            for key in self.REPLICATION_KEYS:
                if key in current_target:
                    setattr(target_conf, key, current_target[key])
            return CommandLineHelper(target_conf)

    def _build_mirror_view(self, volume=None):
        """Builds a client for remote storage device.

        Currently, only support one remote, managed device.
        :param volume: if volume is not None, then build a remote client
                       from volume's replication_driver_data.
        """
        configuration = self.configuration
        remote_info = None
        if volume:
            if volume['replication_driver_data']:
                remote_info = json.loads(volume['replication_driver_data'])
            else:
                LOG.warning(
                    _LW('No replication info from this volume: %s.'),
                    volume.id)
                return None
        else:
            if not configuration.replication_device:
                LOG.info(_LI('Replication is not configured on backend: %s.'),
                         configuration.config_group)
                return None
            remote_info = configuration.replication_device[0]
        # Copy info to replica configuration for remote client
        replica_conf = copy.copy(configuration)
        for key in self.REPLICATION_KEYS:
            if key in remote_info:
                setattr(replica_conf, key, remote_info[key])
        _remote_client = CommandLineHelper(replica_conf)
        _mirror = MirrorView(self._client, _remote_client)
        return _mirror

    def get_pool(self, volume):
        """Returns the pool name of a volume."""

        data = self._client.get_lun_by_name(volume['name'],
                                            [VNXLunProperties.LUN_POOL],
                                            poll=False)
        return data.get(VNXLunProperties.LUN_POOL.key)

    def unmanage(self, volume):
        """Unmanages a volume"""
        pass

    def create_consistencygroup_from_src(self, context, group, volumes,
                                         cgsnapshot=None, snapshots=None,
                                         source_cg=None, source_vols=None):
        """Creates a consistency group from cgsnapshot."""
        if cgsnapshot and snapshots and not source_cg:
            return self._create_consisgroup_from_cgsnapshot(
                group, volumes, cgsnapshot, snapshots)
        elif source_cg and source_vols and not cgsnapshot:
            return self._clone_consisgroup(
                group, volumes, source_cg, source_vols)
        else:
            msg = _("create_consistencygroup_from_src supports a "
                    "cgsnapshot source or a consistency group source. "
                    "Multiple sources cannot be used.")
            raise exception.InvalidInput(reason=msg)

    def _clone_consisgroup(self, group, volumes, source_cg, source_vols):
        temp_cgsnapshot_name = 'temp_snapshot_for_{}'.format(group.id)
        store_spec = {
            'group': group,
            'snapshot': {'id': temp_cgsnapshot_name,
                         'consistencygroup_id': source_cg.id},
            'new_snap_name': temp_cgsnapshot_name,
            'source_lun_id': None,
            'client': self._client
        }
        flow_name = 'clone_consisgroup'
        snap_build_tasks = [CreateSnapshotTask()]

        volume_model_updates = self._create_cg_from_cgsnap_use_workflow(
            flow_name, snap_build_tasks, store_spec,
            volumes, source_vols)

        self._delete_temp_cgsnap(temp_cgsnapshot_name)

        LOG.info(_LI('Consistency group %(cg)s is created successfully.'),
                 {'cg': group.id})

        return None, volume_model_updates

    def _create_consisgroup_from_cgsnapshot(self, group, volumes,
                                            cgsnapshot, snapshots):
        flow_name = 'create_consisgroup_from_cgsnapshot'
        copied_snapshot_name = 'temp_snapshot_for_%s' % group.id
        store_spec = {
            'group': group,
            'src_snap_name': cgsnapshot['id'],
            'new_snap_name': copied_snapshot_name,
            'client': self._client,
            'temp_snap': True,
        }

        snap_build_tasks = [CopySnapshotTask(),
                            ModifySnapshotTask()]

        src_vols = map(lambda snap: snap.volume, snapshots)

        volume_model_updates = self._create_cg_from_cgsnap_use_workflow(
            flow_name, snap_build_tasks, store_spec, volumes, src_vols)

        self._delete_temp_cgsnap(copied_snapshot_name)

        LOG.info(_LI('Consistency group %(cg)s is created successfully.'),
                 {'cg': group.id})

        return None, volume_model_updates

    def _delete_temp_cgsnap(self, snap):
        try:
            self._client.delete_cgsnapshot(snap)
        except exception.EMCVnxCLICmdError as ex:
            LOG.warning(_LW('Delete the temporary cgsnapshot %(name)s failed. '
                            'This temporary cgsnapshot can be deleted '
                            'manually. '
                            'Message: %(msg)s'),
                        {'name': snap,
                         'msg': ex.kwargs['out']})

    def _create_cg_from_cgsnap_use_workflow(self, flow_name, snap_build_tasks,
                                            store_spec, volumes, source_vols):
        work_flow = linear_flow.Flow(flow_name)
        work_flow.add(*snap_build_tasks)
        # Add tasks for each volumes in the consistency group
        lun_id_key_template = 'new_lun_id_%s'
        lun_data_key_template = 'vol_%s'
        volume_model_updates = []

        for i, (volume, src_volume) in enumerate(zip(volumes, source_vols)):
            specs = self.get_volumetype_extraspecs(volume)
            provisioning, tiering = (
                self._get_and_validate_extra_specs(specs))
            pool_name = self.get_target_storagepool(volume, src_volume)
            base_lun_name = self._get_base_lun_name(src_volume)
            sub_store_spec = {
                'volume': volume,
                'base_lun_name': base_lun_name,
                'pool_name': pool_name,
                'dest_vol_name': volume['name'] + '_dest',
                'volume_size': volume['size'],
                'provisioning': provisioning,
                'tiering': tiering,
                'ignore_pool_full_threshold': self.ignore_pool_full_threshold,
                'async_migrate': True,
            }
            work_flow.add(
                CreateSMPTask(name="CreateSMPTask%s" % i,
                              inject=sub_store_spec),
                AttachSnapTask(name="AttachSnapTask%s" % i,
                               inject=sub_store_spec),
                CreateDestLunTask(name="CreateDestLunTask%s" % i,
                                  provides=lun_data_key_template % i,
                                  inject=sub_store_spec),
                MigrateLunTask(name="MigrateLunTask%s" % i,
                               provides=lun_id_key_template % i,
                               inject=sub_store_spec,
                               rebind={'lun_data': lun_data_key_template % i}))

            volume_model_updates.append({'id': volume['id']})
            volume_host = volume['host']
            host = vol_utils.extract_host(volume_host, 'backend')
            host_and_pool = vol_utils.append_host(host, pool_name)
            if volume_host != host_and_pool:
                volume_model_updates[i]['host'] = host_and_pool

        work_flow.add(WaitMigrationsCompleteTask(lun_id_key_template,
                                                 len(volumes)),
                      CreateConsistencyGroupTask(lun_id_key_template,
                                                 len(volumes)))

        flow_engine = taskflow.engines.load(work_flow, store=store_spec)
        flow_engine.run()

        for i, update in enumerate(volume_model_updates):
            new_lun_id = flow_engine.storage.fetch(lun_id_key_template % i)
            update['provider_location'] = (
                self._build_provider_location(new_lun_id))
        return volume_model_updates

    def get_target_storagepool(self, volume, source_volume=None):
        pool = vol_utils.extract_host(volume['host'], 'pool')

        # For new created volume that is not from snapshot or cloned
        # or the pool is the managed pool,
        # just use the pool selected by scheduler
        if not source_volume or pool in self.storage_pools:
            return pool

        # For volume created from snapshot or cloned from volume, the pool to
        # use depends on the source volume version. If the source volume is
        # created by older version of driver which doesn't support pool
        # scheduler, use the pool where the source volume locates. Otherwise,
        # use the pool selected by scheduler
        provider_location = source_volume.get('provider_location')

        if (not provider_location or
                not self.extract_provider_location(provider_location,
                                                   'version')):
            LOG.warning(_LW("The source volume is a legacy volume. "
                            "Create volume in the pool where the source "
                            "volume %s is created."),
                        source_volume['name'])
            data = self._client.get_lun_by_name(source_volume['name'],
                                                [VNXLunProperties.LUN_POOL],
                                                poll=False)
            if data is None:
                msg = (_("Failed to find storage pool for source volume %s.")
                       % source_volume['name'])
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)
            pool = data[VNXLunProperties.LUN_POOL.key]

        if self.storage_pools and pool not in self.storage_pools:
            msg = (_("The source volume %s is not in the pool which "
                     "is managed by the current host.")
                   % source_volume['name'])
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        return pool

    def update_volume_stats(self):
        """Retrieves stats info."""
        self.update_enabler_in_volume_stats()
        if self.protocol == 'iSCSI':
            self.iscsi_targets = self._client.get_iscsi_targets(
                poll=False, io_ports=self.io_ports)

        properties = [VNXPoolProperties.POOL_FREE_CAPACITY,
                      VNXPoolProperties.POOL_TOTAL_CAPACITY,
                      VNXPoolProperties.POOL_STATE,
                      VNXPoolProperties.POOL_SUBSCRIBED_CAPACITY,
                      VNXPoolProperties.POOL_FULL_THRESHOLD]
        if '-FASTCache' in self.enablers:
            properties.append(VNXPoolProperties.POOL_FAST_CACHE)

        pool_list = self._client.get_pool_list(properties, False)

        if self.storage_pools:
            pool_list = filter(lambda a: a['pool_name'] in self.storage_pools,
                               pool_list)
        pool_feature = (self._client.get_pool_feature_properties(poll=False)
                        if self.check_max_pool_luns_threshold else None)
        self.stats['pools'] = [self._build_pool_stats(pool, pool_feature)
                               for pool in pool_list]

        return self.stats


def getEMCVnxCli(prtcl, configuration=None, active_backend_id=None):
    configuration.append_config_values(loc_opts)
    return EMCVnxCliBase(prtcl, configuration=configuration,
                         active_backend_id=active_backend_id)


class CreateSMPTask(task.Task):
    """Creates a snap mount point (SMP) for the source snapshot.

    Reversion strategy: Delete the SMP.
    """
    def __init__(self, name=None, inject=None):
        super(CreateSMPTask, self).__init__(name=name,
                                            provides='new_smp_id',
                                            inject=inject)

    def execute(self, client, volume, base_lun_name, *args, **kwargs):
        LOG.debug('CreateSMPTask.execute')
        client.create_mount_point(base_lun_name, volume['name'])
        return client.get_lun_by_name(volume['name'])['lun_id']

    def revert(self, result, client, volume, *args, **kwargs):
        LOG.debug('CreateSMPTask.revert')
        if isinstance(result, failure.Failure):
            return
        else:
            LOG.warning(_LW('CreateSMPTask.revert: delete mount point %s'),
                        volume['name'])
            client.delete_lun(volume['name'])


class AttachSnapTask(task.Task):
    """Attaches the snapshot to the SMP created before.

    Reversion strategy: Detach the SMP.
    """
    def execute(self, client, volume, new_snap_name,
                *args, **kwargs):
        LOG.debug('AttachSnapTask.execute')
        client.attach_mount_point(volume['name'], new_snap_name)

    def revert(self, result, client, volume, *args, **kwargs):
        LOG.debug('AttachSnapTask.revert')
        if isinstance(result, failure.Failure):
            return
        else:
            LOG.warning(_LW('AttachSnapTask.revert: detach mount point %s'),
                        volume['name'])
            try:
                client.detach_mount_point(volume['name'])
            except exception.EMCVnxCLICmdError as ex:
                with excutils.save_and_reraise_exception() as ctxt:
                    is_not_smp_err = (
                        ex.kwargs["rc"] == 163 and
                        VNXError.has_error("".join(ex.kwargs["out"]),
                                           VNXError.LUN_IS_NOT_SMP))
                    ctxt.reraise = not is_not_smp_err


class CreateDestLunTask(task.Task):
    """Creates a destination lun for migration.

    Reversion strategy: Delete the temp destination lun.
    """
    def __init__(self, name=None, provides='lun_data', inject=None):
        super(CreateDestLunTask, self).__init__(name=name,
                                                provides=provides,
                                                inject=inject)

    def execute(self, client, pool_name, dest_vol_name, volume_size,
                provisioning, tiering, ignore_pool_full_threshold,
                *args, **kwargs):
        LOG.debug('CreateDestLunTask.execute')
        data = client.create_lun_with_advance_feature(
            pool_name, dest_vol_name, volume_size,
            provisioning, tiering,
            ignore_thresholds=ignore_pool_full_threshold)
        return data

    def revert(self, result, client, dest_vol_name, *args, **kwargs):
        LOG.debug('CreateDestLunTask.revert')
        if isinstance(result, failure.Failure):
            return
        else:
            LOG.warning(_LW('CreateDestLunTask.revert: delete temp lun %s'),
                        dest_vol_name)
            client.delete_lun(dest_vol_name)


class MigrateLunTask(task.Task):
    """Starts a migration between the SMP and the temp lun.

    Reversion strategy: None
    """
    def __init__(self, name=None, provides='new_lun_id', inject=None,
                 rebind=None):
        super(MigrateLunTask, self).__init__(name=name,
                                             provides=provides,
                                             inject=inject,
                                             rebind=rebind)

    def execute(self, client, new_smp_id, lun_data, async_migrate=False,
                rate=VNXMigrationRate.HIGH, *args, **kwargs):
        LOG.debug('MigrateLunTask.execute')
        dest_vol_lun_id = lun_data['lun_id']
        LOG.debug('Migrating Mount Point Volume ID: %s', new_smp_id)
        if not async_migrate:
            migrated = client.migrate_lun_with_verification(new_smp_id,
                                                            dest_vol_lun_id,
                                                            None,
                                                            rate)
        else:
            migrated = client.migrate_lun_without_verification(
                new_smp_id, dest_vol_lun_id, None, rate)
        if not migrated:
            msg = (_("Migrate volume failed between source vol %(src)s"
                     " and dest vol %(dst)s.") %
                   {'src': new_smp_id, 'dst': dest_vol_lun_id})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        return new_smp_id

    def revert(self, *args, **kwargs):
        pass


class CreateSnapshotTask(task.Task):
    """Creates a snapshot/cgsnapshot of a volume.

    Reversion Strategy: Delete the created snapshot/cgsnapshot.
    """
    def execute(self, client, snapshot, source_lun_id,
                temp_snap=False, *args, **kwargs):
        LOG.debug('CreateSnapshotTask.execute')
        keep_for = None
        if temp_snap:
            keep_for = SNAP_EXPIRATION_HOUR
        # Create temp Snapshot
        if snapshot['consistencygroup_id']:
            client.create_cgsnapshot(snapshot['consistencygroup_id'],
                                     snapshot['id'], keep_for)
        else:
            snapshot_name = snapshot['name']
            volume_name = snapshot['volume_name']
            LOG.info(_LI('Create snapshot: %(snapshot)s: volume: %(volume)s'),
                     {'snapshot': snapshot_name,
                      'volume': volume_name})
            client.create_snapshot(source_lun_id, snapshot_name, keep_for)

    def revert(self, result, client, snapshot, *args, **kwargs):
        LOG.debug('CreateSnapshotTask.revert')
        if isinstance(result, failure.Failure):
            return
        else:
            if snapshot['consistencygroup_id']:
                LOG.warning(_LW('CreateSnapshotTask.revert: '
                                'delete temp cgsnapshot %s'),
                            snapshot['consistencygroup_id'])
                client.delete_cgsnapshot(snapshot['id'])
            else:
                LOG.warning(_LW('CreateSnapshotTask.revert: '
                                'delete temp snapshot %s'),
                            snapshot['name'])
                client.delete_snapshot(snapshot['name'])


class CopySnapshotTask(task.Task):
    """Task to copy a volume snapshot/consistency group snapshot.

    Reversion Strategy: Delete the copied snapshot/cgsnapshot
    """
    def execute(self, client, src_snap_name, new_snap_name, *args, **kwargs):
        LOG.debug('CopySnapshotTask.execute')
        client.copy_snapshot(src_snap_name,
                             new_snap_name)

    def revert(self, result, client, src_snap_name, new_snap_name,
               *args, **kwargs):
        LOG.debug('CopySnapshotTask.revert')
        if isinstance(result, failure.Failure):
            return
        else:
            LOG.warning(_LW('CopySnapshotTask.revert: delete the '
                            'copied snapshot %(new_name)s of '
                            '%(source_name)s.'),
                        {'new_name': new_snap_name,
                         'source_name': src_snap_name})
            client.delete_snapshot(new_snap_name)


class ModifySnapshotTask(task.Task):
    """Task to modify a Snapshot."""
    def execute(self, client, new_snap_name, temp_snap,
                *args, **kwargs):
        LOG.debug('ModifySnapshotTask.execute')
        keep_for = None
        if temp_snap:
            keep_for = SNAP_EXPIRATION_HOUR
        client.modify_snapshot(new_snap_name,
                               allow_rw=True,
                               keep_for=keep_for)


class CreateConsistencyGroupTask(task.Task):
    """Task to create a consistency group."""
    def __init__(self, lun_id_key_template, num_of_members):
        self.lun_id_keys = sorted(set(
            [lun_id_key_template % i for i in range(num_of_members)]))
        super(CreateConsistencyGroupTask, self).__init__(
            requires=self.lun_id_keys)

    def execute(self, client, group, *args, **kwargs):
        LOG.debug('CreateConsistencyGroupTask.execute')
        lun_ids = [kwargs[key] for key in self.lun_id_keys]
        client.create_consistencygroup(group['id'], lun_ids,
                                       poll=True)


class WaitMigrationsCompleteTask(task.Task):
    """Task to wait migrations to be completed."""
    def __init__(self, lun_id_key_template, num_of_members):
        self.lun_id_keys = sorted(set(
            [lun_id_key_template % i for i in range(num_of_members)]))
        super(WaitMigrationsCompleteTask, self).__init__(
            requires=self.lun_id_keys)

    def execute(self, client, *args, **kwargs):
        LOG.debug('WaitMigrationsCompleteTask.execute')
        lun_ids = [kwargs[key] for key in self.lun_id_keys]
        for lun_id in lun_ids:
            migrated = client.verify_lun_migration(lun_id)
            if not migrated:
                msg = _("Migrate volume %(src)s failed.") % {'src': lun_id}
                raise exception.VolumeBackendAPIException(data=msg)


class MirrorView(object):
    """MirrorView synchronous/asynchronous operations.

    This class is to support operations for volume replication.
    Each operation should ensure commands are sent to correct targeting device.

    NOTE: currently, only synchronous is supported.
    """
    SYNCHRONIZE_MODE = ['sync']

    SYNCHRONIZED_STATE = 'Synchronized'
    CONSISTENT_STATE = 'Consistent'

    def __init__(self, client, secondary_client, mode='sync'):
        """Caller needs to initialize MirrorView via this method.

        :param client: client connecting to primary system
        :param secondary_client: client connecting to secondary system
        :param mode: only 'sync' is allowed
        """
        self._client = client
        self._secondary_client = secondary_client
        if mode not in self.SYNCHRONIZE_MODE:
            msg = _('Invalid synchronize mode specified, allowed '
                    'mode is %s.') % self.SYNCHRONIZE_MODE
            raise exception.VolumeBackendAPIException(
                data=msg)
        self.mode = '-sync'

    def create_mirror_workflow(self, mirror_name, lun_id, pool_name,
                               lun_name, lun_size, provisioning, tiering):
        """Creates mirror view for LUN."""
        store_spec = {'mirror': self}
        work_flow = self._get_create_mirror_flow(
            mirror_name, lun_id, pool_name,
            lun_name, lun_size, provisioning, tiering)
        flow_engine = taskflow.engines.load(work_flow, store=store_spec)
        flow_engine.run()

    def destroy_mirror_view(self, mirror_name, lun_name, mv=None):
        self.fracture_image(mirror_name, mv)
        self.remove_image(mirror_name, mv)
        self.destroy_mirror(mirror_name)
        self.delete_secondary_lun(lun_name)

    def _get_create_mirror_flow(self, mirror_name, lun_id, pool_name,
                                lun_name, lun_size, provisioning, tiering):
        """Gets mirror create flow."""
        flow_name = 'create_mirror_view'
        work_flow = linear_flow.Flow(flow_name)
        work_flow.add(MirrorCreateTask(mirror_name, lun_id),
                      MirrorSecLunCreateTask(pool_name, lun_name, lun_size,
                                             provisioning, tiering),
                      MirrorAddImageTask(mirror_name))
        return work_flow

    def create_mirror(self, name, primary_lun_id, poll=False):
        command_create = ('mirror', '-sync', '-create',
                          '-name', name, '-lun', primary_lun_id,
                          '-usewriteintentlog', '-o')
        out, rc = self._client.command_execute(*command_create, poll=poll)
        if rc != 0 or out.strip():
            if VNXError.has_error(out, VNXError.MIRROR_IN_USE):
                LOG.warning(_LW('MirrorView already created, mirror name '
                                '%(name)s. Message: %(msg)s'),
                            {'name': name, 'msg': out})
            else:
                self._client._raise_cli_error(cmd=command_create,
                                              rc=rc,
                                              out=out)
        return rc

    def create_secondary_lun(self, pool_name, lun_name, lun_size,
                             provisioning,
                             tiering, poll=False):
        """Creates secondary LUN in remote device."""
        data = self._secondary_client.create_lun_with_advance_feature(
            pool=pool_name,
            name=lun_name,
            size=lun_size,
            provisioning=provisioning,
            tiering=tiering,
            consistencygroup_id=None,
            ignore_thresholds=False,
            poll=poll)
        return data['lun_id']

    def delete_secondary_lun(self, lun_name):
        """Deletes secondary LUN in remote device."""
        self._secondary_client.delete_lun(lun_name)

    def destroy_mirror(self, name, poll=False):
        command_destroy = ('mirror', '-sync', '-destroy',
                           '-name', name, '-force', '-o')
        out, rc = self._client.command_execute(*command_destroy, poll=poll)
        if rc != 0 or out.strip():
            if VNXError.has_error(out, VNXError.MIRROR_NOT_FOUND):
                LOG.warning(_LW('MirrorView %(name)s was already deleted. '
                                'Message: %(msg)s'),
                            {'name': name, 'msg': out})
            else:
                self._client._raise_cli_error(cmd=command_destroy,
                                              rc=rc,
                                              out=out)
        return out, rc

    def add_image(self, name, secondary_lun_id, poll=False):
        """Adds secondary image to mirror."""
        secondary_array_ip = self._secondary_client.active_storage_ip
        command_add = ('mirror', '-sync', '-addimage',
                       '-name', name, '-arrayhost', secondary_array_ip,
                       '-lun', secondary_lun_id, '-recoverypolicy', 'auto',
                       '-syncrate', 'high')
        out, rc = self._client.command_execute(*command_add, poll=poll)
        if rc != 0:
            self._client._raise_cli_error(cmd=command_add,
                                          rc=rc,
                                          out=out)
        return out, rc

    def remove_image(self, name, mirror_view=None, poll=False):
        """Removes secondary image(s) from mirror."""
        if not mirror_view:
            mirror_view = self.get_image(name, poll=True)
        image_uid = self._extract_image_uid(mirror_view, 'secondary')
        command_remove = ('mirror', '-sync', '-removeimage',
                          '-name', name, '-imageuid', image_uid,
                          '-o')
        out, rc = self._client.command_execute(*command_remove, poll=poll)
        if rc != 0:
            self._client._raise_cli_error(cmd=command_remove,
                                          rc=rc,
                                          out=out)
        return out, rc

    def get_image(self, name, use_secondary=False, poll=False):
        """Returns mirror view properties.

        :param name: mirror view name
        :param use_secondary: get image info from secodnary or not
        :return: dict of mirror view properties as below:
            {
                'MirrorView Name': 'mirror name',
                'MirrorView Description': 'some desciption here',
                ...,
                'images': [
                    {
                        'Image UID': '50:06:01:60:88:60:08:0F',
                        'Is Image Primary': 'YES',
                        ...
                        'Preferred SP': 'A'
                    },
                    {
                        'Image UID': '50:06:01:60:88:60:03:BA',
                        'Is Image Primary': 'NO',
                        ...,
                        'Synchronizing Progress(%)': 100
                    }
                ]
            }
        """
        if use_secondary:
            client = self._secondary_client
        else:
            client = self._client
        command_get = ('mirror', '-sync', '-list', '-name', name)
        out, rc = client.command_execute(
            *command_get, poll=poll)
        if rc != 0:
            if VNXError.has_error(out, VNXError.MIRROR_NOT_FOUND):
                LOG.warning(_LW('Getting MirrorView %(name)s failed.'
                                ' Message: %(msg)s.'),
                            {'name': name, 'msg': out})
                return None
            else:
                client._raise_cli_error(cmd=command_get,
                                        rc=rc,
                                        out=out)
        mvs = {}
        mvs_info, images_info = re.split('Images:\s*', out)
        for line in mvs_info.strip('\n').split('\n'):
            k, v = re.split(':\s*', line, 1)
            mvs[k] = v if not v or re.search('\D', v) else int(v)
        mvs['images'] = []
        for image_raw in re.split('\n\n+', images_info.strip('\n')):
            image = {}
            for line in image_raw.split('\n'):
                k, v = re.split(':\s*', line, 1)
                image[k] = v if not v or re.search('\D', v) else int(v)
            mvs['images'].append(image)
        return mvs

    def fracture_image(self, name, mirror_view=None, poll=False):
        """Stops the synchronization between LUNs."""
        if not mirror_view:
            mirror_view = self.get_image(name, poll=True)
        image_uid = self._extract_image_uid(mirror_view, 'secondary')
        command_fracture = ('mirror', '-sync', '-fractureimage', '-name', name,
                            '-imageuid', image_uid, '-o')
        out, rc = self._client.command_execute(*command_fracture, poll=poll)
        if rc != 0:
            self._client._raise_cli_error(cmd=command_fracture,
                                          rc=rc,
                                          out=out)
        return out, rc

    def sync_image(self, name, mirror_view=None, poll=False):
        """Synchronizes the secondary image and wait for completion."""
        if not mirror_view:
            mirror_view = self.get_image(name, poll=True)
        image_state = mirror_view['images'][1]['Image State']
        if image_state == self.SYNCHRONIZED_STATE:
            LOG.debug('replication %(name)s is already in %(state)s.',
                      {'name': name, 'state': image_state})
            return "", 0
        image_uid = self._extract_image_uid(mirror_view, 'secondary')
        command_sync = ('mirror', '-sync', '-syncimage', '-name', name,
                        '-imageuid', image_uid, '-o')
        out, rc = self._client.command_execute(*command_sync, poll=poll)
        if rc != 0:
            self._client._raise_cli_error(cmd=command_sync,
                                          rc=rc,
                                          out=out)

        def inner():
            tmp_mirror = self.get_image(name)
            for image in tmp_mirror['images']:
                if 'Image State' in image:
                    # Only secondary image contains this field
                    return (image['Image State'] == self.SYNCHRONIZED_STATE and
                            image['Synchronizing Progress(%)'] == 100)
        self._client._wait_for_a_condition(inner)
        return out, rc

    def promote_image(self, name, mirror_view=None, poll=False):
        """Promotes the secondary image on secondary system.

        NOTE: Only when "Image State" in 'Consistent' or 'Synchnonized'
              can be promoted.
        """
        if not mirror_view:
            mirror_view = self.get_image(name, use_secondary=True, poll=True)
        image_uid = self._extract_image_uid(mirror_view, 'secondary')

        command_promote = ('mirror', '-sync', '-promoteimage', '-name', name,
                           '-imageuid', image_uid, '-o')
        out, rc = self._secondary_client.command_execute(
            *command_promote, poll=poll)
        if rc != 0:
            raise self._client._raise_cli_error(command_promote, rc, out)
        return out, rc

    def _extract_image_uid(self, mirror_view, image_type='primary'):
        """Returns primary or secondary image uid from mirror objects.

        :param mirror_view: parsed mirror view.
        :param image_type: 'primary' or 'secondary'.
        """
        images = mirror_view['images']
        for image in images:
            is_primary = image['Is Image Primary']
            if image_type == 'primary' and is_primary == 'YES':
                image_uid = image['Image UID']
                break
            if image_type == 'secondary' and is_primary == 'NO':
                image_uid = image['Image UID']
                break
        return image_uid


class MirrorCreateTask(task.Task):
    """Creates a MirrorView with primary lun for replication.

    Reversion strategy: Destroy the created MirrorView.
    """
    def __init__(self, mirror_name, primary_lun_id, **kwargs):
        super(MirrorCreateTask, self).__init__()
        self.mirror_name = mirror_name
        self.primary_lun_id = primary_lun_id

    def execute(self, mirror, *args, **kwargs):
        LOG.debug('%s.execute', self.__class__.__name__)
        mirror.create_mirror(self.mirror_name, self.primary_lun_id, poll=True)

    def revert(self, result, mirror, *args, **kwargs):
        method_name = '%s.revert' % self.__class__.__name__
        LOG.debug(method_name)
        if isinstance(result, failure.Failure):
            return
        else:
            LOG.warning(_LW('%(method)s: destroying mirror '
                            'view %(name)s.'),
                        {'method': method_name,
                         'name': self.mirror_name})
            mirror.destroy_mirror(self.mirror_name, poll=True)


class MirrorSecLunCreateTask(task.Task):
    """Creates a secondary LUN on secondary system.

    Reversion strategy: Delete secondary LUN.
    """
    def __init__(self, pool_name, lun_name, lun_size, provisioning, tiering):
        super(MirrorSecLunCreateTask, self).__init__(provides='sec_lun_id')
        self.pool_name = pool_name
        self.lun_name = lun_name
        self.lun_size = lun_size
        self.provisioning = provisioning
        self.tiering = tiering

    def execute(self, mirror, *args, **kwargs):
        LOG.debug('%s.execute', self.__class__.__name__)
        sec_lun_id = mirror.create_secondary_lun(
            self.pool_name, self.lun_name, self.lun_size,
            self.provisioning, self.tiering)
        return sec_lun_id

    def revert(self, result, mirror, *args, **kwargs):
        method_name = '%s.revert' % self.__class__.__name__
        LOG.debug(method_name)
        if isinstance(result, failure.Failure):
            return
        else:
            LOG.warning(_LW('%(method)s: destroying secondary LUN '
                            '%(name)s.'),
                        {'method': method_name,
                         'name': self.lun_name})
            mirror.delete_secondary_lun(self.lun_name)


class MirrorAddImageTask(task.Task):
    """Add the secondary image to MirrorView.

    Reversion strategy: Remove the secondary image.
    """
    def __init__(self, mirror_name):
        super(MirrorAddImageTask, self).__init__()
        self.mirror_name = mirror_name

    def execute(self, mirror, sec_lun_id, *args, **kwargs):
        LOG.debug('%s.execute', self.__class__.__name__)
        mirror.add_image(self.mirror_name, sec_lun_id, poll=True)

    def revert(self, result, mirror, *args, **kwargs):
        method_name = '%s.revert' % self.__class__.__name__
        LOG.debug(method_name)
        if isinstance(result, failure.Failure):
            return
        else:
            LOG.warning(_LW('%(method)s: removing secondary image '
                            'from %(name)s.'),
                        {'method': method_name,
                         'name': self.mirror_name})
            mirror.remove_image(self.mirror_name, poll=True)


def conditional_lock(lock=False, host=None):
    if lock:
        return lockutils.synchronized('emc-connection-' + host,
                                      'emc-connection-', True)
    else:
        return functools.partial
