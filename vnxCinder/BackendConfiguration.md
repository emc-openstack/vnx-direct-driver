# Backend configurations

Make the following changes in the configuration file `/etc/cinder/cinder.conf`:

The configuration options below are specific for EMC VNX driver:

__Note:__

__Changes to your configuration won't take effect until your restart your cinder
service.__  You can restart your cinder service with following commands in
Ubuntu and Debian.

        service cinder-volume restart
        service cinder-api restart

If you are using devstack or other platforms, join the stack screen session and
restart your c-sch and c-vol session.

## Minimum Configuration

Here is a sample of minimum backend configuration.
See following sections for the detail of each option
Replace `EMCCLIFCDriver` to `EMCCLIISCSIDriver` if your are using the iSCSI
driver.

        [DEFAULT]
        enabled_backends = vnx_array1

        [vnx_array1]
        san_ip = 10.10.72.41
        san_login = sysadmin
        san_password = sysadmin
        naviseccli_path = /opt/Navisphere/bin/naviseccli
        volume_driver=cinder.volume.drivers.emc.emc_cli_fc.EMCCLIFCDriver
        initiator_auto_registration=True

## Multi-backend configuration

Here is a sample of a multi-backend configuration.
See following sections for the detail of each option.
Replace `EMCCLIFCDriver` to `EMCCLIISCSIDriver` if your are using the iSCSI
driver.

        [DEFAULT]
        enabled_backends=backendA, backendB

        [backendA]
        storage_vnx_pool_names = Pool_01_SAS, Pool_02_FLASH
        san_ip = 10.10.72.41
        storage_vnx_security_file_dir = /etc/secfile/array1
        naviseccli_path = /opt/Navisphere/bin/naviseccli
        volume_driver=cinder.volume.drivers.emc.emc_cli_fc.EMCCLIFCDriver
        initiator_auto_registration=True

        [backendB]
        storage_vnx_pool_names = Pool_02_SAS
        san_ip = 10.10.26.101
        san_login = username
        san_password = password
        naviseccli_path = /opt/Navisphere/bin/naviseccli
        volume_driver=cinder.volume.drivers.emc.emc_cli_fc.EMCCLIFCDriver
        initiator_auto_registration=True

For more details on multi-backends, see [OpenStack Cloud Administration Guide](http://docs.openstack.org/admin-guide-cloud/index.html)

## Required Configurations

### IP of the VNX Storage Processors

Specify the SP A and SP B IP to connect.

    san_ip = <IP of VNX Storage Processor A>
    san_secondary_ip = <IP of VNX Storage Processor B>

### VNX Login Credentials

There are two ways to specify the credentials.

* Use plain text username and password.

Supply for plain username and password as below.

    san_login = <VNX account with administrator role>
    san_password = <password for VNX account>
    storage_vnx_authentication_type = global

Valid values for `storage_vnx_authentication_type` are: `global` (default),
`local`, `ldap`

* Use Security file

This approach avoid the plain text password in your cinder configuration file.
Supply a security file as below:

    storage_vnx_security_file_dir=<path to security file>

Please check Unisphere CLI user guide or the __Authenticate by Security File__
section in [Appendix](Appendix.md) for how to create a security file.


### Path to your Unisphere CLI

Specify the absolute path to your naviseccli.

    naviseccli_path = /opt/Navisphere/bin/naviseccli

### Driver name

* For FC Driver, add following option:

```
        volume_driver=cinder.volume.drivers.emc.emc_cli_fc.EMCCLIFCDriver
```

* For iSCSI Driver, add following option:

```
        volume_driver=cinder.volume.drivers.emc.emc_cli_iscsi.EMCCLIISCSIDriver
```

## Optional Configurations

### VNX Pool Names

Specify the list of pools to be managed, separated by ','.  They should already
exist in VNX.

    storage_vnx_pool_names = pool 1, pool 2

If this value is not specified, all pools of the array will be used.

### Initiator auto registration

When `initiator_auto_registration=True`, the driver will automatically register
initiators to all working target ports of the VNX array during volume
attaching (The driver will skip those initiators that have already been
registered) if the option `io_port_list` is not specified in cinder.conf.

If the user wants to register the initiators with some specific ports but not
register with the other ports, this functionality should be disabled.

When a comma-separated list is given to `io_port_list`, driver will only
register the initiator to the ports specified in the list and only return target
port(s) which belong to the target ports in the `io_port_list` instead of all
target ports.

  * Example for FC ports:

        io_port_list=a-1,B-3

    `a` or `B` is **Storage Processor**, number `1` and `3` are **Port ID**.

  * Example for iSCSI ports:

        io_port_list=a-1-0,B-3-0

    `a` or `B` is **Storage Processor**, the first numbers `1` and `3` are
    **Port ID** and the second number `0` is **Virtual Port ID**

__Note:__

* Rather than de-registered, the registered ports will be simply bypassed
whatever they are in 'io_port_list' or not.
* Driver will raise an exception if ports in `io_port_list` are not existed in
VNX during startup.

### Force delete volumes in storage group

Some `available` volumes may remain in storage group on the VNX array due to
some OpenStack timeout issue. But the VNX array do not allow the user to delete
the volumes which are in storage group. Option `force_delete_lun_in_storagegroup`
is introduced to allow the user to delete the `available` volumes in this
tricky situation.

When `force_delete_lun_in_storagegroup=True` in the back-end section, the driver
will move the volumes out of storage groups and then delete them if the user
tries to delete the volumes that remain in storage group on the VNX array.

The default value of `force_delete_lun_in_storagegroup` is `False`.

### Over subscription in thin provisioning

Over subscription allows that the sum of all volumes' capacity (provisioned
capacity) to be larger than the pool's total capacity.

