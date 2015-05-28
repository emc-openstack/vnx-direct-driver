# VNX FC Direct Driver

Copyright (c) 2012 - 2015 EMC Corporation
All Rights Reserved

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the License. You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
License for the specific language governing permissions and limitations
under the License.

## Overview

EMCCLIFCDriver (a.k.a. VNX FC Direct Driver) is based on the FibreChannelDriver defined in Cinder, with the ability to create/delete, attach/detach volumes, create/delete snapshots, etc. 

EMCCLIFCDriver performs the volume operations by executing Navisphere CLI. 

The Navisphere CLI (a.k.a. NaviSecCLI) is a Command Line Interface (CLI) used for management, diagnostics and reporting functions for VNX.

## Supported OpenStack Release

This driver supports Havana and Icehouse releases.

## Requirements

* VNX OE for Block version 5.32 or higher.
* VNX Snapshot and Thin Provisioning license should be activated for VNX.
* Navisphere CLI v7.32 or higher is installed along with the driver

## Supported Operations

The following operations will be supported on VNX array:

* Create volume
* Delete volume
* Extend volume
* Attach volume
* Detach volume
* Migrate volume
* Create snapshot
* Delete snapshot
* Create volume from snapshot
* Create cloned volume
* Copy Image to Volume
* Copy Volume to Image

## Preparation

### Install Navisphere CLI 

Navisphere CLI needs to be installed in all the Cinder nodes in an OpenStack deployment.

