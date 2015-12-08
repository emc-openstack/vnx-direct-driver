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
import os

import mock

from cinder import exception
from cinder.openstack.common import processutils
from cinder import test
from cinder.volume import configuration as conf
from cinder.volume.drivers.emc.emc_cli_fc import EMCCLIFCDriver
from cinder.volume.drivers.emc.emc_cli_iscsi import EMCCLIISCSIDriver
import cinder.volume.drivers.emc.emc_vnx_cli as emc_vnx_cli
from cinder.volume.drivers.emc.emc_vnx_cli import CommandLineHelper
from cinder.volume.drivers.emc.emc_vnx_cli import EMCVnxCLICmdError
from cinder.volume import volume_types


SUCCEED = ("", 0)
FAKE_ERROR_RETURN = ("FAKE ERROR", 255)


class EMCVNXCLIDriverTestData():

    test_volume = {
        'name': 'vol1',
        'size': 1,
        'volume_name': 'vol1',
        'id': '1',
        'provider_auth': None,
        'project_id': 'project',
        'display_name': 'vol1',
        'display_description': 'test volume',
        'volume_type_id': None}
    test_volume2 = {
        'name': 'vol2',
        'size': 1,
        'volume_name': 'vol2',
        'id': '1',
        'provider_auth': None,
        'project_id': 'project',
        'display_name': 'vol2',
        'display_description': 'test volume',
        'volume_type_id': None}

    test_volume_with_type = {
        'name': 'vol_with_type',
        'size': 1,
        'volume_name': 'vol_with_type',
        'id': '1',
        'provider_auth': None,
        'project_id': 'project',
        'display_name': 'thin_vol',
        'display_description': 'vol with type',
        'volume_type_id': 'abc1-2320-9013-8813-8941-1374-8112-1231'}

    test_failed_volume = {
        'name': 'failed_vol1',
        'size': 1,
        'volume_name': 'failed_vol1',
        'id': '4',
        'provider_auth': None,
        'project_id': 'project',
        'display_name': 'failed_vol',
        'display_description': 'test failed volume',
        'volume_type_id': None}

    test_volume1_in_sg = {
        'name': 'vol1_in_sg',
        'size': 1,
        'volume_name': 'vol1_in_sg',
        'id': '4',
        'provider_auth': None,
        'project_id': 'project',
        'display_name': 'failed_vol',
        'display_description': 'Volume 1 in SG',
        'volume_type_id': None,
        'provider_location': 'system^fakesn|type^lun|id^4'}

    test_volume2_in_sg = {
        'name': 'vol2_in_sg',
        'size': 1,
        'volume_name': 'vol2_in_sg',
        'id': '5',
        'provider_auth': None,
        'project_id': 'project',
        'display_name': 'failed_vol',
        'display_description': 'Volume 2 in SG',
        'volume_type_id': None,
        'provider_location': 'system^fakesn|type^lun|id^3'}

    test_snapshot = {
        'name': 'snapshot1',
        'size': 1,
        'id': '4444',
        'volume_name': 'vol1',
        'volume_size': 1,
        'project_id': 'project'}
    test_failed_snapshot = {
        'name': 'failed_snapshot',
        'size': 1,
        'id': '5555',
        'volume_name': 'vol-vol1',
        'volume_size': 1,
        'project_id': 'project'}
    test_clone = {
        'name': 'clone1',
        'size': 1,
        'id': '2',
        'volume_name': 'vol1',
        'provider_auth': None,
        'project_id': 'project',
        'display_name': 'clone1',
        'display_description': 'volume created from snapshot',
        'volume_type_id': None}
    connector = {
        'ip': '10.0.0.2',
        'initiator': 'iqn.1993-08.org.debian:01:222',
        'wwpns': ["123456789012345", "123456789054321"],
        'wwnns': ["223456789012345", "223456789054321"],
        'host': 'fakehost'}

    POOL_PROPERTY_CMD = ('storagepool', '-list', '-name', 'unit_test_pool',
                         '-userCap', '-availableCap')

    def SNAP_MP_CREATE_CMD(self, name='vol1', source='vol1'):
        return ('lun', '-create', '-type', 'snap', '-primaryLunName',
                source, '-name', name)

    def SNAP_ATTACH_CMD(self, name='vol1', snapName='snapshot1'):
        return ('lun', '-attach', '-name', name, '-snapName', snapName)

    def SNAP_DELETE_CMD(self, name):
        return ('snap', '-destroy', '-id', name, '-o')

    def SNAP_CREATE_CMD(self, name):
        return ('snap', '-create', '-res', 1, '-name', name,
                '-allowReadWrite', 'yes',
                '-allowAutoDelete', 'no')

    def LUN_DELETE_CMD(self, name):
        return ('lun', '-destroy', '-name', name, '-forceDetach', '-o')

    def LUN_CREATE_CMD(self, name, isthin=False):
        return ('lun', '-create', '-type', 'Thin' if isthin else 'NonThin',
                '-capacity', 1, '-sq', 'gb', '-poolName',
                'unit_test_pool', '-name', name)

    def LUN_EXTEND_CMD(self, name, newsize):
        return ('lun', '-expand', '-name', name, '-capacity', newsize,
                '-sq', 'gb', '-o', '-ignoreThresholds')

    def LUN_PROPERTY_ALL_CMD(self, lunname):
        return ('lun', '-list', '-name', lunname,
                '-state', '-opDetails', '-userCap', '-owner',
                '-attachedSnapshot')

    def MIGRATION_CMD(self, src_id=1, dest_id=1):
        return ("migrate", "-start", "-source", src_id, "-dest", dest_id,
                "-rate", "ASAP", "-o")

    def MIGRATION_VERIFY_CMD(self, src_id):
        return ("migrate", "-list", "-source", src_id)

    def GETPORT_CMD(self):
        return ("connection", "-getport", "-address", "-vlanid")

    def PINGNODE_CMD(self, sp, portid, vportid, ip):
        return ("connection", "-pingnode", "-sp", sp, '-portid', portid,
                "-vportid", vportid, "-address", ip, '-count', '1')

    def GETFCPORT_CMD(self):
        return ('port', '-list', '-sp')

    def CONNECTHOST_CMD(self, hostname, gname):
        return ('storagegroup', '-connecthost',
                '-host', hostname, '-gname', gname, '-o')

    def STORAGEGROUP_LIST_CMD(self, gname=None):
        if gname:
            return ('storagegroup', '-list', '-gname', gname,
                    '-host', '-iscsiAttributes')
        else:
            return ('storagegroup', '-list')

    def STORAGEGROUP_REMOVEHLU_CMD(self, gname, hlu):
        return ('storagegroup', '-removehlu',
                '-hlu', hlu, '-gname', gname, '-o')

    POOL_PROPERTY = ("""\
Pool Name:  unit_test_pool
Pool ID:  1
User Capacity (Blocks):  5769501696
User Capacity (GBs):  10000.5
Available Capacity (Blocks):  5676521472
Available Capacity (GBs):  1000.6
                        """, 0)

    ALL_PORTS = ("SP:  A\n" +
                 "Port ID:  4\n" +
                 "Port WWN:  iqn.1992-04.com.emc:cx.fnm00124000215.a4\n" +
                 "iSCSI Alias:  0215.a4\n\n" +
                 "Virtual Port ID:  0\n" +
                 "VLAN ID:  Disabled\n" +
                 "IP Address:  10.244.214.118\n\n" +
                 "SP:  A\n" +
                 "Port ID:  5\n" +
                 "Port WWN:  iqn.1992-04.com.emc:cx.fnm00124000215.a5\n" +
                 "iSCSI Alias:  0215.a5\n", 0)

    WHITE_LIST_PORTS = ("""SP:  A
Port ID:  0
Port WWN:  iqn.1992-04.com.emc:cx.fnmxxx.a0
iSCSI Alias:  0235.a7

Virtual Port ID:  0
VLAN ID:  Disabled
IP Address:  192.168.3.52

Virtual Port ID:  1
VLAN ID:  100
IP Address:  192.168.100.52

SP:  A
Port ID:  9
Port WWN:  iqn.1992-04.com.emc:cx.fnmxxx.a9
iSCSI Alias:  0235.a9

SP:  A
Port ID:  4
Port WWN:  iqn.1992-04.com.emc:cx.fnmxxx.a4
iSCSI Alias:  0235.a4

SP:  B
Port ID:  2
Port WWN:  iqn.1992-04.com.emc:cx.fnmxxx.b2
iSCSI Alias:  0235.b6

Virtual Port ID:  0
VLAN ID:  Disabled
IP Address:  192.168.4.53
""", 0)

    PING_OK = ("Reply from 10.0.0.2:  bytes=32 time=1ms TTL=30\n" +
               "Reply from 10.0.0.2:  bytes=32 time=1ms TTL=30\n" +
               "Reply from 10.0.0.2:  bytes=32 time=1ms TTL=30\n" +
               "Reply from 10.0.0.2:  bytes=32 time=1ms TTL=30\n", 0)

    FC_PORTS = ("Information about each SPPORT:\n" +
                "\n" +
                "SP Name:             SP A\n" +
                "SP Port ID:          0\n" +
                "SP UID:              50:06:01:60:88:60:01:95:" +
                "50:06:01:60:08:60:01:95\n" +
                "Link Status:         Up\n" +
                "Port Status:         Online\n" +
                "Switch Present:      YES\n" +
                "Switch UID:          10:00:00:05:1E:72:EC:A6:" +
                "20:46:00:05:1E:72:EC:A6\n" +
                "SP Source ID:        272896\n" +
                "\n" +
                "SP Name:             SP B\n" +
                "SP Port ID:          4\n" +
                "SP UID:              iqn.1992-04.com.emc:cx." +
                "fnm00124000215.b4\n" +
                "Link Status:         Up\n" +
                "Port Status:         Online\n" +
                "Switch Present:      Not Applicable\n" +
                "\n" +
                "SP Name:             SP A\n" +
                "SP Port ID:          2\n" +
                "SP UID:              50:06:01:60:88:60:01:95:" +
                "50:06:01:62:08:60:01:95\n" +
                "Link Status:         Down\n" +
                "Port Status:         Online\n" +
                "Switch Present:      NO\n" +
                "\n" +
                "SP Name:             SP B\n" +
                "SP Port ID:          2\n" +
                "SP UID:              50:06:01:60:88:60:08:0F:"
                "50:06:01:6A:08:60:08:0F\n" +
                "Link Status:         Up\n" +
                "Port Status:         Online\n" +
                "Switch Present:      YES\n" +
                "Switch UID:          10:00:50:EB:1A:03:3F:59:"
                "20:11:50:EB:1A:03:3F:59\n" +
                "SP Source ID:        69888\n", 0)

    def LUN_PROPERTY(self, name, is_thin=False, has_snap=False, size=1,
                     state='Ready', faulted='false', operation='None'):
        return ("""
               LOGICAL UNIT NUMBER 1
               Name:  %(name)s
               UID:  60:06:01:60:09:20:32:00:13:DF:B4:EF:C2:63:E3:11
               Current Owner:  SP A
               Default Owner:  SP A
               Allocation Owner:  SP A
               Attached Snapshot: %(has_snap)s
               User Capacity (Blocks):  2101346304
               User Capacity (GBs):  %(size)d
               Consumed Capacity (Blocks):  2149576704
               Consumed Capacity (GBs):  1024.998
               Pool Name:  unit_test_pool
               Current State:  %(state)s
               Status:  OK(0x0)
               Is Faulted:  %(faulted)s
               Is Transitioning:  false
               Current Operation:  %(operation)s
               Current Operation State:  N/A
               Current Operation Status:  N/A
               Current Operation Percent Completed:  0
               Is Thin LUN:  %(is_thin)s""" % {
            'name': name,
            'has_snap': 'FakeSnap' if has_snap else 'N/A',
            'size': size,
            'state': state,
            'faulted': faulted,
            'operation': operation,
            'is_thin': 'Yes' if is_thin else 'No'}, 0)

    def STORAGE_GROUP_NO_MAP(self, sgname):
        return ("""\
        Storage Group Name:    %s
        Storage Group UID:     27:D2:BE:C1:9B:A2:E3:11:9A:8D:FF:E5:3A:03:FD:6D
        Shareable:             YES""" % sgname, 0)

    def STORAGE_GROUP_ISCSI_FC_HBA(self, sgname):

        return ("""\
        Storage Group Name:    %s
        Storage Group UID:     54:46:57:0F:15:A2:E3:11:9A:8D:FF:E5:3A:03:FD:6D
        HBA/SP Pairs:

          HBA UID                                          SP Name     SPPort
          -------                                          -------     ------
          iqn.1993-08.org.debian:01:222                     SP A         4
        Host name:             fakehost
        SPPort:                A-4v0
        Initiator IP:          fakeip
        TPGT:                  3
        ISID:                  fakeid

          22:34:56:78:90:12:34:51:23:45:67:89:01:23:45      SP B         2
        Host name:             fakehost2
        SPPort:                B-2v0
        Initiator IP:          N/A
        TPGT:                  0
        ISID:                  N/A

          22:34:56:78:90:54:32:11:23:45:67:89:05:43:21      SP B         2
        Host name:             fakehost2
        SPPort:                B-2v0
        Initiator IP:          N/A
        TPGT:                  0
        ISID:                  N/A

        HLU/ALU Pairs:

          HLU Number     ALU Number
          ----------     ----------
            1               1
        Shareable:             YES""" % sgname, 0)

    def STORAGE_GROUP_HAS_MAP(self, sgname):

        return ("""\
        Storage Group Name:    %s
        Storage Group UID:     54:46:57:0F:15:A2:E3:11:9A:8D:FF:E5:3A:03:FD:6D
        HBA/SP Pairs:

          HBA UID                                          SP Name     SPPort
          -------                                          -------     ------
          iqn.1993-08.org.debian:01:222                     SP A         4
        Host name:             fakehost
        SPPort:                A-4v0
        Initiator IP:          fakeip
        TPGT:                  3
        ISID:                  fakeid

        HLU/ALU Pairs:

          HLU Number     ALU Number
          ----------     ----------
            1               1
        Shareable:             YES""" % sgname, 0)

    def STORAGE_GROUP_HAS_MAP_ISCSI(self, sgname):

        return ("""\
        Storage Group Name:    %s
        Storage Group UID:     54:46:57:0F:15:A2:E3:11:9A:8D:FF:E5:3A:03:FD:6D
        HBA/SP Pairs:

          HBA UID                                          SP Name     SPPort
          -------                                          -------     ------
          iqn.1993-08.org.debian:01:222                     SP A         2
        Host name:             fakehost
        SPPort:                A-2v0
        Initiator IP:          fakeip
        TPGT:                  3
        ISID:                  fakeid

          iqn.1993-08.org.debian:01:222                     SP A         0
        Host name:             fakehost
        SPPort:                A-0v0
        Initiator IP:          fakeip
        TPGT:                  3
        ISID:                  fakeid

          iqn.1993-08.org.debian:01:222                     SP B         2
        Host name:             fakehost
        SPPort:                B-2v0
        Initiator IP:          fakeip
        TPGT:                  3
        ISID:                  fakeid

        HLU/ALU Pairs:

          HLU Number     ALU Number
          ----------     ----------
            1               1
        Shareable:             YES""" % sgname, 0)

    def STORAGE_GROUP_HAS_MAP_2(self, sgname):

        return ("""\
        Storage Group Name:    %s
        Storage Group UID:     54:46:57:0F:15:A2:E3:11:9A:8D:FF:E5:3A:03:FD:6D
        HBA/SP Pairs:

          HBA UID                                          SP Name     SPPort
          -------                                          -------     ------
          iqn.1993-08.org.debian:01:222                     SP A         4
        Host name:             fakehost
        SPPort:                A-4v0
        Initiator IP:          fakeip
        TPGT:                  3
        ISID:                  fakeid

        HLU/ALU Pairs:

          HLU Number     ALU Number
          ----------     ----------
            1               1
            2               3
        Shareable:             YES""" % sgname, 0)

    def STORAGE_GROUP_HAS_MAP_3(self, sgname):

        return ("""\
        Storage Group Name:    %s
        Storage Group UID:     54:46:57:0F:15:A2:E3:11:9A:8D:FF:E5:3A:03:FD:6D
        HBA/SP Pairs:

          HBA UID                                          SP Name     SPPort
          -------                                          -------     ------
          iqn.1993-08.org.debian:01:222                     SP A         4
        Host name:             fakehost
        SPPort:                A-4v0
        Initiator IP:          fakeip
        TPGT:                  3
        ISID:                  fakeid

        HLU/ALU Pairs:

          HLU Number     ALU Number
          ----------     ----------
            3               3
            4               4
        Shareable:             YES""" % sgname, 0)

    def STORAGE_GROUPS_HAS_MAP(self, sgname1, sgname2):

        return ("""

        Storage Group Name:    irrelative
        Storage Group UID:     9C:86:4F:30:07:76:E4:11:AC:83:C8:C0:8E:9C:D6:1F
        HBA/SP Pairs:

          HBA UID                                          SP Name     SPPort
          -------                                          -------     ------
          iqn.1993-08.org.debian:01:5741c6307e60            SP A         6
        Host name:             fakehost
        SPPort:                A-6v0
        Initiator IP:          fakeip
        TPGT:                  3
        ISID:                  fakeid

        Storage Group Name:    %(sgname1)s
        Storage Group UID:     54:46:57:0F:15:A2:E3:11:9A:8D:FF:E5:3A:03:FD:6D
        HBA/SP Pairs:

          HBA UID                                          SP Name     SPPort
          -------                                          -------     ------
          iqn.1993-08.org.debian:01:222                     SP A         4
        Host name:             fakehost
        SPPort:                A-4v0
        Initiator IP:          fakeip
        TPGT:                  3
        ISID:                  fakeid

        HLU/ALU Pairs:

          HLU Number     ALU Number
          ----------     ----------
            31              3
            41              4
        Shareable:             YES

        Storage Group Name:    %(sgname2)s
        Storage Group UID:     9C:86:4F:30:07:76:E4:11:AC:83:C8:C0:8E:9C:D6:1F
        HBA/SP Pairs:

          HBA UID                                          SP Name     SPPort
          -------                                          -------     ------
          iqn.1993-08.org.debian:01:5741c6307e60            SP A         6
        Host name:             fakehost
        SPPort:                A-6v0
        Initiator IP:          fakeip
        TPGT:                  3
        ISID:                  fakeid

        HLU/ALU Pairs:

          HLU Number     ALU Number
          ----------     ----------
            32              3
            42              4
        Shareable:             YES""" % {'sgname1': sgname1,
                                         'sgname2': sgname2}, 0)

    def LUN_DELETE_IN_SG_ERROR(self, up_to_date=True):
        if up_to_date:
            return ("Cannot unbind LUN "
                    "because it's contained in a Storage Group",
                    156)
        else:
            return ("SP B: Request failed.  "
                    "Host LUN/LUN mapping still exists.",
                    0)

    def set_path_cmd(self, gname, hba, sp, spport, vport=None, ip=None):
        if vport is None:
            return ('storagegroup', '-setpath', '-gname', gname,
                    '-hbauid', hba,
                    '-sp', sp, '-spport', spport,
                    '-ip', ip, '-host', gname, '-o')
        return ('storagegroup', '-setpath', '-gname', gname,
                '-hbauid', hba,
                '-sp', sp, '-spport', spport, '-spvport', vport,
                '-ip', ip, '-host', gname, '-o')


