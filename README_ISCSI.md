# VNX iSCSI Direct Driver for Icehouse

Copyright (c) 2012 - 2014 EMC Corporation
All Rights Reserved

Licensed under EMC Freeware Software License Agreement
You may not use this file except in compliance with the License.
You may obtain a copy of the License at

        https://github.com/emc-openstack/freeware-eula/
        blob/master/Freeware_EULA_20131217_modified.md

## Overview

EMCCLIISCSIDriver (a.k.a. VNX iSCSI Direct Driver) is based on the ISCSIDriver defined in Cinder, with the ability to create/delete, attach/detach volumes, create/delete snapshots, etc. 

EMCSMISISCSIDriver performs the volume operations by executing Navisphere CLI. 

The Navisphere CLI (a.k.a. NaviSecCLI) is a Command Line Interface (CLI) used for management, diagnostics and reporting functions for VNX.

## Supported OpenStack Release

This driver supports Icehouse and newer release. Compared to version in the official OpenStack Github master branch, here are the enhancements:

* Multiple Pools Support
* Connectivity Check for iSCSI Portal Selection
* Initiator Auto Registration
* Storage Group Auto Deletion
* Multiple Authentication Support
* Storage-Assisted Volume Migration
* SP Toggle for HA

## Requirements

* Flare version 5.32 or higher.
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
* For all other variants of Linux, Navisphere CLI is available at [EMC Support](https://support.emc.com/downloads/5890_Navisphere-Agents-CLI---Linux).


### Install Cinder driver

EMC VNX iSCSI Direct Driver provided in the installer package consists of two python files:

		emc_vnx_cli.py
		emc_cli_iscsi.py
                                
Copy the above two files to the `cinder/volume/drivers/emc/` directory of your OpenStack node(s) where cinder-volume is running.

### Register with VNX

To access the storage in VNX, Compute nodes need to be registered with VNX first.

To perform "Copy Image to Volume" and "Copy Volume to Image" operations, nodes running the cinder-volume service(Cinder nodes) must be registered with the VNX as well.

Below mentioned steps are for a Compute node.Please follow the same steps for Cinder nodes also. (The steps can be skipped if Initiator Auto Registration is to be enabled)

* On the node with IP address 10.10.61.1, do the following (assume 10.10.61.35 is the iscsi target):

        # Start iSCSI initiator service on the node
        $ sudo /etc/init.d/open-iscsi start
        # Discover iSCSI target portals on VNX
        $ sudo iscsiadm -m discovery -t st -p 10.10.61.35
        $ cd /etc/iscsi
        # Find out the IQN of the node
        $ sudo more initiatorname.iscsi

* Log in to VNX from the Compute node using the target corresponding to the SPA port:

        $ sudo iscsiadm -m node -T iqn.1992-04.com.emc:cx.apm01234567890.a0 -p 10.10.61.35 -l
* Assume `iqn.1993-08.org.debian:01:1a2b3c4d5f6g` is the initiator name of the Compute node. Register `iqn.1993-08.org.debian:01:1a2b3c4d5f6g` in Unisphere
    * Login to Unisphere, go to `FNM0000000000->Hosts->Initiators`,
    * Refresh and wait until initiator `iqn.1993-08.org.debian:01:1a2b3c4d5f6g` with SP Port `A-8v0` appears.
    * Click the `Register` button, select `CLARiiON/VNX` and enter the host name and IP address:
	    * Hostname : myhost1
	    * IP : 10.10.61.1
	    * Click Register. 
    * Now host 10.10.61.1 will appear under Hosts->Host List as well.
* Log out iSCSI on the node:

		$ sudo iscsiadm -m node -u

* Log in to VNX from the Compute node using the target corresponding to the SPB port:

        $ sudo iscsiadm -m node -T iqn.1992-04.com.emc:cx.apm01234567890.b8 -p 10.10.61.36 -l

* In Unisphere register the initiator with the SPB port.

* Log out iSCSI on the node:

        $ sudo iscsiadm -m node -u
* Register the IQN to more ports if needed.

## Backend Configuration

Make the following changes in `/etc/cinder/cinder.conf`:

Following are the elements specific to VNX CLI driver to be configured

        storage_vnx_pool_name = Pool_01_SAS
        san_ip = 10.10.72.41
        san_secondary_ip = 10.10.72.42
        san_login = username
        san_password = password
        storage_vnx_authentication_type = global
        naviseccli_path = /opt/Navisphere/bin/naviseccli
        # timeout in minutes
        default_timeout = 10
        volume_driver=cinder.volume.drivers.emc.emc_cli_iscsi.EMCCLIISCSIDriver
        destroy_empty_storage_group = False
        iscsi_initiators = {"node1hostname":["10.0.0.1", "10.0.0.2"],"node2hostname":["10.0.0.3"]}

        [database]
        max_pool_size=20
        max_overflow=30



* where `san_ip` is one of the SP IP address of the VNX array. `san_secondary_ip` is the other SP IP address of VNX array. `san_secondary_ip` is an optional field, it serve the purpose of providing a high availability(HA) design. Based on that if one of the SP is down, the other SP can be connected automatically. `san_ip` is a mandatory field, which provide the main connection.
* where `Pool_01_SAS` is the pool user wants to create volume from. The pools can be created using Unisphere for VNX. Refer to the following "Multiple Pools and Thick/Thin Provisioning" section on how to support thick/thin provisioning
* where `iscsi_initiators` is a dicstionary of IP addressess iSCSI initiator ports of all OpenStack nodes that want to VNX Block Storage via iSCSI. If this option is configured, the driver will leverage this information to find a accessible iSCSI target portal for the initiator when attaching volumes. Otherwise, the iSCSI target portal will be chosen in a relative random way.
* Restart of cinder-volume service is needed to make the configuration change take effect.

## Authentication

VNX credentials are needed so that the driver could talk with the VNX system. Credentials in Global, Local and LDAP scopes are supported. 

The credentials can be specified in /etc/cinder/cinder.conf by below 3 options:

        #VNX user name
        san_login = username
        #VNX user password
        san_password = password
        #VNX user type. Valid values are: global, local and ldap. global is the default value
        storage_vnx_authentication_type = ldap

Alternatively, if all cinder backends with VNX Direct Driver deployed on the same Cinder node share the same credentials to access arrays, credentials can also be provided by the standard Navisphere CLI security file as follows.

1. Find out the Linux User ID of the `/usr/bin/cinder-volume` processes. Assuming the service /usr/bin/cinder-volume is running with account `cinder`
2. Switch to root account

        $ sudo su
3. Change `cinder:x:113:120::/var/lib/cinder:/bin/false` to `cinder:x:113:120::/var/lib/cinder:/bin/bash` in `/etc/passwd` 
    (This temporary change is to make step 4 work.)
4. Save the credentials on behave of `cinder` user (assuming the array credentials are `admin/admin` in `global` scope)

        $ su -l cinder -c '/opt/Navisphere/bin/naviseccli -AddUserSecurity -user admin -password admin -scope 0'`
5.	Change `cinder:x:113:120::/var/lib/cinder:/bin/bash` back to `cinder:x:113:120::/var/lib/cinder:/bin/false` in /etc/passwd
6.	Remove the credentials from `/etc/cinder/cinder.conf`
7.	Restart cinder-volume service to make the change take effect

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
        san_login = username
        san_password = password
        storage_vnx_authentication_type = global
        naviseccli_path = /opt/Navisphere/bin/naviseccli
        default_timeout = 10
        volume_driver=cinder.volume.drivers.emc.emc_cli_iscsi.EMCCLIISCSIDriver
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
        san_login = username
        san_password = password
        naviseccli_path = /opt/Navisphere/bin/naviseccli
        # Timeout in Minutes
        default_timeout = 10
        volume_driver=cinder.volume.drivers.emc.emc_cli_iscsi.EMCCLIISCSIDriver
        destroy_empty_storage_group = False
        initiator_auto_registration=True

        [backendB]
        storage_vnx_pool_name = Pool_02_SAS
        san_ip = 10.10.26.101
        san_login = username
        san_password = password
        naviseccli_path = /opt/Navisphere/bin/naviseccli
        # Timeout in Minutes
        default_timeout = 10
        volume_driver=cinder.volume.drivers.emc.emc_cli_iscsi.EMCCLIISCSIDriver
        destroy_empty_storage_group = False
        initiator_auto_registration=True

        [database]

        max_pool_size=20
        max_overflow=30

For more details on multi-backend, see [OpenStack Administration Guide](http://docs.openstack.org/admin-guide-cloud/content/multi_backend.html)

## Restriction of deployment

It is not suggest to deploy the driver on Nova Compute Node if "cinder upload-to-image --force True" is to be used against an in-use volume. Otherwise, "cinder upload-to-image --force True" will terminate the VM instance's data access to the volume.

## Restriction of volume extension

VNX does not support to extend the thick volume which has snapshot. If user tries to extend a volume which has snapshot, status of the volume would change to "error_extending".

## Initiator Auto Registration

When initiator_auto_registration=True, the driver will automatically register iSCSI initiators to all working iSCSI target ports of the VNX array during volume attaching (The driver will skip those initiators that have already been registered)

If you want to register some initiators only on some specific ports and don't want them to be registered on other ports, this functionality should be disabled.