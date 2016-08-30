# Overview

This driver performs the volume operations by executing Navisphere CLI.

Navisphere CLI (NaviSecCLI) is a Command Line Interface (CLI) used for
management, diagnostics and reporting functions for VNX.

## Supported OpenStack Release

This driver supports Mitaka release.

## Important Changes in Mitaka

* `storagetype:provisioning` extra spec has been deprecated, use `provisioning:type` instead.
* `storagetype:pool` extra spec has been deprecated, use `pool_name` instead

## Requirements

* VNX OE for Block version 5.32 or higher.
* VNX Snapshot and Thin Provisioning license should be activated for VNX.
* Navisphere CLI v7.32 or higher is installed along with the driver.
* `storops` python library is required, install it via `sudo pip install storops`.

## Supported Operations

The following operations are supported:

* Create, delete, attach and detach volumes
* Create and delete volume snapshots
* Create a volume from a snapshot
* Copy an image to a volume
* Clone a volume
* Extend a volume
* Migrate a volume
* Retype a volume
* Get volume statistics
* Create and delete consistency groups
* Create and delete consistency group snapshots
* Modify consistency group
* Create a cloned consistency group
* create consistency group from consistency group snapshots
* Efficient non-disruptive volume backup
* Replication v2.1 support