class DriverTestCaseBase(test.TestCase):
    def setUp(self):
        super(DriverTestCaseBase, self).setUp()

        self.stubs.Set(CommandLineHelper, 'command_execute',
                       self.fake_setup_command_execute)
        self.stubs.Set(CommandLineHelper, 'get_array_serial',
                       mock.Mock(return_value={'array_serial':
                                               "fakeSerial"}))
        self.stubs.Set(os.path, 'exists', mock.Mock(return_value=1))

        self.stubs.Set(emc_vnx_cli, 'INTERVAL_1_SEC', 0.01)
        self.stubs.Set(emc_vnx_cli, 'INTERVAL_5_SEC', 0.01)
        self.stubs.Set(emc_vnx_cli, 'INTERVAL_10_SEC', 0.01)
        self.stubs.Set(emc_vnx_cli, 'INTERVAL_30_SEC', 0.01)
        self.stubs.Set(emc_vnx_cli, 'INTERVAL_60_SEC', 0.01)

        self.configuration = conf.Configuration(None)
        self.configuration.append_config_values = mock.Mock(return_value=0)
        self.configuration.naviseccli_path = '/opt/Navisphere/bin/naviseccli'
        self.configuration.san_ip = '10.0.0.1'
        self.configuration.storage_vnx_pool_name = 'unit_test_pool'
        self.configuration.san_login = 'sysadmin'
        self.configuration.san_password = 'sysadmin'
        self.configuration.storage_vnx_security_file_dir = None
        self.configuration.attach_detach_batch_interval = 10
        #set the timeout to 0.012s = 0.0002 * 60 = 1.2ms
        self.configuration.default_timeout = 0.0002
        self.configuration.initiator_auto_registration = True
        self.configuration.force_delete_lun_in_storagegroup = False
        self.stubs.Set(self.configuration, 'safe_get', self.fake_safe_get)
        self.testData = EMCVNXCLIDriverTestData()
        self.navisecclicmd = '/opt/Navisphere/bin/naviseccli ' + \
            '-address 10.0.0.1 -user sysadmin -password sysadmin -scope 0 '
        self.configuration.iscsi_initiators = '{"fakehost": ["10.0.0.2"]}'

    def driverSetup(self, commands=tuple(), results=tuple()):
        self.driver = self.generateDriver(self.configuration)
        fake_command_execute = self.get_command_execute_simulator(
            commands, results)
        fake_cli = mock.Mock(side_effect=fake_command_execute)
        self.driver.cli._client.command_execute = fake_cli
        return fake_cli

    def generateDriver(self, conf):
        raise NotImplementedError

    def get_command_execute_simulator(self, commands, results):
        assert(len(commands) == len(results))

        def fake_command_execute(*args, **kwargv):
            for i in range(len(commands)):
                if args == commands[i]:
                    if isinstance(results[i], list):
                        if len(results[i]) > 0:
                            ret = results[i][0]
                            del results[i][0]
                            return ret
                    else:
                        return results[i]
            return self.standard_fake_command_execute(*args, **kwargv)
        return fake_command_execute

    def standard_fake_command_execute(self, *args, **kwargv):
        standard_commands = [
            self.testData.LUN_PROPERTY_ALL_CMD('vol1'),
            self.testData.LUN_PROPERTY_ALL_CMD('vol2'),
            self.testData.LUN_PROPERTY_ALL_CMD('vol-vol1'),
            self.testData.LUN_PROPERTY_ALL_CMD('snapshot1'),
            self.testData.POOL_PROPERTY_CMD]

        standard_results = [
            self.testData.LUN_PROPERTY('vol1'),
            self.testData.LUN_PROPERTY('vol2'),
            self.testData.LUN_PROPERTY('vol-vol1'),
            self.testData.LUN_PROPERTY('snapshot1'),
            self.testData.POOL_PROPERTY]

        standard_default = SUCCEED
        for i in range(len(standard_commands)):
            if args == standard_commands[i]:
                return standard_results[i]

        return standard_default

    def fake_setup_command_execute(self, *command, **kwargv):
        return self.testData.ALL_PORTS

    def fake_safe_get(self, value):
        if value == "storage_vnx_pool_name":
            return "unit_test_pool"
        elif 'volume_backend_name' == value:
            return "namedbackend"
        else:
            return None


