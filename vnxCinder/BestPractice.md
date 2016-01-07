# Best Practice

## Multipath Setup

Enabling multipath volume access is recommended for robust data access. The
major configuration includes:

* Install `multipath-tools`, `sysfsutils` and `sg3-utils` on nodes hosting
Nova-Compute and Cinder-Volume services (Please check the operating system
manual for the system distribution for specific installation steps.  For
Red Hat based distributions, they should be `device-mapper-multipath`,
`sysfsutils` and `sg3_utils`).
* Specify `use_multipath_for_image_xfer=true` in cinder.conf for each FC/iSCSI
back end.
* Specify `iscsi_use_multipath=True` in `libvirt` section of `nova.conf`.  This
option is valid for both iSCSI and FC driver.

For multipath-tools, here is an EMC recommended sample of `/etc/multipath.conf`.

`user_friendly_names` is not specified in the configuration and thus it will
take the default value `no`. It is NOT recommended to set it to `yes` because
it may fail operations such as VM live migration.

        blacklist {
            # Skip the files under /dev that are definitely not FC/iSCSI devices
            # Different system may need different customization
            devnode "^(ram|raw|loop|fd|md|dm-|sr|scd|st)[0-9]*"
            devnode "^hd[a-z][0-9]*"
            devnode "^cciss!c[0-9]d[0-9]*[p[0-9]*]"

            # Skip LUNZ device from VNX
            device {
                vendor "DGC"
                product "LUNZ"
                }
        }

        defaults {
            user_friendly_names no
            flush_on_last_del yes
        }

        devices {
            # Device attributed for EMC CLARiiON and VNX series ALUA
            device {
                vendor "DGC"
                product ".*"
                product_blacklist "LUNZ"
                path_grouping_policy group_by_prio
                path_selector "round-robin 0"
                path_checker emc_clariion
                features "1 queue_if_no_path"
                hardware_handler "1 alua"
                prio alua
                failback immediate
            }
        }

__Note:__

When multipath is used in OpenStack, multipath faulty devices may come out in
Nova-Compute nodes due to different issues ([Bug 1336683](https://bugs.launchpad.net/nova/+bug/1336683)
is a typical example).

A solution to completely avoid faulty devices has not
been found yet.  `faulty_device_cleanup.py` mitigates this issue when VNX iSCSI
storage is used. Cloud administrators can deploy the script in all Nova-Compute
nodes and use a CRON job to run the script on each Nova-Compute node
periodically so that faulty devices will not stay too long. Please refer to:

https://github.com/emc-openstack/vnx-faulty-device-cleanup

for detailed usage and the script.
