# Feature Support List

Following table is the feature support list for Cinder VNX driver.

  * O - means the feature is supported.
  * X - means feature is not available.

| Feature                             | 2.0.0 | 3.0.0 | 3.0.1 | 3.0.2 | 3.0.3 | 3.0.4 | 3.0.5 | 4.0.0 | 4.1.0 | 4.2.0 | 5.0.0 | 5.1.0 | 5.2.0 | 5.3.0 | 6.0.0 |7.0.0 |
|-------------------------------------|-------|-------|-------|-------|-------|-------|-------|-------|-------|-------|-------|-------|-------|-------|-------|------|
| Pool-based volume backend           |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Array-based volume backend          |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Create volume                       |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Delete volume                       |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Attach volume (iSCSI)               |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Detach volume (iSCSI)               |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Attach volume (FC)                  |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Detach volume (FC)                  |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Create snapshot                     |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Delete snapshot                     |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Create volume from snap             |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Copy image to volume                |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Copy volume to image                |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Clone volume                        |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Extend volume                       |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| HLU selection                       |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| iSCSI target portal selection       |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| FC target port selection            |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Initiator auto registration         |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Thin/Thick in volume creation       |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Compression in volume creation      |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Deduplication in volume creation    |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Tiering in volume creation          |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| FAST cache support                  |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Storage group auto deletion         |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Multiple authentication type        |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Unisphere CLI security file support |   X   |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Storage-assisted volume migration   |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| SP toggle for HA                    |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Import/Export external volume       |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Read-only volume support            |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Auto zoning support                 |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Consistency group support           |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| LUN number threshold check          |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Initiator auto de-registration      |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Force delete storage group LUNs     |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   X   |   X   |   O   |   O   |   O   |   O   |   O   |   O   |  O   |
| Pool aware scheduler support        |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   O   |   O   |  O   |
| Attach/Detach batch processing      |   X   |   X   |   X   |   O   |   O   |   O   |   O   |   X   |   X   |   O   |   X   |   X   |   X   |   X   |   X   |  X   |
| Modify consistency group            |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   O   |  O   |
| Create consistency group from snap  |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   O   |   O   |   O   |  O   |
| LUN over-subscription               |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |  O   |
| Multiple pools supports             |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |  O   |
| Target ports white list             |   X   |   X   |   X   |   X   |   X   |   X   |   O   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |  O   |
| Snap copy support                   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |  O   |
| efficient non-disruptive backup     |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   O   |  O   |
| Clone consistency group from src    |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |  O   |
| Replication v2.1                    |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |  O   |
| Configurable migration rate         |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |   X   |  O   |