class EMCVNXCLIDriverISCSITestCase(DriverTestCaseBase):
    def generateDriver(self, conf):
        return EMCCLIISCSIDriver(configuration=conf)

    def test_create_destroy_volume_withoutExtraSpec(self):
        fake_cli = self.driverSetup()

        self.driver.create_volume(self.testData.test_volume)
        self.driver.delete_volume(self.testData.test_volume)
        expect_cmd = [mock.call(*self.testData.LUN_CREATE_CMD('vol1', False),
                                poll=False),
                      mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1'),
                                poll=False),
                      mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1'),
                                poll=False),
                      mock.call(*self.testData.LUN_DELETE_CMD('vol1'))]

        fake_cli.assert_has_calls(expect_cmd)

    def test_create_destroy_volume_withExtraSpec(self):
        extra_specs = {'storagetype:provisioning': 'Thin'}
        volume_types.get_volume_type_extra_specs = \
            mock.Mock(return_value=extra_specs)

        commands = [self.testData.LUN_PROPERTY_ALL_CMD('vol_with_type')]
        results = [self.testData.LUN_PROPERTY('vol_with_type', True)]
        fake_cli = self.driverSetup(commands, results)
        #case
        self.driver.create_volume(self.testData.test_volume_with_type)
        self.driver.delete_volume(self.testData.test_volume_with_type)

        #verification
        expect_cmd = [
            mock.call(*self.testData.LUN_CREATE_CMD('vol_with_type', True),
                      poll=False),
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol_with_type'),
                      poll=False),
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol_with_type'),
                      poll=False),
            mock.call(*self.testData.LUN_DELETE_CMD('vol_with_type'))]

        fake_cli.assert_has_calls(expect_cmd)

    def test_get_volume_stats(self):
        #expect_result = [POOL_PROPERTY]
        self.driverSetup()
        CommandLineHelper.get_array_serial = mock.Mock(
            return_value={'array_serial': "fakeSerial"})
        status = self.driver.get_volume_stats(True)
        self.assertTrue(status['driver_version'] is not None,
                        "dirver_version is not returned")
        self.assertTrue(
            status['free_capacity_gb'] == 1000.6,
            "free_capacity_gb is not correct")
        self.assertTrue(
            status['reserved_percentage'] == 0,
            "reserved_percentage is not correct")
        self.assertTrue(
            status['storage_protocol'] is not None,
            "storage_protocol is not correct")
        self.assertTrue(
            status['total_capacity_gb'] == 10000.5,
            "total_capacity_gb is not correct")
        self.assertTrue(
            status['vendor_name'] == "EMC",
            "vender name is not correct")
        self.assertTrue(
            status['volume_backend_name'] == "namedbackend",
            "volume backend name is not correct")
        self.assertTrue(status['location_info'] == "unit_test_pool|fakeSerial")

    @mock.patch("cinder.volume.drivers.emc.emc_vnx_cli."
                "CommandLineHelper.create_lun_and_wait",
                mock.Mock(return_value=True))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.EMCVnxCliBase.get_lun_id",
        mock.Mock(
            return_value=1))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.EMCVnxCliBase."
        "get_lun_id_by_name",
        mock.Mock(
            return_value=1))
    def test_volume_migration_timeout(self):
        commands = [self.testData.MIGRATION_CMD(),
                    self.testData.MIGRATION_VERIFY_CMD(1)]
        FAKE_ERROR_MSG = """\
A network error occurred while trying to connect: '10.244.213.142'.
Message : Error occurred because connection refused. \
Unable to establish a secure connection to the Management Server.
"""
        FAKE_ERROR_MSG = FAKE_ERROR_MSG.replace('\n', ' ')
        FAKE_MIGRATE_PROPETY = """\
Source LU Name:  volume-f6247ae1-8e1c-4927-aa7e-7f8e272e5c3d
Source LU ID:  63950
Dest LU Name:  volume-f6247ae1-8e1c-4927-aa7e-7f8e272e5c3d_dest
Dest LU ID:  136
Migration Rate:  ASAP
Current State:  MIGRATED
Percent Complete:  100
Time Remaining:  0 second(s)
"""
        results = [(FAKE_ERROR_MSG, 255),
                   [SUCCEED,
                    (FAKE_MIGRATE_PROPETY, 0),
                    ('The specified source LUN is not currently migrating',
                     23)]]
        fake_cli = self.driverSetup(commands, results)

        fakehost = {'capabilities': {'location_info':
                                     "unit_test_pool2|fakeSerial",
                                     'storage_protocol': 'iSCSI'}}
        ret = self.driver.migrate_volume(None, self.testData.test_volume,
                                         fakehost)[0]
        self.assertTrue(ret)
        #verification
        expect_cmd = [mock.call(*self.testData.MIGRATION_CMD(1, 1),
                                retry_disable=True),
                      mock.call(*self.testData.MIGRATION_VERIFY_CMD(1),
                                poll=True),
                      mock.call(*self.testData.MIGRATION_VERIFY_CMD(1),
                                poll=True)]
        fake_cli.assert_has_calls(expect_cmd)

    @mock.patch("cinder.volume.drivers.emc.emc_vnx_cli."
                "CommandLineHelper.create_lun_and_wait",
                mock.Mock(return_value=True))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.EMCVnxCliBase.get_lun_id",
        mock.Mock(
            side_effect=[1, 1]))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.EMCVnxCliBase."
        "get_lun_id_by_name",
        mock.Mock(return_value=1))
    def test_volume_migration(self):

        commands = [self.testData.MIGRATION_CMD(),
                    self.testData.MIGRATION_VERIFY_CMD(1)]
        FAKE_MIGRATE_PROPETY = """\
Source LU Name:  volume-f6247ae1-8e1c-4927-aa7e-7f8e272e5c3d
Source LU ID:  63950
Dest LU Name:  volume-f6247ae1-8e1c-4927-aa7e-7f8e272e5c3d_dest
Dest LU ID:  136
Migration Rate:  ASAP
Current State:  MIGRATED
Percent Complete:  100
Time Remaining:  0 second(s)
"""
        results = [SUCCEED, [(FAKE_MIGRATE_PROPETY, 0),
                             ('The specified source LUN is not '
                              'currently migrating',
                              23)]]
        fake_cli = self.driverSetup(commands, results)
        fakehost = {'capabilities': {'location_info':
                                     "unit_test_pool2|fakeSerial",
                                     'storage_protocol': 'iSCSI'}}
        ret = self.driver.migrate_volume(None, self.testData.test_volume,
                                         fakehost)[0]
        self.assertTrue(ret)
        #verification
        expect_cmd = [mock.call(*self.testData.MIGRATION_CMD(),
                                retry_disable=True),
                      mock.call(*self.testData.MIGRATION_VERIFY_CMD(1),
                                poll=True),
                      mock.call(*self.testData.MIGRATION_VERIFY_CMD(1),
                                poll=True)]
        fake_cli.assert_has_calls(expect_cmd)

    @mock.patch("cinder.volume.drivers.emc.emc_vnx_cli."
                "CommandLineHelper.create_lun_and_wait",
                mock.Mock(
                    return_value=True))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.EMCVnxCliBase."
        "get_lun_id_by_name",
        mock.Mock(
            return_value=1))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.EMCVnxCliBase.get_lun_id",
        mock.Mock(
            return_value=1))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.CommandLineHelper."
        "get_array_serial",
        mock.Mock(return_value={'array_serial':
                                "fakeSerial"}))
    def test_volume_migration_failed(self):
        commands = [self.testData.MIGRATION_CMD()]
        results = [FAKE_ERROR_RETURN]
        fake_cli = self.driverSetup(commands, results)

        fakehost = {'capabilities': {'location_info':
                                     "unit_test_pool2|fakeSerial",
                                     'storage_protocol': 'iSCSI'}}
        ret = self.driver.migrate_volume(None, self.testData.test_volume,
                                         fakehost)[0]
        self.assertFalse(ret)
        #verification
        expect_cmd = [mock.call(*self.testData.MIGRATION_CMD(),
                                retry_disable=True)]
        fake_cli.assert_has_calls(expect_cmd)

    @mock.patch("cinder.volume.drivers.emc.emc_vnx_cli."
                "CommandLineHelper.create_lun_and_wait",
                mock.Mock(
                    return_value=True))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.EMCVnxCliBase."
        "get_lun_id_by_name",
        mock.Mock(
            return_value=1))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.EMCVnxCliBase.get_lun_id",
        mock.Mock(
            return_value=1))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.CommandLineHelper."
        "get_array_serial",
        mock.Mock(return_value={'array_serial':
                                "fakeSerial"}))
    @mock.patch('time.sleep')
    def test_volume_migration_failed_retry(self, sleep_mock):
        commands = [self.testData.MIGRATION_CMD(),
                    self.testData.MIGRATION_VERIFY_CMD(1)]
        error_msg = ('Error: migrate -start command failed.\n' +
                     'The destination LUN is not available for migration.\n')
        results = [[(error_msg, 7), (error_msg, 7), SUCCEED],
                   ('The specified source LUN is not currently migrating', 23)]
        fake_cli = self.driverSetup(commands, results)

        fakehost = {'capabilities': {'location_info':
                                     "unit_test_pool2|fakeSerial",
                                     'storage_protocol': 'iSCSI'}}
        ret = self.driver.migrate_volume(
            None, self.testData.test_volume, fakehost)[0]
        self.assertTrue(ret)
        #verification
        expect_cmd = [mock.call(*self.testData.MIGRATION_CMD(),
                                retry_disable=True),
                      mock.call(*self.testData.MIGRATION_CMD(),
                                retry_disable=True),
                      mock.call(*self.testData.MIGRATION_CMD(),
                                retry_disable=True)]
        fake_cli.assert_has_calls(expect_cmd)

    def test_create_destroy_volume_snapshot(self):
        fake_cli = self.driverSetup()

        #case
        self.driver.create_snapshot(self.testData.test_snapshot)
        self.driver.delete_snapshot(self.testData.test_snapshot)

        #verification
        expect_cmd = [mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1'),
                                poll=True),
                      mock.call(*self.testData.SNAP_CREATE_CMD('snapshot1'),
                                poll=False),
                      mock.call(*self.testData.SNAP_DELETE_CMD('snapshot1'))]

        fake_cli.assert_has_calls(expect_cmd)

    def test_create_volume_cli_failed(self):
        commands = [self.testData.LUN_CREATE_CMD('failed_vol1')]
        results = [FAKE_ERROR_RETURN]
        fake_cli = self.driverSetup(commands, results)

        self.assertRaises(EMCVnxCLICmdError,
                          self.driver.create_volume,
                          self.testData.test_failed_volume)
        expect_cmd = [mock.call(*self.testData.LUN_CREATE_CMD('failed_vol1'),
                                poll=False)]
        fake_cli.assert_has_calls(expect_cmd)

    def test_create_faulted_volume(self):
        volume_name = 'faulted_volume'
        cmd_create = self.testData.LUN_CREATE_CMD(
            volume_name, False)
        cmd_list_preparing = self.testData.LUN_PROPERTY_ALL_CMD(volume_name)
        commands = [cmd_create, cmd_list_preparing]
        results = [SUCCEED,
                   [self.testData.LUN_PROPERTY(name=volume_name,
                                               state='Faulted',
                                               faulted='true',
                                               operation='Preparing'),
                    self.testData.LUN_PROPERTY(name=volume_name,
                                               state='Faulted',
                                               faulted='true',
                                               operation='None')]]
        fake_cli = self.driverSetup(commands, results)
        faulted_volume = self.testData.test_volume.copy()
        faulted_volume.update({'name': volume_name})
        self.driver.create_volume(faulted_volume)
        expect_cmd = [
            mock.call(*self.testData.LUN_CREATE_CMD(
                volume_name, False), poll=False),
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD(volume_name),
                      poll=False),
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD(volume_name),
                      poll=False)]
        fake_cli.assert_has_calls(expect_cmd)

    def test_create_offline_volume(self):
        volume_name = 'offline_volume'
        cmd_create = self.testData.LUN_CREATE_CMD(
            volume_name, False)
        cmd_list = self.testData.LUN_PROPERTY_ALL_CMD(volume_name)
        commands = [cmd_create, cmd_list]
        results = [SUCCEED,
                   self.testData.LUN_PROPERTY(name=volume_name,
                                              state='Offline',
                                              faulted='true')]
        self.driverSetup(commands, results)
        offline_volume = self.testData.test_volume.copy()
        offline_volume.update({'name': volume_name})
        self.assertRaisesRegexp(exception.VolumeBackendAPIException,
                                "Volume %s was created in VNX, but in"
                                " Offline state." % volume_name,
                                self.driver.create_volume,
                                offline_volume)

    def test_create_volume_snapshot_failed(self):
        commands = [self.testData.SNAP_CREATE_CMD('failed_snapshot')]
        results = [FAKE_ERROR_RETURN]
        fake_cli = self.driverSetup(commands, results)

        #case
        self.assertRaises(EMCVnxCLICmdError,
                          self.driver.create_snapshot,
                          self.testData.test_failed_snapshot)

        #verification
        expect_cmd = [
            mock.call(
                *self.testData.LUN_PROPERTY_ALL_CMD('vol-vol1'),
                poll=True),
            mock.call(
                *self.testData.SNAP_CREATE_CMD(
                    'failed_snapshot'), poll=False)]

        fake_cli.assert_has_calls(expect_cmd)

    def test_create_volume_from_snapshot(self):
        #set up
        cmd_smp = ('lun', '-list', '-name', 'vol2', '-attachedSnapshot')
        output_smp = ("""LOGICAL UNIT NUMBER 1
                     Name:  vol2
                     Attached Snapshot:  N/A""", 0)
        cmd_dest = self.testData.LUN_PROPERTY_ALL_CMD("vol2_dest")
        output_dest = self.testData.LUN_PROPERTY("vol2_dest")
        cmd_migrate = self.testData.MIGRATION_CMD(1, 1)
        output_migrate = ("", 0)
        cmd_migrate_verify = self.testData.MIGRATION_VERIFY_CMD(1)
        output_migrate_verify = (r'The specified source LUN '
                                 'is not currently migrating', 23)
        commands = [cmd_smp, cmd_dest, cmd_migrate, cmd_migrate_verify]
        results = [output_smp, output_dest, output_migrate,
                   output_migrate_verify]
        fake_cli = self.driverSetup(commands, results)

        self.driver.create_volume_from_snapshot(self.testData.test_volume2,
                                                self.testData.test_snapshot)
        expect_cmd = [
            mock.call(
                *self.testData.SNAP_MP_CREATE_CMD(
                    name='vol2', source='vol1')),
            mock.call(
                *self.testData.SNAP_ATTACH_CMD(
                    name='vol2', snapName='snapshot1')),
            mock.call(*self.testData.LUN_CREATE_CMD("vol2_dest"),
                      poll=False),
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol2_dest'),
                      poll=False),
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol2'),
                      poll=True),
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol2_dest'),
                      poll=True),
            mock.call(*self.testData.MIGRATION_CMD(1, 1),
                      retry_disable=True),
            mock.call(*self.testData.MIGRATION_VERIFY_CMD(1), poll=True),

            mock.call('lun', '-list', '-name', 'vol2', '-attachedSnapshot',
                      poll=True)]
        fake_cli.assert_has_calls(expect_cmd)

    def test_create_volume_from_snapshot_sync_failed(self):

        output_smp = ("""LOGICAL UNIT NUMBER 1
                    Name:  vol1
                    Attached Snapshot:  fakesnap""", 0)
        cmd_smp = ('lun', '-list', '-name', 'vol2', '-attachedSnapshot')
        cmd_dest = self.testData.LUN_PROPERTY_ALL_CMD("vol2_dest")
        output_dest = self.testData.LUN_PROPERTY("vol2_dest")
        cmd_migrate = self.testData.MIGRATION_CMD(1, 1)
        output_migrate = ("", 0)
        cmd_migrate_verify = self.testData.MIGRATION_VERIFY_CMD(1)
        output_migrate_verify = (r'The specified source LUN '
                                 'is not currently migrating', 23)
        commands = [cmd_smp, cmd_dest, cmd_migrate, cmd_migrate_verify]
        results = [output_smp, output_dest, output_migrate,
                   output_migrate_verify]
        fake_cli = self.driverSetup(commands, results)

        self.assertRaises(exception.VolumeBackendAPIException,
                          self.driver.create_volume_from_snapshot,
                          self.testData.test_volume2,
                          self.testData.test_snapshot)
        expect_cmd = [
            mock.call(
                *self.testData.SNAP_MP_CREATE_CMD(
                    name='vol2', source='vol1')),
            mock.call(
                *self.testData.SNAP_ATTACH_CMD(
                    name='vol2', snapName='snapshot1')),
            mock.call(*self.testData.LUN_CREATE_CMD("vol2_dest"),
                      poll=False),
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol2_dest'),
                      poll=False),
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol2_dest'),
                      poll=True),
            mock.call(*self.testData.MIGRATION_CMD(1, 1),
                      retry_disable=True),
            mock.call(*self.testData.MIGRATION_VERIFY_CMD(1),
                      poll=True),

            mock.call('lun', '-list', '-name', 'vol2', '-attachedSnapshot',
                      poll=True)]
        fake_cli.assert_has_calls(expect_cmd)

    def test_create_cloned_volume(self):
        cmd_smp = ('lun', '-list', '-name', 'vol1', '-attachedSnapshot')
        output_smp = ("""LOGICAL UNIT NUMBER 1
                     Name:  vol1
                     Attached Snapshot:  N/A""", 0)
        cmd_dest = self.testData.LUN_PROPERTY_ALL_CMD("vol1_dest")
        output_dest = self.testData.LUN_PROPERTY("vol1_dest")
        cmd_migrate = self.testData.MIGRATION_CMD(1, 1)
        output_migrate = ("", 0)
        cmd_migrate_verify = self.testData.MIGRATION_VERIFY_CMD(1)
        output_migrate_verify = (r'The specified source LUN '
                                 'is not currently migrating', 23)
        commands = [cmd_smp, cmd_dest, cmd_migrate, cmd_migrate_verify]
        results = [output_smp, output_dest, output_migrate,
                   output_migrate_verify]
        fake_cli = self.driverSetup(commands, results)

        self.driver.create_cloned_volume(self.testData.test_volume,
                                         self.testData.test_snapshot)
        tmp_snap = 'tmp-snap-' + self.testData.test_volume['id']
        expect_cmd = [
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('snapshot1'),
                      poll=True),
            mock.call(
                *self.testData.SNAP_CREATE_CMD(tmp_snap), poll=False),
            mock.call(*self.testData.SNAP_MP_CREATE_CMD(name='vol1',
                                                        source='snapshot1')),
            mock.call(
                *self.testData.SNAP_ATTACH_CMD(
                    name='vol1', snapName=tmp_snap)),
            mock.call(*self.testData.LUN_CREATE_CMD("vol1_dest"), poll=False),
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1_dest'),
                      poll=False),
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1'),
                      poll=True),
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1_dest'),
                      poll=True),
            mock.call(*self.testData.MIGRATION_CMD(1, 1),
                      retry_disable=True),
            mock.call(*self.testData.MIGRATION_VERIFY_CMD(1),
                      poll=True),
            mock.call('lun', '-list', '-name', 'vol1', '-attachedSnapshot',
                      poll=True),
            mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1'), poll=True),
            mock.call(*self.testData.SNAP_DELETE_CMD(tmp_snap))]
        fake_cli.assert_has_calls(expect_cmd)

    def test_delete_volume_failed(self):
        commands = [self.testData.LUN_DELETE_CMD('failed_vol1')]
        results = [FAKE_ERROR_RETURN]
        fake_cli = self.driverSetup(commands, results)

        self.assertRaises(EMCVnxCLICmdError,
                          self.driver.delete_volume,
                          self.testData.test_failed_volume)
        expected = [mock.call(*self.testData.LUN_DELETE_CMD('failed_vol1'))]
        fake_cli.assert_has_calls(expected)

    def test_delete_volume_in_sg_failed(self):
        commands = [self.testData.LUN_DELETE_CMD('vol1_in_sg'),
                    self.testData.LUN_DELETE_CMD('vol2_in_sg')]
        results = [self.testData.LUN_DELETE_IN_SG_ERROR(),
                   self.testData.LUN_DELETE_IN_SG_ERROR(False)]
        fake_cli = self.driverSetup(commands, results)
        self.assertRaises(EMCVnxCLICmdError,
                          self.driver.delete_volume,
                          self.testData.test_volume1_in_sg)
        self.assertRaises(EMCVnxCLICmdError,
                          self.driver.delete_volume,
                          self.testData.test_volume2_in_sg)

    def test_delete_volume_in_sg_force(self):
        commands = [self.testData.LUN_DELETE_CMD('vol1_in_sg'),
                    self.testData.STORAGEGROUP_LIST_CMD(),
                    self.testData.STORAGEGROUP_REMOVEHLU_CMD('fakehost1',
                                                             '41'),
                    self.testData.STORAGEGROUP_REMOVEHLU_CMD('fakehost1',
                                                             '42'),
                    self.testData.LUN_DELETE_CMD('vol2_in_sg'),
                    self.testData.STORAGEGROUP_REMOVEHLU_CMD('fakehost2',
                                                             '31'),
                    self.testData.STORAGEGROUP_REMOVEHLU_CMD('fakehost2',
                                                             '32')]
        results = [[self.testData.LUN_DELETE_IN_SG_ERROR(),
                    SUCCEED],
                   self.testData.STORAGE_GROUPS_HAS_MAP('fakehost1',
                                                        'fakehost2'),
                   SUCCEED,
                   SUCCEED,
                   [self.testData.LUN_DELETE_IN_SG_ERROR(False),
                    SUCCEED],
                   SUCCEED,
                   SUCCEED]
        fake_cli = self.driverSetup(commands, results)
        self.driver.cli.force_delete_lun_in_sg = True
        self.driver.delete_volume(self.testData.test_volume1_in_sg)
        self.driver.delete_volume(self.testData.test_volume2_in_sg)
        expected = [mock.call(*self.testData.LUN_DELETE_CMD('vol1_in_sg')),
                    mock.call(*self.testData.STORAGEGROUP_LIST_CMD(),
                              poll=True),
                    mock.call(*self.testData.STORAGEGROUP_REMOVEHLU_CMD(
                        'fakehost1', '41'), poll=False),
                    mock.call(*self.testData.STORAGEGROUP_REMOVEHLU_CMD(
                        'fakehost2', '42'), poll=False),
                    mock.call(*self.testData.LUN_DELETE_CMD('vol1_in_sg')),
                    mock.call(*self.testData.LUN_DELETE_CMD('vol2_in_sg')),
                    mock.call(*self.testData.STORAGEGROUP_LIST_CMD(),
                              poll=True),
                    mock.call(*self.testData.STORAGEGROUP_REMOVEHLU_CMD(
                        'fakehost1', '31'), poll=False),
                    mock.call(*self.testData.STORAGEGROUP_REMOVEHLU_CMD(
                        'fakehost2', '32'), poll=False),
                    mock.call(*self.testData.LUN_DELETE_CMD('vol2_in_sg'))]
        fake_cli.assert_has_calls(expected)

    def test_extend_volume(self):
        commands = [self.testData.LUN_PROPERTY_ALL_CMD('vol1')]
        results = [self.testData.LUN_PROPERTY('vol1', size=2)]
        fake_cli = self.driverSetup(commands, results)

        # case
        self.driver.extend_volume(self.testData.test_volume, 2)
        expected = [mock.call(*self.testData.LUN_EXTEND_CMD('vol1', 2)),
                    mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1'),
                              poll=True)]
        fake_cli.assert_has_calls(expected)

    def test_extend_volume_has_snapshot(self):
        commands = [self.testData.LUN_EXTEND_CMD('failed_vol1', 2)]
        results = [FAKE_ERROR_RETURN]
        fake_cli = self.driverSetup(commands, results)

        self.assertRaises(EMCVnxCLICmdError,
                          self.driver.extend_volume,
                          self.testData.test_failed_volume,
                          2)
        expected = [mock.call(*self.testData.LUN_EXTEND_CMD('failed_vol1', 2))]
        fake_cli.assert_has_calls(expected)

    def test_extend_volume_failed(self):
        commands = [self.testData.LUN_PROPERTY_ALL_CMD('failed_vol1')]
        results = [self.testData.LUN_PROPERTY('failed_vol1', size=2)]
        fake_cli = self.driverSetup(commands, results)

        self.assertRaises(exception.VolumeBackendAPIException,
                          self.driver.extend_volume,
                          self.testData.test_failed_volume,
                          3)
        expected = [
            mock.call(
                *self.testData.LUN_EXTEND_CMD('failed_vol1', 3)),
            mock.call(
                *self.testData.LUN_PROPERTY_ALL_CMD('failed_vol1'),
                poll=True)]
        fake_cli.assert_has_calls(expected)

    def fake_get_pool_properties(self, filter_option, properties=None):
        pool_info = {'pool_name': "unit_test_pool0",
                     'total_capacity_gb': 1000.0,
                     'free_capacity_gb': 1000.0
                     }
        return pool_info

    def fake_get_lun_properties(self, filter_option, properties=None):
        lun_info = {'lun_name': "vol1",
                    'lun_id': 1,
                    'pool': "unit_test_pool",
                    'attached_snapshot': "N/A",
                    'owner': "A",
                    'total_capacity_gb': 1.0,
                    'state': "Ready"}
        return lun_info


