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
when creating cloned volume or creating volume from snapshot. Then the newly
created volume will be in fact a snap copy instead of a full copy.
If a full copy is needed, retype/migration can be used to convert the
snap-copy volume to a full-copy volume which may be time-consuming.

        cinder create --source-volid <source-void> --name "cloned_volume" --metadata snapcopy=True

or

        cinder create --snapshot-id <snapshot-id> --name "vol_from_snapshot" --metadata snapcopy=True

User can determine whether the volume is a snap-copy volume or not by
showing its metadata. If the `snapcopy` in metadata is `True` or `true` , the volume is a
snap-copy volume. Otherwise, it is a full-copy volume.

        cinder metadata-show <volume>

__Constraints:__

* The number of snap-copy volume created from a single source volume is limited to
  255 at one point in time.
* The source volume which has snap-copy volume can not be deleted.
* The source volume which has snap-copy volume can not be migrated.
* snapcopy volume will be change to full-copy volume after host-assisted or storage-assisted migration.
* snapcopy volume can not be added to consisgroup because of VNX limitation.

## Efficient non-disruptive volume backup

The default implementation in Cinder for non-disruptive volume backup is not
efficient since a cloned volume will be created during backup.

The approach of efficient backup is to create a snapshot for the volume and
connect this snapshot (a mount point in VNX) to the Cinder host for volume
backup. This eliminates migration time involved in volume clone.
