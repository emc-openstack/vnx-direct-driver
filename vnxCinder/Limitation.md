# Restrictions and Limitations

## iSCSI port cache

EMC VNX iSCSI driver caches the iSCSI ports information, so that the user
should restart the cinder-volume service or wait for seconds
(which is configured by `periodic_interval` in `cinder.conf`) before any volume
attachment operation after changing the iSCSI port configurations.  Otherwise
the attachment may fail because the old iSCSI port configurations were used.

## No extending for volume with snapshots

VNX does not support extending the thick volume which has a snapshot.
If the user tries to extend a volume which has a snapshot, the status of the
volume would change to `error_extending`.

## Limitations for deploying cinder on computer node

It is not recommended to deploy the driver on a compute node if
`cinder upload-to-image --force True` is used against an in-use volume.
Otherwise, `cinder upload-to-image --force True` will terminate the data
access of the vm instance to the volume.

## Storage group with host names in VNX

When the driver notices tht there is no existing storage group that has the
host name as the storage group name, it will create the storage group and also
add the compute node's or Block Storage nodes' registered initiators into the
storage group.

If the driver notices that the storage group already exists, it will assume
that the registered initiators have also been put into it and skip the
operations above for better performance.

It is recommended that the storage administrator does not create the storage
group manually and instead relies on the driver for the preparation. If the
storage administrator needs to create the storage group manually for some
special requirements, the correct registered initiators should be put into the
storage group as well (otherwise the following volume attaching operations will
fail  ).

## EMC storage-assisted volume migration

EMC VNX driver supports storage-assisted volume migration, when the user
starts migrating with `cinder migrate --force-host-copy False <volume_id> <host>`
or `cinder migrate <volume_id> <host>`, cinder will try to leverage the VNX's
native volume migration functionality.

In following scenarios, VNX storage-assisted volume migration will not be
triggered:

1. Volume migration between back ends with different storage protocol, ex,
   FC and iSCSI.
2. Volume is to be migrated across arrays.