class EMCVNXCLIDriverFCTestCase(DriverTestCaseBase):
    def generateDriver(self, conf):
        return EMCCLIFCDriver(configuration=conf)

    def fake_get_pool_properties(self, filter_option, properties=None):
        pool_info = {'pool_name': "unit_test_pool0",
                     'total_capacity_gb': 1000.0,
                     'free_capacity_gb': 1000.0
                     }
        return pool_info

    def fake_get_lun_properties(self, filter_option, properties=None):
        lun_info = {'lun_name': "vol1",
                    'lun_id': 1,
                    'pool': "unit_test_pool",
                    'attached_snapshot': "N/A",
                    'owner': "A",
                    'total_capacity_gb': 1.0,
                    'state': "Ready"}
        return lun_info


class EMCVNXCLIToggleSPTestData():
    def FAKE_COMMAND_PREFIX(self, sp_address):
        return ('/opt/Navisphere/bin/naviseccli', '-address', sp_address,
                '-user', 'sysadmin', '-password', 'sysadmin',
                '-scope', 'global')


class EMCVNXCLIToggleSPTestCase(test.TestCase):
    def setUp(self):
        super(EMCVNXCLIToggleSPTestCase, self).setUp()
        self.stubs.Set(os.path, 'exists', mock.Mock(return_value=1))
        self.configuration = mock.Mock(conf.Configuration)
        self.configuration.naviseccli_path = '/opt/Navisphere/bin/naviseccli'
        self.configuration.san_ip = '10.10.10.10'
        self.configuration.san_secondary_ip = "10.10.10.11"
        self.configuration.storage_vnx_pool_name = 'unit_test_pool'
        self.configuration.san_login = 'sysadmin'
        self.configuration.san_password = 'sysadmin'
        self.configuration.default_timeout = 1
        self.configuration.max_luns_per_storage_group = 10
        self.configuration.destroy_empty_storage_group = 10
        self.configuration.storage_vnx_authentication_type = "global"
        self.configuration.iscsi_initiators = '{"fakehost": ["10.0.0.2"]}'
        self.configuration.zoning_mode = None
        self.configuration.storage_vnx_security_file_dir = ""
        self.cli_client = emc_vnx_cli.CommandLineHelper(
            configuration=self.configuration)
        self.test_data = EMCVNXCLIToggleSPTestData()

    def test_no_sp_toggle(self):
        self.cli_client.active_storage_ip = '10.10.10.10'
        FAKE_SUCCESS_RETURN = ('success', 0)
        FAKE_COMMAND = ('list', 'pool')
        SIDE_EFFECTS = [FAKE_SUCCESS_RETURN]

        with mock.patch('cinder.utils.execute') as mock_utils:
            mock_utils.side_effect = SIDE_EFFECTS
            self.cli_client.command_execute(*FAKE_COMMAND)
            self.assertEqual("10.10.10.10", self.cli_client.active_storage_ip)
            expected = [
                mock.call(*(self.test_data.FAKE_COMMAND_PREFIX('10.10.10.10')
                          + FAKE_COMMAND), check_exit_code=True)]
            mock_utils.assert_has_calls(expected)

    def test_toggle_sp_with_server_unavailabe(self):
        self.cli_client.active_storage_ip = '10.10.10.10'
        FAKE_ERROR_MSG = """\
Error occurred during HTTP request/response from the target: '10.244.213.142'.
Message : HTTP/1.1 503 Service Unavailable"""
        FAKE_SUCCESS_RETURN = ('success', 0)
        FAKE_COMMAND = ('list', 'pool')
        SIDE_EFFECTS = [processutils.ProcessExecutionError(
            exit_code=255, stdout=FAKE_ERROR_MSG),
            FAKE_SUCCESS_RETURN]

        with mock.patch('cinder.utils.execute') as mock_utils:
            mock_utils.side_effect = SIDE_EFFECTS
            self.cli_client.command_execute(*FAKE_COMMAND)
            self.assertEqual("10.10.10.11", self.cli_client.active_storage_ip)
            expected = [
                mock.call(
                    *(self.test_data.FAKE_COMMAND_PREFIX('10.10.10.10')
                        + FAKE_COMMAND),
                    check_exit_code=True),
                mock.call(
                    *(self.test_data.FAKE_COMMAND_PREFIX('10.10.10.11')
                        + FAKE_COMMAND),
                    check_exit_code=True)]
            mock_utils.assert_has_calls(expected)

    def test_toggle_sp_with_end_of_data(self):
        self.cli_client.active_storage_ip = '10.10.10.10'
        FAKE_ERROR_MSG = """\
Error occurred during HTTP request/response from the target: '10.244.213.142'.
Message : End of data stream"""
        FAKE_SUCCESS_RETURN = ('success', 0)
        FAKE_COMMAND = ('list', 'pool')
        SIDE_EFFECTS = [processutils.ProcessExecutionError(
            exit_code=255, stdout=FAKE_ERROR_MSG),
            FAKE_SUCCESS_RETURN]

        with mock.patch('cinder.utils.execute') as mock_utils:
            mock_utils.side_effect = SIDE_EFFECTS
            self.cli_client.command_execute(*FAKE_COMMAND)
            self.assertEqual("10.10.10.11", self.cli_client.active_storage_ip)
            expected = [
                mock.call(
                    *(self.test_data.FAKE_COMMAND_PREFIX('10.10.10.10')
                        + FAKE_COMMAND),
                    check_exit_code=True),
                mock.call(
                    *(self.test_data.FAKE_COMMAND_PREFIX('10.10.10.11')
                        + FAKE_COMMAND),
                    check_exit_code=True)]
            mock_utils.assert_has_calls(expected)

    def test_toggle_sp_with_connection_refused(self):
        self.cli_client.active_storage_ip = '10.10.10.10'
        FAKE_ERROR_MSG = """\
A network error occurred while trying to connect: '10.244.213.142'.
Message : Error occurred because connection refused. \
Unable to establish a secure connection to the Management Server.
"""
        FAKE_SUCCESS_RETURN = ('success', 0)
        FAKE_COMMAND = ('list', 'pool')
        SIDE_EFFECTS = [processutils.ProcessExecutionError(
            exit_code=255, stdout=FAKE_ERROR_MSG),
            FAKE_SUCCESS_RETURN]

        with mock.patch('cinder.utils.execute') as mock_utils:
            mock_utils.side_effect = SIDE_EFFECTS
            self.cli_client.command_execute(*FAKE_COMMAND)
            self.assertEqual("10.10.10.11", self.cli_client.active_storage_ip)
            expected = [
                mock.call(
                    *(self.test_data.FAKE_COMMAND_PREFIX('10.10.10.10')
                        + FAKE_COMMAND),
                    check_exit_code=True),
                mock.call(
                    *(self.test_data.FAKE_COMMAND_PREFIX('10.10.10.11')
                        + FAKE_COMMAND),
                    check_exit_code=True)]
            mock_utils.assert_has_calls(expected)

    def test_toggle_sp_with_connection_error(self):
        self.cli_client.active_storage_ip = '10.10.10.10'
        FAKE_ERROR_MSG = """\
A network error occurred while trying to connect: '192.168.1.56'.
Message : Error occurred because of time out"""
        FAKE_SUCCESS_RETURN = ('success', 0)
        FAKE_COMMAND = ('list', 'pool')
        SIDE_EFFECTS = [processutils.ProcessExecutionError(
            exit_code=255, stdout=FAKE_ERROR_MSG),
            FAKE_SUCCESS_RETURN]

        with mock.patch('cinder.utils.execute') as mock_utils:
            mock_utils.side_effect = SIDE_EFFECTS
            self.cli_client.command_execute(*FAKE_COMMAND)
            self.assertEqual("10.10.10.11", self.cli_client.active_storage_ip)
            expected = [
                mock.call(
                    *(self.test_data.FAKE_COMMAND_PREFIX('10.10.10.10')
                        + FAKE_COMMAND),
                    check_exit_code=True),
                mock.call(
                    *(self.test_data.FAKE_COMMAND_PREFIX('10.10.10.11')
                        + FAKE_COMMAND),
                    check_exit_code=True)]
            mock_utils.assert_has_calls(expected)


