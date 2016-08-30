# Advanced Features

## Read-only volumes

OpenStack supports read-only volumes.  The following command can be used to set a
volume as read-only.

        cinder --os-username admin --os-tenant-name admin readonly-mode-update <volume> True

After a volume is marked as read-only, the driver will forward the information
when a hypervisor is attaching the volume and the hypervisor will make sure the
volume is read-only.

## Snap copy

* Metadata Key: `snapcopy`
* Possible Values:
    * `True` or `true`
    * `False` or `false`
* Default: `False`

VNX driver supports snap copy which extremely accelerates the process for
creating a copied volume.

By default, the driver will do full data copy when creating a
volume from a snapshot or cloning a volume, which is time-consuming especially
for large volumes.  When snap copy is used, driver will simply create a snapshot
and mount it as a volume for the 2 kinds of operations which will be instant
even for large volumes.

To enable this functionality, user should append `--metadata snapcopy=True`
when creating cloned volume or creating volume from snapshot.

        cinder create --source-volid <source-void> --name "cloned_volume" --metadata snapcopy=True

or

        cinder create --snapshot-id <snapshot-id> --name "vol_from_snapshot" --metadata snapcopy=True


Then the newly created volume will be in fact a snap copy instead of a full copy.
If a full copy is needed, retype/migration can be used to convert the
snap-copy volume to a full-copy volume which may be time-consuming.

User can determine whether the volume is a snap-copy volume or not by
showing its metadata. If the `snapcopy` in metadata is `True` or `true` , the volume is a
snap-copy volume. Otherwise, it is a full-copy volume.

        cinder metadata-show <volume>

__Constraints:__

* The number of snap-copy volume created from a single source volume is limited to
  255 at one point in time.
* The source volume which has snap-copy volume can not be deleted or migrated.
* snapcopy volume will be change to full-copy volume after host-assisted or storage-assisted migration.
* snapcopy volume can not be added to consisgroup because of VNX limitation.

## Efficient non-disruptive volume backup

The default implementation in Cinder for non-disruptive volume backup is not
efficient since a cloned volume will be created during backup.

The approach of efficient backup is to create a snapshot for the volume and
connect this snapshot (a mount point in VNX) to the Cinder host for volume
backup. This eliminates migration time involved in volume clone.

## Configurable migration rate

VNX Cinder driver are leveraging the LUN migration from the VNX. LUN migration is involved in cloning/migrating/retyping and creating volume from snapshot. when admin set `migrate_rate` in volume's `metadata`, VNX driver can start migration with specified rate. The available values for the `migrate_rate` are `high`, `asap`, `low` and `medium`

Here is the example to set `migrate_rate` to `asap`

    cinder metadata <volume-id> set migrate_rate=asap

After set, any cinder volume operations involving VNX LUN migration will take the value as the migration rate.

to restore the migration rate to default, unset the metadata as following

    cinder metadata <volume-id> unset migrate_rate

__Note:__

* Do not use the `asap` migration rate when the system is in production,
as the normal host I/O may be interrupted.
Use asap only when the system is offline (free of any host-level I/O).


## Replication v2.1 support

Cinder introduces Replication v2.1 support in Mitaka, it supports fail-over/fail-back replication for specific back end.
In VNX Cinder driver, **MirrorView** is used to setup replication for the volume.

To enable this feature, user needs to set configuration in cinder.conf as below:

    replication_device = backend_id:<secondary VNX serial number>,
                         san_ip:192.168.1.2,
                         san_login:admin,
                         san_password:admin,
                         naviseccli_path:/opt/Navisphere/bin/naviseccli,
                         storage_vnx_authentication_type:global,
                         storage_vnx_security_file_dir:

Currently, only synchronized mode **MirrorView** is supported, and one volume can only have 1 secondary storage system, so
you can have only one `replication-device` presented in driver configuration section.

To create a replication enabled volume, user needs to create a volume type first:

    cinder type-create replication-type
    cinder type-key replication-type set replication_enabled="<is> True"

and, then create volume with above volume type:

    cinder create --volume-type replication-type --name replication-volume 1

### Supported operations

* Create volume
* Create cloned volume
* Create volume from snapshot
* Fail-over volume (via `cinder failover-host <secondary VNX serial number>`)
* Fail-back volume (via `cinder failover-host default`)

### REQUIREMENTS:

* 2 VNX systems must be in same domain.
* For iSCSI MirrorView, user needs to setup iSCSI connection before enable replication in Cinder.
* For FC MirrorView, user needs to zone specific FC ports from 2 VNX system together.
* MirrorView Sync enabler( **MirrorView/S** ) installed on both systems.
* Write intent log enabled on both VNX systems.

For more information on how to configure, please refer to: [MirrorView-Knowledgebook:-Releases-30-–-33] (https://support.emc.com/docu32906_MirrorView-Knowledgebook:-Releases-30-%E2%80%93-33---A-Detailed-Review.pdf?language=en_US)