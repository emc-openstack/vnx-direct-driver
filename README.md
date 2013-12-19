VNX iSCSI Direct Driver for Havana
==================================

Copyright (c) 2013 EMC Corporation
All Rights Reserved

Licensed under EMC Freeware Software License Agreement
You may not use this file except in compliance with the License.
You may obtain a copy of the License at

        https://github.com/emc-openstack/freeware-eula/
        blob/master/Freeware_EULA_20131217_modified.mdOverview

Overview
=========
	The OpenStack project is an open source cloud computing platform to meet the needs of public and private            clouds. Cinder is the Block Storage module of OpenStack which enables management of volumes, volume                 snapshots, and volume types.

	EMC VNX CLI Cinder driver is based on the ISCSIDriver defined in Cinder, with the ability to create/delete          attach/detach volumes and create/delete snapshots, etc. This Cinder driver executes the volume operations
	by communicating with the backend EMC storage through NaviSec CLI.


Requirements
=============

OpenStack Release
-----------------
	Havana
	Grizzly

Supported VNX arrays
---------------------
	VNX Series - With VNX Block Storage

Flare 
-----------------
        Flare version 5.32 or higher.
	
Pre-Requisites
-----------------	
	VNX Snapshot and Clone license must be activated for the array.
	All the iSCSI ports from the VNX should be accessible via OpenStack hosts.

Naviseccli (Navishere CLI tool)
-------------------------------
	Navisphere CLI v7.32 or higher	



Supported Operations
=======================
The following operations will be supported on VNX arrays:
        Create volume
	Delete volume
	Attach volume
	Detach volume
	Create snapshot
	Delete snapshot
	Create volume from snapshot
	Create cloned volume
	Copy Image to Volume
	Copy Volume to Image
	
Only thin provisioning is supported by the EMC Cinder driver.

Preparation
===============

Install NaviSec CLI 
-------------------
	NaviSec CLI tool needs to be installed in Controller node and all the Cinder nodes in an OpenStack Deployment.

	For Ubuntu x64 platform, use the custom installation package naviseccli.tgz as follows:
		* Change directory to naviseccli-files and execute install.sh file.
		* cd naviseccli-files/
		* sh install.sh

	For all the other variants of Linux, download the NaviSec CLI installer from EMC's Website at following location:
	https://support.emc.com/downloads/5890_Navisphere-Agents-CLI---Linux

Install Cinder driver
-----------------------
	EMC VNX CLI Cinder driver provided in the installer package consists of two python files:
					emc_vnx_cli.py and emc_cli_iscsi.py.
					
	Copy the above two files to the cinder/volume/drivers/emc/ directory of your OpenStack node(s) 
	where cinder-volume is running.This directory is where other Cinder drivers are located.

Register with VNX
------------------
	For a VNX volume to be exported to a Compute node, the node needs to be registered with VNX first.
	
	For performing "Copy Image to Volume" & "Copy Volume to Image" operations, nodes running 
	the cinder-volume service(Cinder nodes) must be registered with the VNX.
	
	Below mentioned steps are for a Compute node.Please follow the same steps for Cinder nodes also.
	
	On the Compute node 1.1.1.1, do the following (assume 10.10.61.35 is the iscsi target):
	$ sudo /etc/init.d/open-iscsi start
	$ sudo iscsiadm -m discovery -t st -p 10.10.61.35
	$ cd /etc/iscsi
	$ sudo more initiatorname.iscsi
	$ iscsiadm -m node

	Log in to VNX from the Compute node using the target corresponding to the SPA port:
	* $ sudo iscsiadm -m node -T iqn.1992-04.com.emc:cx.apm01234567890.a0 -p 10.10.61.35 -l
	* Assume "iqn.1993-08.org.debian:01:1a2b3c4d5f6g" is the initiator name of the Compute node. 
	* Login to Unisphere, go to VNX00000->Hosts->Initiators,
	* Refresh and wait until initiator "iqn.1993-08.org.debian:01:1a2b3c4d5f6g" with SP Port "A-8v0" appears.
	* Click the "Register" button, select "CLARiiON/VNX" and enter the host name and IP address:
	* Hostname : myhost1 (please enter the hostname of your Ubuntu host only)
	* IP : 1.1.1.1
	* Click Register. Now host 1.1.1.1 will appear under Hosts->Host List as well.

	Log out of VNX on the Compute node:
	* $ sudo iscsiadm -m node -u

	Log in to VNX from the Compute node using the target corresponding to the SPB port:
	* $ sudo iscsiadm -m node -T iqn.1992-04.com.emc:cx.apm01234567890.b8 -p 10.10.10.11 -l

	In Unisphere register the initiator with the SPB port.

	Log out:
	* $ sudo iscsiadm -m node -u

Setup
========
Normal configuration
-----------------------
	Make the following changes in /etc/cinder/cinder.conf:

	Following are the elements specific to VNX CLI driver to be configured 

	iscsi_pool_id = 1
	iscsi_ip_address = 10.10.61.35
	storage_vnx_ip_address = 10.10.72.41
	storage_vnx_username = username
	storage_vnx_password = password
	naviseccli_path = /opt/Navisphere/bin/naviseccli
	#Timeout in Minutes
	default_timeout = 10
	volume_driver=cinder.volume.drivers.emc.emc_cli_iscsi.EMCCLIISCSIDriver

	[database]
	max_pool_size=20
	max_overflow=30

	* where 10.10.61.35 is the IP address of the VNX iSCSI target and 10.10.72.41 is 
	        the IP address of the VNX array.
	* Restart the cinder-volume service after configuring the above options.

Multi-backend configuration (from the same host)
----------------------------
	enabled_backends=driverA, driverB

	[driverA]
	iscsi_pool_id = 1
	iscsi_ip_address = 10.10.61.35
	storage_vnx_ip_address = 10.10.72.41
	storage_vnx_username = username
	storage_vnx_password = password
	naviseccli_path = /opt/Navisphere/bin/naviseccli
	#Timeout in Minutes
	default_timeout = 10
	volume_driver=cinder.volume.drivers.emc.emc_cli_iscsi.EMCCLIISCSIDriver

	[driverB]
	iscsi_pool_id = 1
	iscsi_ip_address = 10.10.101.40
	storage_vnx_ip_address = 10.10.26.101
	storage_vnx_username = username
	storage_vnx_password = password
	naviseccli_path = /opt/Navisphere/bin/naviseccli
	#Timeout in Minutes
	default_timeout = 10
	volume_driver=cinder.volume.drivers.emc.emc_cli_iscsi.EMCCLIISCSIDriver

	[database]
	max_pool_size=20
	max_overflow=30

	* Restart the cinder-volume service after configuring the above options.

	* For more reading about multi-backend :
	http://docs.openstack.org/admin-guide-cloud/content//multi_backend.html
        

        