class BatchAttachDetachTestCaseBase(DriverTestCaseBase):
    def generateAddOrder(self, **kwargs):
        order = {'alu': 0,
                 'type': emc_vnx_cli.BatchOrderType.ADD,
                 'status': emc_vnx_cli.AddHluStatus.NEW,
                 'tried': 0,
                 'hlu': None,
                 'msg': '',
                 'payload': None}
        order.update(kwargs)
        return order

    def generateRemoveOrder(self, **kwargs):
        order = {'alu': 0,
                 'type': emc_vnx_cli.BatchOrderType.REMOVE,
                 'status': emc_vnx_cli.RemoveHluStatus.NEW,
                 'tried': 0,
                 'hlu': None,
                 'msg': '',
                 'payload': None}
        order.update(kwargs)
        return order


@mock.patch("random.shuffle",
            mock.Mock(return_value=0))
class BatchAttachDetachISCSITestCase(BatchAttachDetachTestCaseBase):
    def generateDriver(self, conf):
        return EMCCLIISCSIDriver(configuration=conf)

    def generateAddOrder(self, **kwargs):
        order = {'alu': 0,
                 'type': emc_vnx_cli.BatchOrderType.ADD,
                 'status': emc_vnx_cli.AddHluStatus.NEW,
                 'tried': 0,
                 'hlu': None,
                 'msg': '',
                 'payload': None}
        order.update(kwargs)
        return order

    def generateRemoveOrder(self, **kwargs):
        order = {'alu': 0,
                 'type': emc_vnx_cli.BatchOrderType.REMOVE,
                 'status': emc_vnx_cli.RemoveHluStatus.NEW,
                 'tried': 0,
                 'hlu': None,
                 'msg': '',
                 'payload': None}
        order.update(kwargs)
        return order

    @mock.patch(
        "cinder.openstack.common.processutils.execute",
        mock.Mock(
            return_value=(
                "fakeportal iqn.1992-04.fake.com:fake.apm00123907237.a8", 0)))
    def test_initialize_connection_withPingNode(self):
        test_volume = self.testData.test_volume.copy()
        test_volume['provider_location'] = 'system^fakesn|type^lun|id^1'
        td = self.testData
        commands = [self.testData.PINGNODE_CMD('A', 4, 0, '10.0.0.2')]
        results = [self.testData.PING_OK]
        fake_cli = self.driverSetup(commands, results)

        class fake_batch_worker(object):
            def submit(self, order):
                order['status'] = emc_vnx_cli.AddHluStatus.OK
                order['hlu'] = 3
                order['payload'] = {
                    'raw_output': td.STORAGE_GROUP_HAS_MAP('fakehost')[0]
                }

        with mock.patch('cinder.volume.drivers.emc.emc_vnx_cli.'
                        'EMCVnxCliBase.get_batch_worker',
                        mock.Mock(return_value=fake_batch_worker())):
            iscsi_data = self.driver.initialize_connection(
                test_volume,
                self.testData.connector)
            self.assertTrue(iscsi_data['data']['target_lun'] == 3,
                            "iSCSI initialize connection returned wrong HLU")
            expected = [mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1'),
                                  poll=False),
                        mock.call(*self.testData.PINGNODE_CMD('A', 4, 0,
                                                              '10.0.0.2'))]
            fake_cli.assert_has_calls(expected)

    @mock.patch(
        "cinder.openstack.common.processutils.execute",
        mock.Mock(
            return_value=(
                "fakeportal iqn.1992-04.fake.com:fake.apm00123907237.a8", 0)))
    def test_initialize_connection_noPingNode(self):
        test_volume = self.testData.test_volume.copy()
        test_volume['provider_location'] = 'system^fakesn|type^lun|id^1'
        td = self.testData
        old_iscsi_initiators = self.configuration.iscsi_initiators
        self.configuration.iscsi_initiators = ''
        fake_cli = self.driverSetup([], [])

        class fake_batch_worker(object):
            def submit(self, order):
                order['status'] = emc_vnx_cli.AddHluStatus.OK
                order['hlu'] = 3
                order['payload'] = {
                    'raw_output':
                    td.STORAGE_GROUP_HAS_MAP('fakehost')[0]
                }

        with mock.patch('cinder.volume.drivers.emc.emc_vnx_cli.'
                        'EMCVnxCliBase.get_batch_worker',
                        mock.Mock(return_value=fake_batch_worker())):
            iscsi_data = self.driver.initialize_connection(
                test_volume,
                self.testData.connector)
            self.assertTrue(iscsi_data['data']['target_lun'] == 3,
                            "iSCSI initialize connection returned wrong HLU")
            expected = [mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1'),
                                  poll=False)]
            fake_cli.assert_has_calls(expected)

        self.configuration.iscsi_initiators = old_iscsi_initiators

    @mock.patch(
        "cinder.openstack.common.processutils.execute",
        mock.Mock(
            return_value=(
                "fakeportal iqn.1992-04.fake.com:fake.apm00123907237.a8", 0)))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.EMCVnxCliBase.get_lun_id",
        mock.Mock(
            return_value=4))
    def test_initialize_connection_no_hlu_left(self):
        """There is no usable hlu for the SG
        """
        self.driverSetup([], [])

        class fake_batch_worker(object):
            def submit(self, order):
                order['status'] = emc_vnx_cli.AddHluStatus.NO_HLU_LEFT

        with mock.patch('cinder.volume.drivers.emc.emc_vnx_cli.'
                        'EMCVnxCliBase.get_batch_worker',
                        mock.Mock(return_value=fake_batch_worker())):
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.initialize_connection,
                              self.testData.test_volume,
                              self.testData.connector)

    @mock.patch(
        "cinder.openstack.common.processutils.execute",
        mock.Mock(
            return_value=(
                "fakeportal iqn.1992-04.fake.com:fake.apm00123907237.a8", 0)))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.EMCVnxCliBase.get_lun_id",
        mock.Mock(
            return_value=4))
    def test_initialize_connection_failure(self):
        """There is no usable hlu for the SG
        """
        self.driverSetup([], [])

        class fake_batch_worker(object):
            def submit(self, order):
                order['status'] = emc_vnx_cli.AddHluStatus.FAILURE

        with mock.patch('cinder.volume.drivers.emc.emc_vnx_cli.'
                        'EMCVnxCliBase.get_batch_worker',
                        mock.Mock(return_value=fake_batch_worker())):
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.initialize_connection,
                              self.testData.test_volume,
                              self.testData.connector)

    def test_terminate_connection_failure(self):
        self.driverSetup([], [])

        class fake_batch_worker(object):
            def submit(self, order):
                order['status'] = emc_vnx_cli.RemoveHluStatus.FAILURE

        with mock.patch('cinder.volume.drivers.emc.emc_vnx_cli.'
                        'EMCVnxCliBase.get_batch_worker',
                        mock.Mock(return_value=fake_batch_worker())):
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.terminate_connection,
                              self.testData.test_volume,
                              self.testData.connector)

    def test_terminate_connection(self):

        self.driverSetup([], [])

        class fake_batch_worker(object):
            def submit(self, order):
                order['status'] = emc_vnx_cli.RemoveHluStatus.HLU_NOT_IN_SG

        with mock.patch('cinder.volume.drivers.emc.emc_vnx_cli.'
                        'EMCVnxCliBase.get_batch_worker',
                        mock.Mock(return_value=fake_batch_worker())):
            self.driver.terminate_connection(
                self.testData.test_volume,
                self.testData.connector)

        class fake_batch_worker_not_in_SG(object):
            def submit(self, order):
                order['status'] = emc_vnx_cli.RemoveHluStatus.HLU_NOT_IN_SG

        with mock.patch('cinder.volume.drivers.emc.emc_vnx_cli.'
                        'EMCVnxCliBase.get_batch_worker',
                        mock.Mock(return_value=fake_batch_worker())):
            self.driver.terminate_connection(
                self.testData.test_volume,
                self.testData.connector)

    def test_handle_order_in_batch(self):
        orderList = []
        orderList.append(self.generateAddOrder(alu=1))
        orderList.append(self.generateAddOrder(alu=2))
        orderList.append(self.generateRemoveOrder(alu=3))
        orderList.append(self.generateRemoveOrder(alu=4))
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('storagegroup', '-addhlu', '-gname', 'fakehost',
                     '-hlus', '255,254', '-verboseStatus',
                     '-alus', '1,2', '-verboseStatus',
                     '-o'),
                    ('storagegroup', '-removehlu', '-gname', 'fakehost',
                     '-hlus', '3,4', '-verboseStatus',
                     '-o')]
        results = [self.testData.STORAGE_GROUP_HAS_MAP_3('fakehost'),
                   ("LUs Added: 1,2.", 0),
                   ("LUs Removed: 3,4.", 0)]

        fake_cli = self.driverSetup(commands, results)

        executor = self.driver.cli.get_attach_detach_batch_executor(
            self.testData.connector
        )

        executor(orderList)
        self.assertTrue(orderList[0]['status'] == emc_vnx_cli.AddHluStatus.OK
                        and
                        orderList[0]['hlu'] == 255,
                        "add hlu in batch failed")
        self.assertTrue(orderList[1]['status'] == emc_vnx_cli.AddHluStatus.OK
                        and
                        orderList[1]['hlu'] == 254,
                        "add hlu in batch failed")
        self.assertTrue(orderList[2]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.OK,
                        "remove hlu in batch failed")
        self.assertTrue(orderList[3]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.OK,
                        "remove hlu in batch failed")
        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-addhlu', '-gname', 'fakehost',
                              '-hlus', '255,254', '-verboseStatus', '-alus',
                              '1,2', '-verboseStatus', '-o',
                              poll=False,
                              retry_disable=True),
                    mock.call('storagegroup', '-removehlu', '-gname',
                              'fakehost', '-hlus', '3,4', '-verboseStatus',
                              '-o',
                              poll=False,
                              retry_disable=True)]

        fake_cli.assert_has_calls(expected)

    def test_handle_order_revert_1(self):
        """Test handle the orders when there are revert after timeout.
        Such as terminate connection to same alu right after
        a initialize connection is running
        """
        orderList = []
        orderList.append(self.generateAddOrder(alu=1))
        orderList.append(self.generateAddOrder(alu=1))
        orderList.append(self.generateAddOrder(alu=1))
        orderList.append(self.generateAddOrder(alu=2))
        orderList.append(self.generateRemoveOrder(alu=1))
        orderList.append(self.generateRemoveOrder(alu=1))
        orderList.append(self.generateRemoveOrder(alu=4))
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('storagegroup', '-addhlu', '-gname', 'fakehost',
                     '-hlus', '255', '-verboseStatus',
                     '-alus', '2', '-verboseStatus',
                     '-o'),
                    ('storagegroup', '-removehlu', '-gname', 'fakehost',
                     '-hlus', '4', '-verboseStatus',
                     '-o')]
        results = [self.testData.STORAGE_GROUP_HAS_MAP_3('fakehost'),
                   ("LUs Added: 2.", 0),
                   ("LUs Removed: 4.", 0)]

        fake_cli = self.driverSetup(commands, results)

        executor = self.driver.cli.get_attach_detach_batch_executor(
            self.testData.connector
        )

        executor(orderList)
        self.assertTrue(orderList[0]['status'] ==
                        emc_vnx_cli.AddHluStatus.ABANDON,
                        "Order should be in abandon status")
        self.assertTrue(orderList[1]['status'] ==
                        emc_vnx_cli.AddHluStatus.ABANDON,
                        "Order should be in abandon status")
        self.assertTrue(orderList[2]['status'] ==
                        emc_vnx_cli.AddHluStatus.ABANDON,
                        "Order should be in abandon status")
        self.assertTrue(orderList[3]['status'] == emc_vnx_cli.AddHluStatus.OK
                        and
                        orderList[3]['hlu'] == 255,
                        "add hlu in batch failed")
        self.assertTrue(orderList[4]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.HLU_NOT_IN_SG,
                        "remove hlu in batch failed")
        self.assertTrue(orderList[5]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.HLU_NOT_IN_SG,
                        "remove hlu in batch failed")
        self.assertTrue(orderList[6]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.OK,
                        "remove hlu in batch failed")
        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-addhlu', '-gname', 'fakehost',
                              '-hlus', '255', '-verboseStatus', '-alus',
                              '2', '-verboseStatus', '-o',
                              poll=False,
                              retry_disable=True),
                    mock.call('storagegroup', '-removehlu', '-gname',
                              'fakehost', '-hlus', '4', '-verboseStatus',
                              '-o',
                              poll=False,
                              retry_disable=True)]
        fake_cli.assert_has_calls(expected)

    def test_handle_order_revert_2(self):
        """Test handle the orders when there are revert after timeout.
        Such as terminate connection to same alu right after
        a initialize connection is running
        """
        orderList = []
        orderList.append(self.generateAddOrder(alu=1))
        orderList.append(self.generateRemoveOrder(alu=4))
        orderList.append(self.generateAddOrder(alu=1))
        orderList.append(self.generateAddOrder(alu=2))
        orderList.append(self.generateAddOrder(alu=5))
        orderList.append(self.generateAddOrder(alu=6))
        orderList.append(self.generateRemoveOrder(alu=1))
        orderList.append(self.generateAddOrder(alu=6))
        orderList.append(self.generateRemoveOrder(alu=1))
        orderList.append(self.generateRemoveOrder(alu=4))
        orderList.append(self.generateAddOrder(alu=5))
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('storagegroup', '-addhlu', '-gname', 'fakehost',
                     '-hlus', '2,1', '-verboseStatus',
                     '-alus', '2,5', '-verboseStatus',
                     '-o'),
                    ('storagegroup', '-removehlu', '-gname', 'fakehost',
                     '-hlus', '4', '-verboseStatus',
                     '-o')]
        results = [self.testData.STORAGE_GROUP_HAS_MAP_3('fakehost'),
                   ("LUs Added: 2,5.", 0),
                   ("LUs Removed: 4.", 0)]

        old_max_luns = self.configuration.max_luns_per_storage_group
        self.configuration.max_luns_per_storage_group = 4

        fake_cli = self.driverSetup(commands, results)

        executor = self.driver.cli.get_attach_detach_batch_executor(
            self.testData.connector
        )

        executor(orderList)
        self.assertTrue(orderList[0]['status'] ==
                        emc_vnx_cli.AddHluStatus.ABANDON,
                        "Order should be in abandon status")
        self.assertTrue(orderList[1]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.OK,
                        "remove hlu in batch failed")
        self.assertTrue(orderList[2]['status'] ==
                        emc_vnx_cli.AddHluStatus.ABANDON,
                        "Order should be in abandon status")
        self.assertTrue(orderList[3]['status'] == emc_vnx_cli.AddHluStatus.OK
                        and
                        orderList[3]['hlu'] == 2,
                        "add hlu in batch failed")
        self.assertTrue(orderList[4]['status'] == emc_vnx_cli.AddHluStatus.OK
                        and
                        orderList[4]['hlu'] == 1,
                        "add hlu in batch failed")
        self.assertTrue(orderList[5]['status'] ==
                        emc_vnx_cli.AddHluStatus.NO_HLU_LEFT,
                        "add hlu in batch failed")
        self.assertTrue(orderList[6]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.HLU_NOT_IN_SG,
                        "remove hlu in batch failed")
        self.assertTrue(orderList[7]['status'] ==
                        emc_vnx_cli.AddHluStatus.NO_HLU_LEFT,
                        "add hlu in batch failed")
        self.assertTrue(orderList[8]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.HLU_NOT_IN_SG,
                        "remove hlu in batch failed")
        self.assertTrue(orderList[9]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.OK,
                        "remove hlu in batch failed")
        self.assertTrue(orderList[10]['status'] == emc_vnx_cli.AddHluStatus.OK
                        and
                        orderList[10]['hlu'] == 1,
                        "add hlu in batch failed")
        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-addhlu', '-gname', 'fakehost',
                              '-hlus', '2,1', '-verboseStatus', '-alus',
                              '2,5', '-verboseStatus', '-o',
                              poll=False,
                              retry_disable=True),
                    mock.call('storagegroup', '-removehlu', '-gname',
                              'fakehost', '-hlus', '4', '-verboseStatus',
                              '-o',
                              poll=False,
                              retry_disable=True)]
        fake_cli.assert_has_calls(expected)
        self.configuration.max_luns_per_storage_group = old_max_luns

    def test_handle_duplicated_order(self):
        orderList = []
        orderList.append(self.generateAddOrder(alu=1))
        orderList.append(self.generateAddOrder(alu=1))
        orderList.append(self.generateRemoveOrder(alu=3))
        orderList.append(self.generateRemoveOrder(alu=3))
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('storagegroup', '-addhlu', '-gname', 'fakehost',
                     '-hlus', '255', '-verboseStatus',
                     '-alus', '1', '-verboseStatus',
                     '-o'),
                    ('storagegroup', '-removehlu', '-gname', 'fakehost',
                     '-hlus', '3', '-verboseStatus',
                     '-o')]
        results = [self.testData.STORAGE_GROUP_HAS_MAP_3('fakehost'),
                   ("LUs Added: 1.", 0),
                   ("LUs Removed: 3.", 0)]

        fake_cli = self.driverSetup(commands, results)

        executor = self.driver.cli.get_attach_detach_batch_executor(
            self.testData.connector
        )

        executor(orderList)
        self.assertTrue(orderList[0]['status'] == emc_vnx_cli.AddHluStatus.OK
                        and
                        orderList[0]['hlu'] == 255,
                        "add hlu in batch failed")
        self.assertTrue(orderList[1]['status'] == emc_vnx_cli.AddHluStatus.OK
                        and
                        orderList[1]['hlu'] == 255,
                        "add hlu in batch failed")
        self.assertTrue(orderList[2]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.OK,
                        "remove hlu in batch failed")
        self.assertTrue(orderList[3]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.OK,
                        "remove hlu in batch failed")
        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-addhlu', '-gname', 'fakehost',
                              '-hlus', '255', '-verboseStatus', '-alus',
                              '1', '-verboseStatus', '-o',
                              poll=False,
                              retry_disable=True),
                    mock.call('storagegroup', '-removehlu', '-gname',
                              'fakehost', '-hlus', '3', '-verboseStatus',
                              '-o',
                              poll=False,
                              retry_disable=True)]

        fake_cli.assert_has_calls(expected)

    def test_handle_order_retry(self):
        orderList = []
        orderList.append(self.generateAddOrder(alu=1))
        orderList.append(self.generateAddOrder(alu=2, tried=4))
        orderList.append(self.generateRemoveOrder(alu=3))
        orderList.append(self.generateRemoveOrder(alu=4, tried=1))
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('storagegroup', '-addhlu', '-gname', 'fakehost',
                     '-hlus', '255,254', '-verboseStatus',
                     '-alus', '1,2', '-verboseStatus',
                     '-o'),
                    ('storagegroup', '-removehlu', '-gname', 'fakehost',
                     '-hlus', '3,4', '-verboseStatus', '-o')]
        results = [self.testData.STORAGE_GROUP_HAS_MAP_3('fakehost'),
                   ("LUs Added: . LUs Not Added 1,2.", 0),
                   ("LUs Removed: . LUs Not Removed 3,4.", 0)]

        self.driverSetup(commands, results)

        executor = self.driver.cli.get_attach_detach_batch_executor(
            self.testData.connector
        )

        executor(orderList)

        self.assertTrue(orderList[0]['status'] ==
                        emc_vnx_cli.AddHluStatus.NEW and
                        orderList[0]['tried'] == 1,
                        "add hlu in batch failure is not detacted")
        self.assertTrue(orderList[1]['status'] ==
                        emc_vnx_cli.AddHluStatus.FAILURE,
                        "add hlu in batch failure is not detacted")
        self.assertTrue(orderList[2]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.NEW and
                        orderList[2]['tried'] == 1,
                        "remove hlu in batch failure is not detacted")
        self.assertTrue(orderList[3]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.FAILURE,
                        "remove hlu in batch failure is not detacted")

    def test_handle_order_no_hlu_left(self):
        orderList = []
        orderList.append(self.generateAddOrder(alu=1))
        orderList.append(self.generateAddOrder(alu=2))
        orderList.append(self.generateAddOrder(alu=3))
        orderList.append(self.generateAddOrder(alu=5))
        orderList.append(self.generateAddOrder(alu=5))
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('storagegroup', '-addhlu', '-gname', 'fakehost',
                     '-hlus', '2,1', '-verboseStatus',
                     '-alus', '1,2', '-verboseStatus',
                     '-o')]
        results = [self.testData.STORAGE_GROUP_HAS_MAP_3('fakehost'),
                   ("LUs Added: 1,2.", 0)]
        old_max_luns = self.configuration.max_luns_per_storage_group
        self.configuration.max_luns_per_storage_group = 4
        self.driverSetup(commands, results)

        executor = self.driver.cli.get_attach_detach_batch_executor(
            self.testData.connector
        )

        executor(orderList)

        self.assertTrue(orderList[0]['status'] ==
                        emc_vnx_cli.AddHluStatus.OK,
                        "add hlu in batch error")
        self.assertTrue(orderList[1]['status'] ==
                        emc_vnx_cli.AddHluStatus.OK,
                        "add hlu in batch error")
        self.assertTrue(orderList[2]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.OK,
                        "add hlu in batch error")
        self.assertTrue(orderList[3]['status'] ==
                        emc_vnx_cli.AddHluStatus.NO_HLU_LEFT,
                        "NO_HLU_LEFT is not detacted")
        self.assertTrue(orderList[4]['status'] ==
                        emc_vnx_cli.AddHluStatus.NO_HLU_LEFT,
                        "NO_HLU_LEFT is not detacted")
        self.configuration.max_luns_per_storage_group = old_max_luns

    def test_handle_order_not_in_SG(self):
        orderList = []
        orderList.append(self.generateRemoveOrder(alu=1))
        orderList.append(self.generateRemoveOrder(alu=2, tried=1))
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost')]
        results = [self.testData.STORAGE_GROUP_HAS_MAP_3('fakehost')]

        self.driverSetup(commands, results)

        executor = self.driver.cli.get_attach_detach_batch_executor(
            self.testData.connector
        )

        executor(orderList)

        self.assertTrue(orderList[0]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.HLU_NOT_IN_SG,
                        "add hlu in SG is not detacted")
        self.assertTrue(orderList[1]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.HLU_NOT_IN_SG,
                        "add hlu in SG is not detacted")

    def test_handle_order_in_batch_sg_preparation_failed(self):
        orderList = []
        orderList.append(self.generateAddOrder(alu=1))
        orderList.append(self.generateAddOrder(alu=2))
        orderList.append(self.generateRemoveOrder(alu=3))
        orderList.append(self.generateRemoveOrder(alu=4))
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('storagegroup', '-create', '-gname', 'fakehost'),
                    self.testData.set_path_cmd(
                        'fakehost', 'iqn.1993-08.org.debian:01:222',
                        'A', 4, 0, '10.0.0.2')]
        results = [("No Group", 83),
                   ("Failure", 23),
                   ("Failure", 23)]

        fake_cli = self.driverSetup(commands, results)

        executor = self.driver.cli.get_attach_detach_batch_executor(
            self.testData.connector
        )

        executor(orderList)
        self.assertTrue(orderList[0]['status'] ==
                        emc_vnx_cli.AddHluStatus.FAILURE,
                        "add hlu status is not changed")
        self.assertTrue(orderList[1]['status'] ==
                        emc_vnx_cli.AddHluStatus.FAILURE,
                        "add hlu status is not changed")
        self.assertTrue(orderList[2]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.FAILURE,
                        "remove hlu status is not changed")
        self.assertTrue(orderList[3]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.FAILURE,
                        "remove hlu status is not changed")

    def test_SG_auto_delete(self):
        orderList = []
        orderList.append(self.generateRemoveOrder(alu=1))
        orderList.append(self.generateRemoveOrder(alu=2, tried=1))
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('storagegroup', '-disconnecthost',
                     '-host', 'fakehost',
                     '-gname', 'fakehost', '-o'),
                    ('storagegroup', '-destroy', '-gname', 'fakehost', '-o')]
        results = [[self.testData.STORAGE_GROUP_HAS_MAP_3('fakehost'),
                    self.testData.STORAGE_GROUP_NO_MAP('fakehost')],
                   SUCCEED,
                   SUCCEED]
        oldValue = self.configuration.destroy_empty_storage_group
        self.configuration.destroy_empty_storage_group = True
        fake_cli = self.driverSetup(commands, results)

        executor = self.driver.cli.get_attach_detach_batch_executor(
            self.testData.connector
        )
        executor(orderList)

        self.assertTrue(orderList[0]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.HLU_NOT_IN_SG,
                        "add hlu in SG is not detacted")
        self.assertTrue(orderList[1]['status'] ==
                        emc_vnx_cli.RemoveHluStatus.HLU_NOT_IN_SG,
                        "add hlu in SG is not detacted")

        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-disconnecthost',
                              '-host', 'fakehost',
                              '-gname', 'fakehost', '-o'),
                    mock.call('storagegroup', '-destroy',
                              '-gname', 'fakehost', '-o')]

        fake_cli.assert_has_calls(expected)
        self.configuration.destroy_empty_storage_group = oldValue

    def test_initiator_auto_registration(self):
        orderList = []
        orderList.append(self.generateAddOrder(alu=1))
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('storagegroup', '-addhlu', '-gname', 'fakehost',
                     '-hlus', '255', '-verboseStatus',
                     '-alus', '1', '-verboseStatus',
                     '-o')]
        results = [[("No Group", 83),
                    self.testData.STORAGE_GROUP_HAS_MAP_3('fakehost')],
                   ("LUs Added: 1.", 0)]
        fake_cli = self.driverSetup(commands, results)
        executor = self.driver.cli.get_attach_detach_batch_executor(
            self.testData.connector
        )

        executor(orderList)

        self.assertTrue(orderList[0]['status'] ==
                        emc_vnx_cli.AddHluStatus.OK,
                        "add hlu in batch failed")

        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-create', '-gname', 'fakehost'),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              'iqn.1993-08.org.debian:01:222', 'A', 4, 0,
                              '10.0.0.2')),
                    mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-addhlu', '-gname', 'fakehost',
                              '-hlus', '255', '-verboseStatus', '-alus', '1',
                              '-verboseStatus', '-o',
                              poll=False,
                              retry_disable=True)]

        fake_cli.assert_has_calls(expected)