`max_over_subscription_ratio` in the back-end section is the ratio of
provisioned capacity over total capacity.

The default value of `max_over_subscription_ratio` is 20.0, which means the
provisioned capacity can not exceed the total capacity. If the value of this
ratio is set larger than 1.0, the provisioned capacity can exceed the total
capacity.

### Storage group automatic deletion

For volume attaching, the driver has a storage group on VNX for each compute
node hosting the vm instances which are going to consume VNX Block Storage
(using compute node's hostname as storage group's name). All the volumes
attached to the vm instances in a computer node will be put into the storage
group. If `destroy_empty_storage_group=True`, the driver will remove the empty
storage group after its last volume is detached.
For data safety, it does not suggest to set `destroy_empty_storage_group=True`
unless the VNX is exclusively managed by one Block Storage node because
consistent lock_path is required for operation synchronization for this behavior.

### Initiator auto deregistration

Enabling storage group automatic deletion is the precondition of this function.
If `initiator_auto_deregistration=True` is set, the driver will deregister all the
initiators of the host after its storage group is deleted.

### FC SAN auto zoning

EMC VNX FC driver supports FC SAN auto zoning when ZoneManager is configured.
Set `zoning_mode` to `fabric` in `DEFAULT` section to enable this feature. For
ZoneManager configuration, please refer to Block Storage official guide.

###  Volume number threshold

In VNX, there is a limitation on the number of pool volumes that can be created
in the system. When the limitation is reached, no more pool volumes can be created
even if there is remaining capacity in the storage pool.  In other words,
if the scheduler dispatches a volume creation request to a back end that
has free capacity but reaches the volume limitation, the creation fails.

The default value of `check_max_pool_luns_threshold` is `False`. When
`check_max_pool_luns_threshold=True`, the pool-based back end will check the
limit and will report 0 free capacity to the scheduler if the limit is reached.
So the scheduler will be able to skip this kind of pool-based back end that runs
out of the pool volume number.

### iSCSI Initiators

`iscsi_initiators` is a dictionary of IP addresses of the iSCSI initiator ports
on OpenStack Nova/Cinder nodes which want to connect to VNX via iSCSI. If this option is
configured, the driver will leverage this information to find an accessible
iSCSI target portal for the initiator when attaching volume. Otherwise, the
iSCSI target portal will be chosen in a relative random way.

__This option is only valid for iSCSI driver.__

Here is an example.  VNX will connect `host1` with `10.0.0.1` and `10.0.0.2`.
And it will connect `host2` with `10.0.0.3`.

The key name (like `host1` in the example) should be the output of command
`hostname`.

        iscsi_initiators = {"host1":["10.0.0.1", "10.0.0.2"],"host2":["10.0.0.3"]}

### Default Timeout

Specify the timeout(minutes) for operations like LUN migration, LUN creation, etc.
For example, LUN migration is a typical long running operation, which depends
on the LUN size and the load of the array.  An upper bound in the specific
deployment can be set to avoid unnecessary long wait.

The default value for this option is infinite.

Example:

        default_timeout = 10

### Max LUNs per storage group

`max_luns_per_storage_group` specify the max number of LUNs in a storage group.
Default value is 255.  It is also the max value supported by VNX.

### Ignore Pool Full Threshold

if `ignore_pool_full_threshold` is set to `True`, driver will force LUN creation
even if the full threshold of pool is reached.  Default to `False`
