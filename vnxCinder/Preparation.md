# Preparation

## Install Navisphere CLI

Navisphere CLI needs to be installed on all Block Storage nodes within an
OpenStack deployment.  You need to download different versions for different
platform.

* [DEB package For Ubuntu x64](https://github.com/emc-openstack/naviseccli/blob/master/navicli-linux-64-x86-en-us_7.33.2.0.51-1_all.deb?raw=true)
* [Other platforms For VNX2 Series](https://support.emc.com/downloads/36656_VNX2-Series)
* [Other platforms For VNX1 Series](https://support.emc.com/downloads/12781_VNX1-Series).

After installation, set the security level of Navisphere CLI to low:

    /opt/Navisphere/bin/naviseccli security -certificate -setLevel low

## Check Array Software

Make sure your have following software installed for certain features.

| Feature                                | Software Required |
|----------------------------------------|-------------------|
| All                                    | ThinProvisioning  |
| All                                    | VNXSnapshots      |
| FAST cache support                     | FASTCache         |
| Create volume with type `compressed`   | Compression       |
| Create volume with type `deduplicated` | Deduplication     |

You can check the status of your array software in the "Software" page of
"Storage System Properties".  Here is how it looks like.

![example](../imgs/enabler.png)

## Install EMC VNX Driver (Optional)

__Notes:__

EMC VNX FC driver and iSCSI driver is included in cinder.  You don't need to
install them separately.

Following these instructions __only if__ you want to install a specialized version
 of the EMC VNX driver.

EMC VNX FC driver provided in the installer package consists of following files:

        emc_vnx_cli.py
        emc_cli_fc.py

EMC VNX iSCSI driver consists of following files:

        emc_vnx_cli.py
        emc_cli_iscsi.py

You could retrieve them from [EMC Github](https://github.com/emc-openstack/vnx-direct-driver)
Copy files to `.../cinder/volume/drivers/emc/`
directory of the OpenStack node(s) for cinder-volume.

## Network Configuration

For FC Driver, FC zoning is properly configured between hosts and VNX.
Check __Register FC Port with VNX__ in [Appendix](Appendix.md) for reference.

For iSCSI Driver, make sure your VNX iSCSI port is accessible by your hosts.
Check __Register iSCSI Port with VNX__ in [Appendix](Appendix.md) for reference.

User could use `initiator_auto_registration=True` configuration to avoid register
the ports manually.  Please check the detail of the configuration in
[Backend Configuration](BackendConfiguration.md).

If you are trying to setup multipath, please refer to __Multipath Setup__ in
[Best Practice](BestPractice.md) section.
