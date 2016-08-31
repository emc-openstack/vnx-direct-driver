# Advanced Features

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

## Asynchronous migration support

* Metadata Key: `async_migrate`
* Possible Values:
    * `True` or `true`
    * `False` or `false`
* Default: `False`

Currently VNX Cinder driver leverages LUN migration when creating a cloned
volume or a volume from snapshot.
By default, user has to wait a long time before the volume becomes
`available`.

Now user can add `--metadata async_migrate=true` when creating a cloned volume
or a volume from snapshot, which makes the volume `available` when LUN
migration starts.

Examples:

        cinder create --source-volid <source-void> --name "cloned_volume" --metadata async_migrate=True

or

        cinder create --snapshot-id <snapshot-id> --name "vol_from_snapshot" --metadata async_migrate=True

__Constraints:__

* Source volume cannot be deleted/migrated/retyped before
  migration completes on VNX.
* Following operations cannot be performed on the newly created volume before
  migration completes on VNX.
  - Migrate volume
  - Retype volume
  - Take snapshot

## Efficient non-disruptive volume backup

The default implementation in Cinder for non-disruptive volume backup is not
efficient since a cloned volume will be created during backup.

The approach of efficient backup is to create a snapshot for the volume and
connect this snapshot (a mount point in VNX) to the Cinder host for volume
backup. This eliminates migration time involved in volume clone.
