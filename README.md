# VNX Cinder Driver

Copyright (c) 2012 - 2015 EMC Corporation, Inc.
All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License"); you may
not use this file except in compliance with the License. You may obtain
a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
License for the specific language governing permissions and limitations
under the License.

## Driver Repositories

* Latest Driver: 
     * [OpenStack](https://github.com/openstack/cinder)
* Latest Driver for Kilo:
     * [EMC Github](https://github.com/emc-openstack/vnx-direct-driver/tree/kilo)
* Latest Driver for Juno:
     * [EMC Github](https://github.com/emc-openstack/vnx-direct-driver/tree/juno)
* Latest Driver for Icehouse: 
     * [EMC Github](https://github.com/emc-openstack/vnx-direct-driver/tree/icehouse)


## Detailed Usage

EMCCLIFCDriver (EMC VNX FC driver) is based on the FibreChannelDriver  
defined in Block Storage.

EMCCLIISCSIDriver (EMC VNX iSCSI driver) is based on the ISCSIDriver defined in 
Block Storage.


### Table of Contents
* [__Overview__](vnxCinder/Overview.md)
    * Supported OpenStack Release
    * Requirements
    * Supported Operations
* [__Feature Support List__](featureSupportList.md)
* [__Preparation__](vnxCinder/Preparation.md)
    * Install Navisphere CLI
    * Check Array Software
    * Install EMC VNX Driver (Optional)
    * Network Configuration
* [__Backend Configurations__](vnxCinder/BackendConfiguration.md)
    * Minimum Configuration
    * Multi-backend Configuration
    * Required Configurations
    * Optional Configurations
* [__Volume Type Extra Spec Definition__](vnxCinder/ExtraSpec.md)
* [__Advanced Features__](vnxCinder/AdvancedFeature.md)
* [__Best Practice__](vnxCinder/BestPractice.md)
    * Multipath Setup
* [__Restrictions and Limitations__](vnxCinder/Limitation.md)
* [__Appendix__](vnxCinder/Appendix.md)
    * Authenticate by Security File
    * Register FC port with VNX
    * Register iSCSI port with VNX