@mock.patch("random.shuffle",
            mock.Mock(return_value=0))
class BatchAttachDetachFCTestCase(BatchAttachDetachTestCaseBase):

    def generateDriver(self, conf):
        return EMCCLIFCDriver(configuration=conf)

    @mock.patch(
        "cinder.openstack.common.processutils.execute",
        mock.Mock(
            return_value=(
                "fakeportal iqn.1992-04.fake.com:fake.apm00123907237.a8", 0)))
    @mock.patch('random.randint',
                mock.Mock(return_value=0))
    def test_initialize_connection(self):
        # Test for auto registration
        test_volume = self.testData.test_volume.copy()
        test_volume['provider_location'] = 'system^fakesn|type^lun|id^1'
        td = self.testData

        class fake_batch_worker(object):
            def submit(self, order):
                order['status'] = emc_vnx_cli.AddHluStatus.OK
                order['hlu'] = 3
                order['payload'] = {
                    'raw_output': td.STORAGE_GROUP_HAS_MAP('fakehost')[0]
                }

        with mock.patch('cinder.volume.drivers.emc.emc_vnx_cli.'
                        'EMCVnxCliBase.get_batch_worker',
                        mock.Mock(return_value=fake_batch_worker())):
            commands = [('storagegroup', '-list', '-ganme', 'fakehost'),
                        self.testData.GETFCPORT_CMD()]
            results = [self.testData.STORAGE_GROUP_ISCSI_FC_HBA('fakehost'),
                       self.testData.FC_PORTS]

            fake_cli = self.driverSetup(commands, results)
            connection = self.driver.initialize_connection(
                test_volume,
                self.testData.connector)
            self.assertEqual(3, connection['data']['target_lun'])

    def test_initiator_auto_registration(self):
        orderList = []
        orderList.append(self.generateAddOrder(alu=1))
        old_auto_registration = self.configuration.initiator_auto_registration
        self.configuration.initiator_auto_registration = True
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('port', '-list', '-sp'),
                    ('storagegroup', '-addhlu', '-gname', 'fakehost',
                     '-hlus', '255', '-verboseStatus',
                     '-alus', '1', '-verboseStatus',
                     '-o')]
        results = [[("No Group", 83),
                    self.testData.STORAGE_GROUP_HAS_MAP_3('fakehost')],
                   self.testData.FC_PORTS,
                   ("LUs Added: 1.", 0)]

        fake_cli = self.driverSetup(commands, results)
        executor = self.driver.cli.get_attach_detach_batch_executor(
            self.testData.connector
        )
        executor(orderList)

        self.assertTrue(
            orderList[0]['status'] == emc_vnx_cli.AddHluStatus.OK,
            "add hlu in batch failed")

        expected = [
            mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                      poll=True),
            mock.call('storagegroup', '-create', '-gname', 'fakehost'),
            mock.call('port', '-list', '-sp'),
            mock.call(*self.testData.set_path_cmd('fakehost',
                      '22:34:56:78:90:12:34:51:23:45:67:89:01:23:45',
                      'A', '0', None, '10.0.0.2')),
            mock.call(*self.testData.set_path_cmd('fakehost',
                      '22:34:56:78:90:12:34:51:23:45:67:89:01:23:45',
                      'B', '2', None, '10.0.0.2')),
            mock.call(*self.testData.set_path_cmd('fakehost',
                      '22:34:56:78:90:54:32:11:23:45:67:89:05:43:21',
                      'A', '0', None, '10.0.0.2')),
            mock.call(*self.testData.set_path_cmd('fakehost',
                      '22:34:56:78:90:54:32:11:23:45:67:89:05:43:21',
                      'B', '2', None, '10.0.0.2')),
            mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                      poll=True),
            mock.call('storagegroup', '-addhlu', '-gname', 'fakehost',
                      '-hlus', '255', '-verboseStatus', '-alus', '1',
                      '-verboseStatus', '-o',
                      poll=False,
                      retry_disable=True)
        ]

        fake_cli.assert_has_calls(expected)
        self.configuration.initiator_auto_registration = old_auto_registration


