# DELL EMC VNX Driver for Newton

## Versions
|       | Liberty | Mitaka | Newton | Ocata | Pike   | Queens | Rocky  | Stein  |
|-------|---------|--------|--------|-------|--------|--------|--------|--------|
| Unity | 0.3.x   | 0.4.x  | 0.5.x  | 1.x.x | 2.x.x  | 3.x.x  | 4.x.x  | 5.x.x  |
| VNX   | 6.x.x   | 7.x.x  | 8.x.x  | 9.x.x | 10.x.x | 11.x.x | 12.x.x | 13.x.x |

## License
    Copyright (c) 2016 EMC Corporation, Inc.
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

## How to deploy
1. Copy the `cinder/volume/drivers/emc/vnx` folder to where Cinder codes are deployed.
2. [Optional] Copy the `cinder/tests/unit/volume/drivers/emc` folder to the Cinder unit-test folder.

## How to configure
Please refer to the configure reference [here](https://docs.openstack.org/ocata/config-reference/block-storage/drivers/emc-vnx-driver.html)