* For Ubuntu x64, DEB is available in [EMC OpenStack Github](https://github.com/emc-openstack/naviseccli).
* For all other variants of Linux, Navisphere CLI is available at [Downloads for VNX2 Series](https://support.emc.com/downloads/36656_VNX2-Series) or [Downloads for VNX1 Series](https://support.emc.com/downloads/12781_VNX1-Series).


### Install Cinder driver

EMC VNX FC Direct Driver provided in the installer package consists of three python files:

        emc_vnx_cli.py
        emc_cli_fc.py
        queueworker.py

Copy the above files to the `cinder/volume/drivers/emc/` directory of your OpenStack node(s) where cinder-volume is running.

### FC Zoning with VNX
FC Zoning should be done between OpenStack node(s) and VNX.

### Register with VNX

To access the storage in VNX, Compute nodes need to be registered with VNX first.

To perform "Copy Image to Volume" and "Copy Volume to Image" operations, nodes running the cinder-volume service(Cinder nodes) must be registered with the VNX as well.

Below mentioned steps are for a Compute node.Please follow the same steps for Cinder nodes also. (The steps can be skipped if Initiator Auto Registration is to be enabled)

* Assume `20:00:00:24:FF:48:BA:C2:21:00:00:24:FF:48:BA:C2` is WWN of one FC initiator port name of the Compute node. Register `20:00:00:24:FF:48:BA:C2:21:00:00:24:FF:48:BA:C2` in Unisphere
    * Login to Unisphere, go to `FNM0000000000->Hosts->Initiators`,
    * Refresh and wait until initiator `20:00:00:24:FF:48:BA:C2:21:00:00:24:FF:48:BA:C2` with SP Port `A-1` appears.
    * Click the `Register` button, select `CLARiiON/VNX` and enter the host name and IP address:
        * Hostname : myhost1
        * IP : 10.10.61.1
        * Click Register. 
    * Now host 10.10.61.1 will appear under Hosts->Host List as well.
* Register the WWN or more WWNs to more ports if needed.

## Backend Configuration

Make the following changes in `/etc/cinder/cinder.conf`:

Following are the elements specific to VNX CLI driver to be configured

        storage_vnx_pool_name = Pool_01_SAS
        san_ip = 10.10.72.41
        san_secondary_ip = 10.10.72.42
        #VNX user name
        #san_login = username
        #VNX user password
        #san_password = password
        #VNX user type. Valid values are: global, local and ldap. global is the default value
        #storage_vnx_authentication_type = ldap
        #Directory path that contains the VNX security file. Make sure the security file is generated first
        #VNX credentials are not needed to configured with using security file
        storage_vnx_security_file_dir = /etc/secfile/array1
        naviseccli_path = /opt/Navisphere/bin/naviseccli
        # timeout in minutes
        default_timeout = 10
        volume_driver=cinder.volume.drivers.emc.emc_cli_fc.EMCCLIFCDriver
        destroy_empty_storage_group = False
        [database]
        max_pool_size=20
        max_overflow=30



* where `san_ip` is one of the SP IP address of the VNX array. `san_secondary_ip` is the other SP IP address of VNX array. `san_secondary_ip` is an optional field, it serve the purpose of providing a high availability(HA) design. Based on that if one of the SP is down, the other SP can be connected automatically. `san_ip` is a mandatory field, which provide the main connection.
* where `Pool_01_SAS` is the pool user wants to create volume from. The pools can be created using Unisphere for VNX. Refer to the following "Multiple Pools and Thick/Thin Provisioning" section on how to support thick/thin provisioning
* where `storage_vnx_security_file_dir` is directory path that contains the VNX security file. Make sure the security file is generated according to the steps in Authentication section if use it
* Restart of cinder-volume service is needed to make the configuration change take effect.

## Authentication

VNX credentials are needed so that the driver could talk with the VNX system. Credentials in Global, Local and LDAP scopes are supported. There are 2 approaches to provide the credentials:

The recommended one is using Navisphere CLI security file to provide credentials which can get rid of providing plain text credentials in configuration file. Below is instruction on how to do this.

1. Find out the Linux User ID of the `/usr/bin/cinder-volume` processes. Assuming the service /usr/bin/cinder-volume is running with account `cinder`
2. Switch to root account

       $ sudo su
3. Change `cinder:x:113:120::/var/lib/cinder:/bin/false` to `cinder:x:113:120::/var/lib/cinder:/bin/bash` in `/etc/passwd` 
    (This temporary change is to make step 4 work.)
4. Save the credentials on behave of `cinder` user to security file(assuming the array credentials are `admin/admin` in `global` scope). In below command, swith -secfilepath is used to specify where to save security file (assume saving to directory /etc/secfile/array1).

        $ su -l cinder -c '/opt/Navisphere/bin/naviseccli -AddUserSecurity -user admin -password admin -scope 0 -secfilepath /etc/secfile/array1'
   Save security file to different locations for different arrays except same credentials are shared between all arrays managed by the host. Otherwise, the credentials in security file will be overwritten. If `-secfilepath` is not specified in above command, the security file will be saved to default location which is home directory of executor. 
5. Change `cinder:x:113:120::/var/lib/cinder:/bin/bash` back to `cinder:x:113:120::/var/lib/cinder:/bin/false` in /etc/passwd
6. Remove the credentials options `san_login`, `san_password` and `storage_vnx_authentication_type` from cinder.conf (normally it is `/etc/cinder/cinder.conf`). 
   Add option `storage_vnx_security_file_dir` and set its value to the directory path supplied with switch '-secfilepath' in step 4. Omit this option if '-secfilepath' is not used in step 4.

        #Directory path that contains the VNX security file. Generate the security file first
        storage_vnx_security_file_dir = /etc/secfile/array1
7. Restart cinder-volume service to make the change take effect

Alternatively, the credentials can be specified in /etc/cinder/cinder.conf by below 3 options with plain text:

        #VNX user name
        san_login = username
        #VNX user password
        san_password = password
        #VNX user type. Valid values are: global, local and ldap. global is the default value
        storage_vnx_authentication_type = ldap

## Restrictions

* It is not suggest to deploy the driver on Nova Compute Node if "cinder upload-to-image --force True" is to be used against an in-use volume. Otherwise, "cinder upload-to-image --force True" will terminate the VM instance's data access to the volume.
* VNX does not support to extend the thick volume which has snapshot. If user tries to extend a volume which has snapshot, status of the volume would change to "error_extending".

## Thick/Thin Provisioning

Use Cinder Volume Type to define a provisioning type and the provisioning type could be either thin or thick.

Here is an example of how to create thick/thin volume. First create volume types. Then define extra specs for each volume type.

        cinder --os-username admin --os-tenant-name admin type-create "ThickVolume"
        cinder --os-username admin --os-tenant-name admin type-create "ThinVolume"
        cinder --os-username admin --os-tenant-name admin type-key "ThickVolume" set storagetype:provisioning=thick
        cinder --os-username admin --os-tenant-name admin type-key "ThinVolume" set storagetype:provisioning=thin

In the example above, two volume types are created: `ThickVolume` and `ThinVolume`. For `ThickVolume`, `storagetype:provisioning` is set to `thick`. Similarly for `ThinVolume`. If `storagetype:provisioning` is not specified or an invalid value, default value `thick` is adopted.

Volume Type names `ThickVolume` and `ThinVolume` are user-defined and can be any names. Extra spec key `storagetype:provisioning` has to be the exact name listed here. Extra spec value for `storagetype:provisioning` has to be either `thick` or `thin`.
During volume creation, if the driver find `storagetype:provisioning` in the extra spec of the Volume Type, it will create the volume of the provisioning type accordingly. Otherwise, the volume will be default to thick.

## Multiple Pools Support

Normally a storage pool is configured for a cinder backend (named as pool-based backend), so that only that storage pool will be used by that cinder backend. 

When `storage_vnx_pool_name` is not given in the configuration file, driver will allow user to use extra spec key `storagetype:pool` in Volume Type to specify the storage pool for volume creation. When `storagetype:pool` is not specified in Volume Type and `storage_vnx_pool_name` is not found in configuration file, the driver will randomly choose a pool to create the volume. This kind of cinder backend is named as array-based backend.

Here is an example of configuration of array-based backend.

        san_ip = 10.10.72.41
        #Directory path that contains the VNX security file. Make sure the security file is generated first
        storage_vnx_security_file_dir = /etc/secfile/array1
        naviseccli_path = /opt/Navisphere/bin/naviseccli
        default_timeout = 10
        volume_driver=cinder.volume.drivers.emc.emc_cli_fc.EMCCLIFCDriver
        destroy_empty_storage_group = False
        volume_backend_name = vnx_41

In this configuration, if want to create the volume on a certain storage pool, first create a volume type and specify storage pool in extra spec, then create a volume with this volume type. 

Here is an example to create the volume type:

        cinder --os-username admin --os-tenant-name admin type-create "HighPerf"
        cinder --os-username admin --os-tenant-name admin type-key "HighPerf" set storagetype:pool=Pool_02_SASFLASH volume_backend_name=vnx_41

## Storage Group Automatic Deletion

For volume attaching, the driver has a Storage Group on VNX for each Compute Node hosting the VM instances that are to consume VNX Block Storage(Using Compute Node's hostname as Storage Group's name). All the volumes used by the VM instances in a Computer Node will be put into the corresponding Storage Group. If destroy_empty_storage_group=True, the driver will remove the empty Storage Group when its last volume is detached.
For data safety, it is NOT suggest to set destroy_empty_storage_group=True unless the VNX is exclusively managed by one Cinder Node because consistent lock_path is required for operation synchronization for this behavior.


## EMC storage-assisted volume migration

EMC Direct Driver support storage-assisted volume migration, when you start migrate with "cinder migrate --force-host-copy False <volume_id> <host>" or 
"cinder migrate <volume_id> <host>" then Cinder will try to leverage the VNX's native LUN Migration functionality.

In following scenarios, VNX native LUN migration will not be triggered:

1. Volume Migration between backends with different storage protocol, ex, FC and iSCSI
2. Volume Migration from pool-based backend to array-based backend
3. Volume is to be migrated across arrays

## Multi-backend configuration

        [DEFAULT]

        enabled_backends=backendA, backendB

        [backendA]

        storage_vnx_pool_name = Pool_01_SAS
        san_ip = 10.10.72.41
        #Directory path that contains the VNX security file. Make sure the security file is generated first
        storage_vnx_security_file_dir = /etc/secfile/array1
        naviseccli_path = /opt/Navisphere/bin/naviseccli
        # Timeout in Minutes
        default_timeout = 10
        volume_driver=cinder.volume.drivers.emc.emc_cli_fc.EMCCLIFCDriver
        destroy_empty_storage_group = False
        initiator_auto_registration=True
        io_port_list=a-1,B-3
        [backendB]
        storage_vnx_pool_name = Pool_02_SAS
        san_ip = 10.10.26.101
        san_login = username
        san_password = password
        naviseccli_path = /opt/Navisphere/bin/naviseccli
        # Timeout in Minutes
        default_timeout = 10
        volume_driver=cinder.volume.drivers.emc.emc_cli_fc.EMCCLIFCDriver
        destroy_empty_storage_group = False
        initiator_auto_registration=True
        io_port_list=a-1,B-3
        [database]

        max_pool_size=20
        max_overflow=30

For more details on multi-backend, see [OpenStack Administration Guide](http://docs.openstack.org/admin-guide-cloud/content/multi_backend.html)

## Initiator Auto Registration

When `initiator_auto_registration=True`, the driver will automatically register FC initiators to all working FC target ports of the VNX array during volume attaching (The driver will skip those initiators that have already been registered) if the option `io_port_list` is not specified in cinder.conf.

When a comma-separated list is given to `io_port_list`, the driver will only register the initiator to the ports specified in the list and only return FC target port(s) which belong to the target ports in the `io_port_list` instead of all target ports.

Here is an example

    io_port_list=a-1,B-3

`a` or `B` is **Storage Processor**, number `1` and `3` are **Port ID**.

Note:

* Rather than de-registered, the registered ports will be simply bypassed whatever they are in 'io_port_list' or not.

* Driver will raise an exception when attaching a volume if ports in `io_port_list` are not existed in VNX.

## Batch Processing for Volume Attaching/Detaching

Batch Processing is introduced to improve the performance of volume attaching/detaching. The driver accumulates the concurrent attaching/detaching requests and then serves the requests in batch later. Because some duplicated operations will be removed, the whole process will be more efficient. The minimum serving time of a request may increase since time is needed for requests to accumulate but the maximum serving time will be reduced as long as the time for accumulation is not excessively long. Batch processing is disabled by default.
Option `attach_detach_batch_interval` within the backend section is used to control this support

* `attach_detach_batch_interval=-1`: Batch processing is disabled. This is the default value.

* `attach_detach_batch_interval=<Number of seconds>`: Batch processing is enabled and worker threads will sleep <Number of seconds> for the requests to accumulate before it serve them in batch.

## Force Delete LUNs in Storage Groups

Some LUNs corresponding to some `available` volumes may remain in some Storage Groups in VNX array side due to some OpenStack timeout issue. But VNX arrays do not allow users to delete LUNs still in some Storage Groups. `force_delete_lun_in_storagegroup` is introduced to allow users to delete the `available` volumes in this tricky situation.

When `force_delete_lun_in_storagegroup=True` in the backend section, the driver will move the LUN out of Storage Groups and then delete the LUN if the user try to delete some volume whose corresponding LUN remains in some Storage Groups in the VNX array.

The default value of `force_delete_lun_in_storagegroup` is `False`.