class SingleAttachDetachISCSITestCase(DriverTestCaseBase):
    def setUp(self):
        super(SingleAttachDetachISCSITestCase, self).setUp()
        self.configuration.attach_detach_batch_interval = -1

    def generateDriver(self, conf):
        return EMCCLIISCSIDriver(configuration=conf)

    @mock.patch(
        "cinder.openstack.common.processutils.execute",
        mock.Mock(
            return_value=(
                "fakeportal iqn.1992-04.fake.com:fake.apm00123907237.a8", 0)))
    @mock.patch('random.randint',
                mock.Mock(return_value=0))
    def test_initialize_connection(self):
        test_volume = self.testData.test_volume.copy()
        test_volume['provider_location'] = 'system^fakesn|type^lun|id^1'

        # Test for auto registration
        self.configuration.initiator_auto_registration = True
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    self.testData.PINGNODE_CMD('A', 4, 0, '10.0.0.2')]
        results = [[("No group", 83),
                    self.testData.STORAGE_GROUP_HAS_MAP('fakehost')],
                   self.testData.PING_OK]

        fake_cli = self.driverSetup(commands, results)

        self.driver.initialize_connection(
            test_volume,
            self.testData.connector)

        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=False),
                    mock.call('storagegroup', '-create', '-gname', 'fakehost'),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              'iqn.1993-08.org.debian:01:222',
                              'A', 4, 0, '10.0.0.2')),
                    mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-addhlu', '-hlu', 2, '-alu', 1,
                              '-gname', 'fakehost',
                              poll=False),
                    mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1'),
                              poll=False),
                    mock.call(*self.testData.PINGNODE_CMD('A', 4, 0,
                                                          '10.0.0.2'))]
        fake_cli.assert_has_calls(expected)

        # Test for manaul registration
        self.configuration.initiator_auto_registration = False

        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    self.testData.CONNECTHOST_CMD('fakehost', 'fakehost')]
        results = [
            [("No group", 83),
             self.testData.STORAGE_GROUP_HAS_MAP('fakehost')],
            ('', 0),
        ]
        fake_cli = self.driverSetup(commands, results)
        self.driver.initialize_connection(
            test_volume,
            self.testData.connector)

        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=False),
                    mock.call('storagegroup', '-create', '-gname', 'fakehost'),
                    mock.call('storagegroup', '-connecthost',
                              '-host', 'fakehost', '-gname', 'fakehost', '-o'),
                    mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-addhlu', '-hlu', 2, '-alu', 1,
                              '-gname', 'fakehost', poll=False),
                    mock.call('lun', '-list', '-name', 'vol1', '-state',
                              '-opDetails', '-userCap', '-owner',
                              '-attachedSnapshot', poll=False),
                    mock.call(*self.testData.PINGNODE_CMD('A', 4, 0,
                                                          '10.0.0.2'))]
        fake_cli.assert_has_calls(expected)

    @mock.patch(
        "cinder.openstack.common.processutils.execute",
        mock.Mock(
            return_value=(
                "fakeportal iqn.1992-04.fake.com:fake.apm00123907237.a8", 0)))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.EMCVnxCliBase.get_lun_id",
        mock.Mock(
            return_value=3))
    @mock.patch('random.randint',
                mock.Mock(return_value=0))
    def test_initialize_connection_exist(self):
        """A LUN is added to the SG right before the attach,
        it may not exists in the first SG query
        """
        # Test for auto registration
        self.configuration.initiator_auto_registration = True
        self.configuration.max_luns_per_storage_group = 2
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('storagegroup', '-addhlu', '-hlu', 2, '-alu', 3,
                     '-gname', 'fakehost'),
                    self.testData.PINGNODE_CMD('A', 4, 0, '10.0.0.2')]
        results = [[self.testData.STORAGE_GROUP_HAS_MAP('fakehost'),
                    self.testData.STORAGE_GROUP_HAS_MAP_2('fakehost')],
                   ("fakeerror", 23),
                   self.testData.PING_OK]

        fake_cli = self.driverSetup(commands, results)

        iscsi_data = self.driver.initialize_connection(
            self.testData.test_volume,
            self.testData.connector
        )
        self.assertTrue(iscsi_data['data']['target_lun'] == 2,
                        "iSCSI initialize connection returned wrong HLU")
        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=False),
                    mock.call('storagegroup', '-addhlu', '-hlu', 2, '-alu', 3,
                              '-gname', 'fakehost',
                              poll=False),
                    mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1'),
                              poll=False),
                    mock.call(*self.testData.PINGNODE_CMD('A', 4, 0,
                                                          '10.0.0.2'))]
        fake_cli.assert_has_calls(expected)

    @mock.patch(
        "cinder.openstack.common.processutils.execute",
        mock.Mock(
            return_value=(
                "fakeportal iqn.1992-04.fake.com:fake.apm00123907237.a8", 0)))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.EMCVnxCliBase.get_lun_id",
        mock.Mock(
            return_value=4))
    @mock.patch('random.randint',
                mock.Mock(return_value=0))
    def test_initialize_connection_no_hlu_left_1(self):
        """There is no hlu per the first SG query
        But there are hlu left after the full poll
        """
        # Test for auto registration
        self.configuration.initiator_auto_registration = True
        self.configuration.max_luns_per_storage_group = 2
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('storagegroup', '-addhlu', '-hlu', 2, '-alu', 4,
                     '-gname', 'fakehost'),
                    self.testData.PINGNODE_CMD('A', 4, 0, '10.0.0.2')]
        results = [[self.testData.STORAGE_GROUP_HAS_MAP_2('fakehost'),
                    self.testData.STORAGE_GROUP_HAS_MAP('fakehost')],
                   ("", 0),
                   self.testData.PING_OK]

        fake_cli = self.driverSetup(commands, results)

        iscsi_data = self.driver.initialize_connection(
            self.testData.test_volume,
            self.testData.connector)
        self.assertTrue(iscsi_data['data']['target_lun'] == 2,
                        "iSCSI initialize connection returned wrong HLU")
        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=False),
                    mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-addhlu', '-hlu', 2, '-alu', 4,
                              '-gname', 'fakehost',
                              poll=False),
                    mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1'),
                              poll=False),
                    mock.call(*self.testData.PINGNODE_CMD('A', 4, 0,
                                                          u'10.0.0.2'))]
        fake_cli.assert_has_calls(expected)

    @mock.patch(
        "cinder.openstack.common.processutils.execute",
        mock.Mock(
            return_value=(
                "fakeportal iqn.1992-04.fake.com:fake.apm00123907237.a8", 0)))
    @mock.patch(
        "cinder.volume.drivers.emc.emc_vnx_cli.EMCVnxCliBase.get_lun_id",
        mock.Mock(
            return_value=4))
    @mock.patch('random.randint',
                mock.Mock(return_value=0))
    def test_initialize_connection_no_hlu_left_2(self):
        """There is no usable hlu for the SG
        """
        # Test for auto registration
        self.configuration.initiator_auto_registration = True
        old_max_luns = self.configuration.max_luns_per_storage_group
        self.configuration.max_luns_per_storage_group = 2
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost')]
        results = [
            [self.testData.STORAGE_GROUP_HAS_MAP_2('fakehost'),
             self.testData.STORAGE_GROUP_HAS_MAP_2('fakehost')]
        ]

        fake_cli = self.driverSetup(commands, results)

        self.assertRaises(exception.VolumeBackendAPIException,
                          self.driver.initialize_connection,
                          self.testData.test_volume,
                          self.testData.connector)
        expected = [
            mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                      poll=False),
            mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                      poll=True),
        ]
        fake_cli.assert_has_calls(expected)
        self.configuration.max_luns_per_storage_group = old_max_luns

    def test_terminate_connection(self):

        os.path.exists = mock.Mock(return_value=1)
        self.driver = EMCCLIISCSIDriver(configuration=self.configuration)
        cli_helper = self.driver.cli._client
        data = {'storage_group_name': "fakehost",
                'storage_group_uid': "2F:D4:00:00:00:00:00:"
                "00:00:00:FF:E5:3A:03:FD:6D",
                'lunmap': {1: 16, 2: 88, 3: 47}}
        cli_helper.get_storage_group = mock.Mock(
            return_value=data)
        lun_info = {'lun_name': "unit_test_lun",
                    'lun_id': 1,
                    'pool': "unit_test_pool",
                    'attached_snapshot': "N/A",
                    'owner': "A",
                    'total_capacity_gb': 1.0,
                    'state': "Ready"}
        cli_helper.get_lun_by_name = mock.Mock(return_value=lun_info)
        cli_helper.remove_hlu_from_storagegroup = mock.Mock()
        self.driver.terminate_connection(self.testData.test_volume,
                                         self.testData.connector)
        cli_helper.remove_hlu_from_storagegroup.assert_called_once_with(
            16, self.testData.connector["host"])

    def test_get_registered_spport_set(self):
        self.driverSetup()
        spport_set = self.driver.cli._client.get_registered_spport_set(
            'iqn.1993-08.org.debian:01:222', 'fakehost',
            self.testData.STORAGE_GROUP_HAS_MAP_ISCSI('fakehost')[0])
        self.assertTrue({('A', 2, 0), ('A', 0, 0), ('B', 2, 0)} == spport_set)


