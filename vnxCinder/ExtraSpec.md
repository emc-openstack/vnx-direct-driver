# Extra Spec Options

Extra spec is used in volume types created in cinder as the preferred property
of volume.

Cinder Scheduler will use extra spec to find the suitable backend for the
volume.  And Cinder Driver will create volume based on the properties
specified by extra spec.

Use following command to create a volume type:

        cinder type-create "demoVolumeType"

Use following command to update the extra spec of a volume type:

        cinder type-key "demoVolumeType" set provisioning:type=thin

Volume types can also be configured in OpenStack Horizon.

In VNX Driver, we defined several extra specs.  They are introduced below:

## Provisioning type

* Key: `provisioning:type`
* Possible Values:
    * `thick`

      Volume is fully provisioned.

      Example of creating a `thick` volume type:

        cinder type-create "ThickVolumeType"
        cinder type-key "ThickVolumeType" set provisioning:type=thick thick_provisioning_support='<is> True'

    * `thin`

      Volume is virtually provisioned

      Example of creating a `thin` volume type:

        cinder type-create "ThinVolumeType"
        cinder type-key "ThinVolumeType" set provisioning:type=thin thin_provisioning_support='<is> True'

    * `deduplicated`

      Volume is `thin` and deduplication is enabled.
      The administrator shall go to VNX to configure the system level deduplication
      settings.  To create a deduplicated volume, the VNX Deduplication
      license must be activated on VNX, and specify
      `deduplication_support='<is> True'` to let Block
      Storage scheduler find the proper volume back end.

      Example of creating a `deduplicated` volume type:

        cinder type-create "DeduplicatedVolumeType"
        cinder type-key "DeduplicatedVolumeType" set provisioning:type=deduplicated deduplication_support='<is> True'

    * `compressed`

      Volume is `thin` and compression is enabled.
      The administrator shall go to the
      VNX to configure the system level compression settings.  To create a
      compressed volume, the VNX Compression license must be activated on VNX
      , and use `compression_support='<is> True'` to let Block Storage
      scheduler find a volume back end.  VNX does not support creating snapshots
      on a compressed volume.

      Example of creating a `compressed` volume type:

        cinder type-create "CompressedVolumeType"
        cinder type-key "CompressedVolumeType" set provisioning:type=compressed compression_support='<is> True'


* Default: `thick`

__Note__:

`provisioning:type` replaces the old spec key `storagetype:provisioning`.
The latter one will be obsoleted in the next release.
If both `provisioning:type`and `storagetype:provisioning` are set in the volume
type, the value of `provisioning:type` will be used.

## Storage Tiering Support

* Key: `storagetype:tiering`
* Possible Values:
    * `StartHighThenAuto`
    * `Auto`
    * `HighestAvailable`
    * `LowestAvailable`
    * `NoMovement`
* Default: `StartHighThenAuto`

VNX supports fully automated storage tiering which requires the FAST license
activated on the VNX. The OpenStack administrator can use the extra spec key
`storagetype:tiering` to set the tiering policy of a volume and use the key
`fast_support='<is> True'` to let Block Storage scheduler find a volume back
end which manages a VNX with FAST license activated. Here are the five
supported values for the extra spec key `storagetype:tiering`:

Here is an example of creating a volume types with tiering policy:

        cinder type-create "ThinVolumeOnLowestAvaibleTier"
        cinder type-key "CompressedVolumeOnLowestAvaibleTier" set provisioning:type=thin storagetype:tiering=Auto fast_support='<is> True'

__Note:__

Tiering policy can not be applied to a deduplicated volume.  Tiering policy
of the deduplicated LUN align with the settings of the pool.

## FAST Cache support

* Key: `fast_cache_enabled`
* Possible Values:
    * `True`
    * `False`
* Default: `False`

VNX has FAST Cache feature which requires the FAST Cache license activated on
the VNX.  Volume will be created on the backend with FAST cache enabled when
`True` is specified.

## Pool Name

* Key: `pool_name`
* Possible Values: name of the storage pool managed by cinder
* Default: None

If the user wants to create a volume on a certain storage pool in a backend that
manages multiple pools, a volume type with a extra spec specified storage pool
should be created first, then the user can use this volume type to create the
volume.

Here is an example about the volume type creation:

        cinder type-create "HighPerf"
        cinder type-key "HighPerf" set pool_name=Pool_02_SASFLASH volume_backend_name=vnx_41

## Obsoleted extra specs

Please avoid using following extra spec keys.

* `storagetype:provisioning`
* `storagetype:pool`
