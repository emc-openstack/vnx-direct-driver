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

## Detailed Usage

See `README_ISCSI.md` and `README_FC.md` for detailed information.

## Driver Repositories

* The first release of the driver for a specific OpenStack release will
be available in the corresponding branch of
[OpenStack Official Github](https://github.com/openstack/cinder) along
with the GA of the OpenStack release.
* If a following update of the driver for the same OpenStack release is
published, the branch of
[EMC Github](https://github.com/emc-openstack/vnx-direct-driver) will be used
and the version tag (the middle version number or the minor version number will
be increased and the major version number is the same as the one in OpenStack
Official Github) will be added.
* The supported OpenStack release of the driver in EMC Github will be
explicitly stated in the corresponding `README_ISCSI.md` and `README_FC.md`.