class SingleAttachDetachFCTestCase(DriverTestCaseBase):
    def setUp(self):
        super(SingleAttachDetachFCTestCase, self).setUp()
        self.configuration.attach_detach_batch_interval = -1

    def generateDriver(self, conf):
        return EMCCLIFCDriver(configuration=conf)

    @mock.patch(
        "cinder.openstack.common.processutils.execute",
        mock.Mock(
            return_value=(
                "fakeportal iqn.1992-04.fake.com:fake.apm00123907237.a8", 0)))
    @mock.patch('random.randint',
                mock.Mock(return_value=0))
    def test_initialize_connection_fc_auto_reg(self):
        # Test for auto registration
        test_volume = self.testData.test_volume.copy()
        test_volume['provider_location'] = 'system^fakesn|type^lun|id^1'
        self.configuration.initiator_auto_registration = True
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('storagegroup', '-list', '-gname', 'fakehost'),
                    self.testData.GETFCPORT_CMD()]
        results = [[("No group", 83),
                    self.testData.STORAGE_GROUP_HAS_MAP('fakehost')],
                   self.testData.STORAGE_GROUP_ISCSI_FC_HBA('fakehost'),
                   self.testData.FC_PORTS]

        fake_cli = self.driverSetup(commands, results)
        self.driver.initialize_connection(
            test_volume,
            self.testData.connector)

        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=False),
                    mock.call('storagegroup', '-create', '-gname', 'fakehost'),
                    mock.call('port', '-list', '-sp'),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              '22:34:56:78:90:12:34:51:23:45:67:89:01:23:45',
                              'A', '0', None, '10.0.0.2')),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              '22:34:56:78:90:12:34:51:23:45:67:89:01:23:45',
                              'B', '2', None, '10.0.0.2')),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              '22:34:56:78:90:54:32:11:23:45:67:89:05:43:21',
                              'A', '0', None, '10.0.0.2')),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              '22:34:56:78:90:54:32:11:23:45:67:89:05:43:21',
                              'B', '2', None, '10.0.0.2')),
                    mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-addhlu', '-hlu', 2, '-alu', 1,
                              '-gname', 'fakehost',
                              poll=False),
                    mock.call('storagegroup', '-list', '-gname', 'fakehost',
                              poll=True),
                    mock.call('port', '-list', '-sp')]
        fake_cli.assert_has_calls(expected)

        # Test for manaul registration
        self.configuration.initiator_auto_registration = False

        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    ('storagegroup', '-list', '-gname', 'fakehost'),
                    self.testData.CONNECTHOST_CMD('fakehost', 'fakehost'),
                    self.testData.GETFCPORT_CMD()]
        results = [[("No group", 83),
                    self.testData.STORAGE_GROUP_NO_MAP('fakehost')],
                   self.testData.STORAGE_GROUP_ISCSI_FC_HBA('fakehost'),
                   ('', 0),
                   self.testData.FC_PORTS]
        fake_cli = self.driverSetup(commands, results)
        self.driver.initialize_connection(
            test_volume,
            self.testData.connector)

        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=False),
                    mock.call('storagegroup', '-create', '-gname', 'fakehost'),
                    mock.call('storagegroup', '-connecthost',
                              '-host', 'fakehost', '-gname', 'fakehost', '-o'),
                    mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-addhlu', '-hlu', 1, '-alu', 1,
                              '-gname', 'fakehost', poll=False),
                    mock.call('storagegroup', '-list', '-gname', 'fakehost',
                              poll=True),
                    mock.call('port', '-list', '-sp')]
        fake_cli.assert_has_calls(expected)


class WhileListFCTestCase(DriverTestCaseBase):
    def generateDriver(self, conf):
        return EMCCLIFCDriver(configuration=conf)

    @mock.patch('random.randint',
                mock.Mock(return_value=0))
    def test_initialize_connection_fc_white_list(self):
        self.configuration.attach_detach_batch_interval = -1
        self.configuration.io_port_list = 'a-0,B-2'
        test_volume = self.testData.test_volume.copy()
        test_volume['provider_location'] = 'system^fakesn|type^lun|id^1'
        self.configuration.initiator_auto_registration = True
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    self.testData.GETFCPORT_CMD()]
        results = [[("No group", 83),
                    self.testData.STORAGE_GROUP_HAS_MAP_ISCSI('fakehost')],
                   self.testData.FC_PORTS]

        fake_cli = self.driverSetup(commands, results)
        data = self.driver.initialize_connection(
            test_volume,
            self.testData.connector)

        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=False),
                    mock.call('storagegroup', '-create', '-gname', 'fakehost'),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              '22:34:56:78:90:12:34:51:23:45:67:89:01:23:45',
                              'A', 0, None, '10.0.0.2')),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              '22:34:56:78:90:12:34:51:23:45:67:89:01:23:45',
                              'B', 2, None, '10.0.0.2')),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              '22:34:56:78:90:54:32:11:23:45:67:89:05:43:21',
                              'A', 0, None, '10.0.0.2')),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              '22:34:56:78:90:54:32:11:23:45:67:89:05:43:21',
                              'B', 2, None, '10.0.0.2')),
                    mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-addhlu', '-hlu', 2, '-alu', 1,
                              '-gname', 'fakehost',
                              poll=False),
                    mock.call('port', '-list', '-sp')]
        fake_cli.assert_has_calls(expected)
        self.assertEqual(data['data']['target_wwn'],
                         ['5006016008600195', '5006016A0860080F'])

    @mock.patch('random.randint',
                mock.Mock(return_value=0))
    def test_initialize_connection_fc_port_registered(self):
        self.configuration.attach_detach_batch_interval = -1
        self.configuration.io_port_list = 'a-0,B-2'
        test_volume = self.testData.test_volume.copy()
        test_volume['provider_location'] = 'system^fakesn|type^lun|id^1'
        self.configuration.initiator_auto_registration = True
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                    self.testData.GETFCPORT_CMD()]
        results = [self.testData.STORAGE_GROUP_ISCSI_FC_HBA('fakehost'),
                   self.testData.FC_PORTS]

        fake_cli = self.driverSetup(commands, results)
        data = self.driver.initialize_connection(
            test_volume,
            self.testData.connector)

        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=False),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              '22:34:56:78:90:12:34:51:23:45:67:89:01:23:45',
                              'A', 0, None, '10.0.0.2')),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              '22:34:56:78:90:54:32:11:23:45:67:89:05:43:21',
                              'A', 0, None, '10.0.0.2')),
                    mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-addhlu', '-hlu', 2, '-alu', 1,
                              '-gname', 'fakehost',
                              poll=False),
                    mock.call('port', '-list', '-sp')]
        fake_cli.assert_has_calls(expected)
        self.assertEqual(data['data']['target_wwn'],
                         ['5006016008600195', '5006016A0860080F'])


class WhileListISCSITestCase(DriverTestCaseBase):

    def generateDriver(self, conf):
        return EMCCLIISCSIDriver(configuration=conf)

    @mock.patch('random.randint',
                mock.Mock(return_value=0))
    def test_initialize_connection_iscsi_white_list(self):
        self.configuration.attach_detach_batch_interval = -1
        self.configuration.io_port_list = 'a-0-0,B-2-0'
        test_volume = self.testData.test_volume.copy()
        test_volume['provider_location'] = 'system^fakesn|type^lun|id^1'
        # Test for auto registration
        self.configuration.initiator_auto_registration = True
        commands = [self.testData.STORAGEGROUP_LIST_CMD('fakehost')]
        results = [[("No group", 83),
                    self.testData.STORAGE_GROUP_HAS_MAP_ISCSI('fakehost')]]
        fake_cli = self.driverSetup(commands, results)
        self.driver.cli.iscsi_targets = {'A': [{'SP': 'A', 'Port ID': 0,
                                               'Virtual Port ID': 0,
                                               'Port WWN': 'fake_iqn',
                                               'IP Address': '192.168.1.1'},
                                               {'SP': 'A', 'Port ID': 0,
                                               'Virtual Port ID': 1,
                                               'Port WWN': 'fake_iqn',
                                               'IP Address': '192.168.2.1'}],
                                         'B': [{'SP': 'B', 'Port ID': 2,
                                                'Virtual Port ID': 0,
                                                'Port WWN': 'fake_iqn1',
                                                'IP Address': '192.168.1.2'}]}
        self.driver.initialize_connection(
            test_volume,
            self.testData.connector)
        expected = [mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=False),
                    mock.call('storagegroup', '-create', '-gname', 'fakehost'),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              'iqn.1993-08.org.debian:01:222',
                              'A', 0, 0, '10.0.0.2')),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              'iqn.1993-08.org.debian:01:222',
                              'A', 0, 1, '10.0.0.2')),
                    mock.call(*self.testData.set_path_cmd('fakehost',
                              'iqn.1993-08.org.debian:01:222',
                              'B', 2, 0, '10.0.0.2')),
                    mock.call(*self.testData.STORAGEGROUP_LIST_CMD('fakehost'),
                              poll=True),
                    mock.call('storagegroup', '-addhlu', '-hlu', 2, '-alu', 1,
                              '-gname', 'fakehost',
                              poll=False),
                    mock.call(*self.testData.LUN_PROPERTY_ALL_CMD('vol1'),
                              poll=False)]
        fake_cli.assert_has_calls(expected)

    @mock.patch('cinder.volume.drivers.emc.emc_vnx_cli.'
                'CommandLineHelper.get_array_serial',
                mock.Mock(return_value={'array_serial': 'fake_serial'}))
    @mock.patch('cinder.volume.drivers.emc.emc_vnx_cli.'
                'CommandLineHelper.get_pool',
                mock.Mock(return_value={'total_capacity_gb': 0.0,
                                        'free_capacity_gb': 0.0}))
    def test_update_io_ports(self):
        self.configuration.attach_detach_batch_interval = -1
        self.configuration.io_port_list = 'a-0-0,a-0-1,B-2-0'
        # Test for auto registration
        self.configuration.initiator_auto_registration = True
        commands = [self.testData.GETPORT_CMD()]
        results = [self.testData.WHITE_LIST_PORTS]
        fake_cli = self.driverSetup(commands, results)
        self.driver.update_volume_status()
        expected = [mock.call(*self.testData.GETPORT_CMD(), poll=False)]
        fake_cli.assert_has_calls(expected)
        io_ports = self.driver.cli.iscsi_targets
        self.assertEqual((io_ports['A'][0]['Port ID'],
                          io_ports['A'][0]['Port WWN'],
                          io_ports['A'][0]['Virtual Port ID']),
                         (0, 'iqn.1992-04.com.emc:cx.fnmxxx.a0', 0))
        self.assertEqual((io_ports['A'][1]['Port ID'],
                          io_ports['A'][1]['Port WWN'],
                          io_ports['A'][1]['Virtual Port ID']),
                         (0, 'iqn.1992-04.com.emc:cx.fnmxxx.a0', 1))
        self.assertEqual((io_ports['B'][0]['Port ID'],
                          io_ports['B'][0]['Port WWN'],
                          io_ports['A'][0]['Virtual Port ID']),
                         (2, 'iqn.1992-04.com.emc:cx.fnmxxx.b2', 0))
